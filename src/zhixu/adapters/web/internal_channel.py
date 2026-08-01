"""Authenticated loopback broker separating channel network code from domain data."""

from __future__ import annotations

import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhixu.adapters.channels import InboundReceiptStore
from zhixu.adapters.storage.sqlite import (
    ChannelRoute,
    ChannelRouteStore,
    GroupMode,
    UserRepository,
)
from zhixu.application import AssistantEngine, AssistantReply
from zhixu.application.intents import IntentAction
from zhixu.channels import (
    ChannelCapabilities,
    ChannelDeliveryResult,
    ConversationKind,
    InboundEvent,
    MessageKind,
)
from zhixu.delivery import OutboxStore, QuotaManager, render_for_capabilities
from zhixu.delivery.quota import QuotaScope
from zhixu.domain import (
    Action,
    AuthenticationStrength,
    CommandContext,
    EncryptedIdentifier,
    ExternalIdentity,
    RequestChannel,
    ResourceRef,
    User,
    UserStatus,
)
from zhixu.domain.errors import ConflictError, PermissionDenied, ValidationError
from zhixu.security import FieldCipher, OpaqueReferenceFactory

from .admin import AdminResponse


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class InboundPayload(_StrictModel):
    event_id: str = Field(min_length=1, max_length=512)
    channel: str = Field(min_length=1, max_length=40)
    channel_account: str = Field(min_length=1, max_length=160)
    actor_ref: str = Field(min_length=1, max_length=160)
    conversation_ref: str = Field(min_length=1, max_length=160)
    conversation_kind: ConversationKind
    message_kind: MessageKind
    text: str = Field(min_length=1, max_length=20_000)
    received_at: datetime
    mentioned: bool = False
    reply_context_ref: str = Field(default="", max_length=160)

    @field_validator("received_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("received_at must include a timezone")
        return value


class ClaimPayload(_StrictModel):
    channel: str = Field(min_length=1, max_length=40)
    channel_account: str = Field(min_length=1, max_length=160)
    worker_id: str = Field(min_length=1, max_length=160)


class CompletePayload(_StrictModel):
    delivery_id: str = Field(min_length=1, max_length=160)
    lease_token: str = Field(min_length=1, max_length=256)
    ok: bool
    retryable: bool = False
    provider_code: str = Field(default="", max_length=80)
    provider_message_id: str = Field(default="", max_length=160)


@dataclass(frozen=True, slots=True)
class ChannelAdmissionDecision:
    accepted: bool
    user_id: str | None
    reason_code: str
    roles: frozenset[str] = frozenset()
    shared_owner_user_id: str | None = None
    readable_shared_owner_user_ids: tuple[str, ...] = ()
    reply: AssistantReply | None = None


class InternalChannelAPI:
    def __init__(
        self,
        *,
        service_token: str,
        users: UserRepository,
        routes: ChannelRouteStore,
        receipts: InboundReceiptStore,
        assistant: AssistantEngine,
        outbox: OutboxStore,
        quota: QuotaManager,
        references: OpaqueReferenceFactory,
        capabilities: dict[str, ChannelCapabilities],
        field_cipher: FieldCipher | None = None,
    ) -> None:
        if len(service_token) < 32:
            raise ValueError("internal channel service token is too short")
        self._service_token = service_token
        self.users = users
        self.routes = routes
        self.receipts = receipts
        self.assistant = assistant
        self.outbox = outbox
        self.quota = quota
        self.references = references
        self.capabilities = capabilities
        self.field_cipher = field_cipher

    def dispatch(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: bytes,
    ) -> AdminResponse:
        try:
            self._authenticate(headers)
            if method == "POST" and path == "/internal/channel/event":
                return self._event(InboundPayload.model_validate_json(body))
            if method == "POST" and path == "/internal/channel/delivery/claim":
                return self._claim(ClaimPayload.model_validate_json(body))
            if method == "POST" and path == "/internal/channel/delivery/complete":
                return self._complete(CompletePayload.model_validate_json(body))
            return _error(404, "not_found", "route was not found")
        except PermissionDenied:
            return _error(403, "permission_denied", "internal channel request denied")
        except (ValueError, ValidationError):
            return _error(422, "validation_error", "internal channel request is invalid")
        except ConflictError:
            return _error(409, "conflict", "delivery lease is no longer valid")
        except Exception:
            return _error(500, "internal_error", "internal server error")

    def _authenticate(self, headers: dict[str, str]) -> None:
        value = headers.get("authorization", "")
        scheme, _, token = value.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            token,
            self._service_token,
        ):
            raise PermissionDenied("invalid internal service credential")

    def _event(self, payload: InboundPayload) -> AdminResponse:
        event = InboundEvent(
            event_id=payload.event_id,
            channel=payload.channel,
            channel_account=payload.channel_account,
            external_actor_ref=payload.actor_ref,
            external_conversation_ref=payload.conversation_ref,
            conversation_kind=payload.conversation_kind,
            message_kind=payload.message_kind,
            received_at=payload.received_at,
            text=payload.text,
            metadata={
                "mentioned": payload.mentioned,
                "reply_context_ref": payload.reply_context_ref,
            },
        )
        self.routes.observe(
            channel=event.channel,
            channel_account=event.channel_account,
            opaque_ref=event.external_conversation_ref,
            kind=event.conversation_kind.value,
            now=event.received_at,
        )
        if event.external_actor_ref != event.external_conversation_ref:
            self.routes.observe(
                channel=event.channel,
                channel_account=event.channel_account,
                opaque_ref=event.external_actor_ref,
                kind="actor",
                now=event.received_at,
            )
        decision = self._admit(event)
        if not self.receipts.reserve(event, decision):
            return AdminResponse(200, {"accepted": False, "reason_code": "duplicate_event"})
        intent_kind = ""
        try:
            if not decision.accepted or decision.user_id is None:
                return AdminResponse(
                    200,
                    {"accepted": False, "reason_code": decision.reason_code},
                )
            context = CommandContext(
                actor_user_id=decision.user_id,
                roles=decision.roles,
                shared_owner_user_id=decision.shared_owner_user_id,
                readable_shared_owner_user_ids=(
                    decision.readable_shared_owner_user_ids
                ),
                authentication=AuthenticationStrength.CHANNEL,
                request_channel=(
                    RequestChannel.GROUP_CHAT
                    if event.conversation_kind is ConversationKind.GROUP
                    else RequestChannel.PRIVATE_CHAT
                ),
            )
            reply = decision.reply or self.assistant.handle(
                event.text or "",
                context,
                target_ref=event.external_conversation_ref,
            )
            intent_kind = reply.code
            reply_ref = self.references.create(
                "reply",
                event.channel,
                event.channel_account,
                event.event_id,
            )
            self.outbox.enqueue(
                delivery_id=reply_ref,
                idempotency_key=reply_ref,
                owner_user_id=decision.user_id,
                message=self._reply_message(event, reply),
                now=event.received_at,
            )
            return AdminResponse(202, {"accepted": True, "reason_code": "accepted"})
        finally:
            self.receipts.complete(event, decision, intent_kind=intent_kind)

    def _admit(self, event: InboundEvent) -> ChannelAdmissionDecision:
        if event.conversation_kind is ConversationKind.GROUP:
            explicitly_addressed = bool(event.metadata.get("mentioned")) or (
                event.text or ""
            ).lstrip().startswith("/")
            route = self.routes.get(
                event.channel,
                event.channel_account,
                event.external_conversation_ref,
            )
            if not explicitly_addressed:
                continuous_actor = self.users.identity_by_opaque_ref(
                    event.channel,
                    event.channel_account,
                    event.external_actor_ref,
                )
                continuous_plan = (
                    self.assistant.pending_plans.current(
                        actor_user_id=continuous_actor.user_id,
                        target_ref=event.external_conversation_ref,
                        now=self.assistant.services.clock.now(),
                    )
                    if route is not None
                    and route.commands_enabled
                    and route.group_mode is GroupMode.INTERNAL
                    and continuous_actor is not None
                    and self.assistant.pending_plans is not None
                    else None
                )
                if continuous_plan is None:
                    return ChannelAdmissionDecision(
                        False,
                        None,
                        "group_trigger_required",
                    )
            activation = re.fullmatch(
                r"/启用内部群\s+(\d{8})",
                (event.text or "").strip(),
            )
            if activation is not None:
                activated = self._activate_group(event, activation.group(1))
                if activated is not None:
                    return activated
            if (
                route is None
                or not route.commands_enabled
                or route.group_mode is GroupMode.DISABLED
            ):
                return ChannelAdmissionDecision(
                    False,
                    None,
                    "conversation_disabled",
                )
            if route.group_mode is GroupMode.PUBLIC:
                if route.owner_user_id is None:
                    return ChannelAdmissionDecision(False, None, "route_owner_unavailable")
                owner = self.users.get(route.owner_user_id)
                if owner is None or owner.status is not UserStatus.ACTIVE:
                    return ChannelAdmissionDecision(False, None, "route_owner_unavailable")
                intent = self.assistant.router.route(event.text or "")
                if intent is None or intent.action not in {
                    IntentAction.HELP,
                    IntentAction.ANSWER,
                }:
                    return ChannelAdmissionDecision(
                        False,
                        route.owner_user_id,
                        "group_permission_denied",
                        frozenset({"public_group_guest"}),
                    )
                return ChannelAdmissionDecision(
                    True,
                    route.owner_user_id,
                    "accepted",
                    frozenset({"public_group_guest"}),
                )
            identity = self.users.identity_by_opaque_ref(
                event.channel,
                event.channel_account,
                event.external_actor_ref,
            )
            if identity is None:
                identity = self._provision_group_member(event, route)
                if identity is None:
                    return ChannelAdmissionDecision(False, None, "identity_unbound")
            user = self.users.get(identity.user_id)
            if user is None or user.status is not UserStatus.ACTIVE:
                return ChannelAdmissionDecision(False, identity.user_id, "user_disabled")
            if not self.routes.is_member(
                event.channel,
                event.channel_account,
                event.external_conversation_ref,
                identity.user_id,
            ):
                if route.owner_user_id is None:
                    return ChannelAdmissionDecision(
                        False,
                        identity.user_id,
                        "group_permission_denied",
                    )
                self.routes.add_member(
                    route=route,
                    user_id=identity.user_id,
                    added_by_user_id=route.owner_user_id,
                    now=event.received_at,
                )
            if route.shared_owner_user_id is None:
                return ChannelAdmissionDecision(
                    False,
                    identity.user_id,
                    "group_workspace_unavailable",
                )
            roles = {"internal_group_member", "shared_workspace_member"}
            if identity.user_id == route.owner_user_id:
                roles.add("group_owner")
            private_link = re.fullmatch(
                r"/绑定私聊\s+(\d{8})",
                (event.text or "").strip(),
            )
            if private_link is not None:
                return self._bind_private_identity(
                    event,
                    identity,
                    route,
                    private_link.group(1),
                    frozenset(roles),
                )
            return ChannelAdmissionDecision(
                True,
                identity.user_id,
                "accepted",
                frozenset(roles),
                route.shared_owner_user_id,
                (route.shared_owner_user_id,),
            )
        identity = self.users.identity_by_opaque_ref(
            event.channel,
            event.channel_account,
            event.external_actor_ref,
        )
        if identity is None:
            if event.conversation_kind is ConversationKind.PRIVATE:
                return self._unbound_private(event)
            return ChannelAdmissionDecision(False, None, "identity_unbound")
        user = self.users.get(identity.user_id)
        if user is None or user.status is not UserStatus.ACTIVE:
            return ChannelAdmissionDecision(False, identity.user_id, "user_disabled")
        if (event.text or "").strip() == "/申请绑定":
            return ChannelAdmissionDecision(
                True,
                identity.user_id,
                "accepted",
                reply=AssistantReply(
                    "当前私聊身份已经完成绑定，无需重复申请。",
                    "identity_already_bound",
                    "deterministic",
                ),
            )
        if (event.text or "").strip() == "/登记内部群":
            if not self.users.has_role(identity.user_id, "project_admin"):
                return ChannelAdmissionDecision(
                    False,
                    identity.user_id,
                    "group_permission_denied",
                )
            code = f"{secrets.randbelow(100_000_000):08d}"
            self.routes.issue_activation(
                code_hash=self.references.create(
                    "group_activation",
                    event.channel,
                    event.channel_account,
                    code,
                ),
                user_id=identity.user_id,
                channel=event.channel,
                channel_account=event.channel_account,
                group_mode=GroupMode.INTERNAL,
                expires_at=event.received_at + timedelta(minutes=10),
                now=event.received_at,
            )
            return ChannelAdmissionDecision(
                True,
                identity.user_id,
                "accepted",
                reply=AssistantReply(
                    f"群登记码：{code}\n10分钟内在目标群发送：/启用内部群 {code}",
                    "group_activation_issued",
                    "deterministic",
                ),
            )
        shared_owners = self.routes.shared_owners_for_member(identity.user_id)
        roles = set()
        if shared_owners:
            roles.add("shared_workspace_member")
        if self.users.has_role(identity.user_id, "project_admin"):
            roles.add("project_admin")
        return ChannelAdmissionDecision(
            True,
            identity.user_id,
            "accepted",
            frozenset(roles),
            None,
            shared_owners,
        )

    def _unbound_private(self, event: InboundEvent) -> ChannelAdmissionDecision:
        registration = self.users.get("service:registration")
        if registration is None or registration.status is not UserStatus.ACTIVE:
            return ChannelAdmissionDecision(False, None, "registration_unavailable")
        if (event.text or "").strip() != "/申请绑定":
            return ChannelAdmissionDecision(
                True,
                registration.id,
                "accepted",
                frozenset({"pending_identity"}),
                reply=AssistantReply(
                    "当前私聊尚未绑定。请发送 /申请绑定 获取一次性码，"
                    "再到已启用的内部群发送 /绑定私聊 绑定码。",
                    "identity_link_required",
                    "deterministic",
                ),
            )
        code = f"{secrets.randbelow(100_000_000):08d}"
        self.routes.issue_private_link(
            code_hash=self.references.create(
                "private_link",
                event.channel,
                event.channel_account,
                code,
            ),
            channel=event.channel,
            channel_account=event.channel_account,
            private_actor_ref=event.external_actor_ref,
            expires_at=event.received_at + timedelta(minutes=10),
            now=event.received_at,
        )
        return ChannelAdmissionDecision(
            True,
            registration.id,
            "accepted",
            frozenset({"pending_identity"}),
            reply=AssistantReply(
                f"私聊绑定码：{code}\n"
                f"10分钟内由本人在已启用的内部群发送：/绑定私聊 {code}",
                "identity_link_issued",
                "deterministic",
            ),
        )

    def _bind_private_identity(
        self,
        event: InboundEvent,
        group_identity: ExternalIdentity,
        route: ChannelRoute,
        code: str,
        roles: frozenset[str],
    ) -> ChannelAdmissionDecision:
        if self.field_cipher is None or route.shared_owner_user_id is None:
            return ChannelAdmissionDecision(
                False,
                group_identity.user_id,
                "registration_unavailable",
            )
        private_actor_ref = self.routes.consume_private_link(
            code_hash=self.references.create(
                "private_link",
                event.channel,
                event.channel_account,
                code,
            ),
            channel=event.channel,
            channel_account=event.channel_account,
            consumed_by_user_id=group_identity.user_id,
            now=event.received_at,
        )
        if private_actor_ref is None:
            return ChannelAdmissionDecision(
                True,
                group_identity.user_id,
                "accepted",
                roles,
                route.shared_owner_user_id,
                (route.shared_owner_user_id,),
                AssistantReply(
                    "私聊绑定码无效或已过期，请在私聊中重新发送 /申请绑定。",
                    "identity_link_invalid",
                    "deterministic",
                ),
            )
        existing = self.users.identity_by_opaque_ref(
            event.channel,
            event.channel_account,
            private_actor_ref,
        )
        if existing is not None and existing.user_id != group_identity.user_id:
            return ChannelAdmissionDecision(
                True,
                group_identity.user_id,
                "accepted",
                roles,
                route.shared_owner_user_id,
                (route.shared_owner_user_id,),
                AssistantReply(
                    "该私聊身份已绑定到其他用户，操作已拒绝。",
                    "identity_link_conflict",
                    "deterministic",
                ),
            )
        if existing is None:
            identity_id = self.references.create(
                "identity",
                event.channel,
                event.channel_account,
                private_actor_ref,
            )
            context = CommandContext(
                actor_user_id=group_identity.user_id,
                now=event.received_at,
            )
            self.users.bind_identity(
                ExternalIdentity(
                    identity_id,
                    group_identity.user_id,
                    event.channel,
                    event.channel_account,
                    EncryptedIdentifier(
                        self.field_cipher.encrypt(
                            private_actor_ref,
                            context=(
                                f"external-identity:{event.channel}:"
                                f"{event.channel_account}:{private_actor_ref}"
                            ),
                        )
                    ),
                    private_actor_ref,
                    event.received_at,
                ),
                self.assistant.services.policy.require(
                    context,
                    Action.CREATE,
                    ResourceRef(
                        "external_identity",
                        identity_id,
                        group_identity.user_id,
                    ),
                ),
            )
        return ChannelAdmissionDecision(
            True,
            group_identity.user_id,
            "accepted",
            roles,
            route.shared_owner_user_id,
            (route.shared_owner_user_id,),
            AssistantReply(
                "私聊身份绑定成功。现在可在私聊中使用个人接口和本人已加入群的共享库。",
                "identity_linked",
                "deterministic",
            ),
        )

    def _activate_group(
        self,
        event: InboundEvent,
        code: str,
    ) -> ChannelAdmissionDecision | None:
        if self.field_cipher is None:
            return None
        consumed = self.routes.consume_activation(
            code_hash=self.references.create(
                "group_activation",
                event.channel,
                event.channel_account,
                code,
            ),
            channel=event.channel,
            channel_account=event.channel_account,
            now=event.received_at,
        )
        if consumed is None:
            return None
        user_id, mode = consumed
        user = self.users.get(user_id)
        if (
            user is None
            or user.status is not UserStatus.ACTIVE
            or not self.users.has_role(user_id, "project_admin")
        ):
            return None
        identity = self.users.identity_by_opaque_ref(
            event.channel,
            event.channel_account,
            event.external_actor_ref,
        )
        if identity is not None and identity.user_id != user_id:
            return None
        if identity is None:
            identity_id = self.references.create(
                "identity",
                event.channel,
                event.channel_account,
                event.external_actor_ref,
            )
            context = CommandContext(actor_user_id=user_id, now=event.received_at)
            self.users.bind_identity(
                ExternalIdentity(
                    identity_id,
                    user_id,
                    event.channel,
                    event.channel_account,
                    EncryptedIdentifier(
                        self.field_cipher.encrypt(
                            event.external_actor_ref,
                            context=(
                                f"external-identity:{event.channel}:"
                                f"{event.channel_account}:{event.external_actor_ref}"
                            ),
                        )
                    ),
                    event.external_actor_ref,
                    event.received_at,
                ),
                self.assistant.services.policy.require(
                    context,
                    Action.CREATE,
                    ResourceRef("external_identity", identity_id, user_id),
                ),
            )
        self.routes.set_commands_enabled(
            channel=event.channel,
            channel_account=event.channel_account,
            opaque_ref=event.external_conversation_ref,
            enabled=True,
            actor_user_id=user_id,
            now=event.received_at,
            group_mode=mode,
            member_user_ids=(user_id,),
        )
        route = self.routes.get(
            event.channel,
            event.channel_account,
            event.external_conversation_ref,
        )
        if route is None or route.shared_owner_user_id is None:
            return None
        return ChannelAdmissionDecision(
            True,
            user_id,
            "accepted",
            frozenset(
                {
                    "project_admin",
                    "group_owner",
                    "internal_group_member",
                    "shared_workspace_member",
                }
            ),
            route.shared_owner_user_id,
            (route.shared_owner_user_id,),
            AssistantReply(
                "内部群已自动登记。已绑定成员可直接使用，新群成员首次使用时会自动加入本群共享库。",
                "group_activated",
                "deterministic",
            ),
        )

    def _provision_group_member(
        self,
        event: InboundEvent,
        route: ChannelRoute,
    ) -> ExternalIdentity | None:
        if self.field_cipher is None or route.owner_user_id is None:
            return None
        member_id = self.references.create(
            "group_member",
            event.channel,
            event.channel_account,
            event.external_actor_ref,
        )
        now = event.received_at
        context = CommandContext(actor_user_id=member_id, now=now)
        if self.users.get(member_id) is None:
            try:
                self.users.create(
                    User(member_id, "QQ group member", UserStatus.ACTIVE, now),
                    self.assistant.services.policy.require(
                        context,
                        Action.CREATE,
                        ResourceRef("user", member_id, member_id),
                    ),
                )
            except ConflictError:
                if self.users.get(member_id) is None:
                    return None
        identity_id = self.references.create(
            "identity",
            event.channel,
            event.channel_account,
            event.external_actor_ref,
        )
        try:
            self.users.bind_identity(
                ExternalIdentity(
                    identity_id,
                    member_id,
                    event.channel,
                    event.channel_account,
                    EncryptedIdentifier(
                        self.field_cipher.encrypt(
                            event.external_actor_ref,
                            context=(
                                f"external-identity:{event.channel}:"
                                f"{event.channel_account}:{event.external_actor_ref}"
                            ),
                        )
                    ),
                    event.external_actor_ref,
                    now,
                ),
                self.assistant.services.policy.require(
                    context,
                    Action.CREATE,
                    ResourceRef("external_identity", identity_id, member_id),
                ),
            )
        except ConflictError:
            existing = self.users.identity_by_opaque_ref(
                event.channel,
                event.channel_account,
                event.external_actor_ref,
            )
            if existing is None or existing.user_id != member_id:
                return None
        self.routes.add_member(
            route=route,
            user_id=member_id,
            added_by_user_id=route.owner_user_id,
            now=now,
        )
        return self.users.identity_by_opaque_ref(
            event.channel,
            event.channel_account,
            event.external_actor_ref,
        )

    @staticmethod
    def _reply_message(event: InboundEvent, reply: AssistantReply):
        from zhixu.channels import OutboundMessage

        return OutboundMessage(
            event.channel,
            event.channel_account,
            event.external_conversation_ref,
            (
                MessageKind.BUTTON
                if reply.buttons
                else MessageKind.MARKDOWN
                if reply.rich_text
                else MessageKind.TEXT
            ),
            reply.text,
            buttons=reply.buttons,
            calendar_preview=reply.calendar_preview,
            reply_context_ref=(
                ""
                if event.message_kind is MessageKind.BUTTON
                else str(event.metadata.get("reply_context_ref") or "")
            ),
        )

    def _claim(self, payload: ClaimPayload) -> AdminResponse:
        capabilities = self.capabilities.get(payload.channel)
        if capabilities is None:
            raise ValidationError("channel capability contract is unavailable")
        now = self.assistant.services.clock.now()
        claimed = self.outbox.claim(
            worker_id=payload.worker_id,
            now=now,
            accounts=((payload.channel, payload.channel_account),),
        )
        if claimed is None:
            return AdminResponse(200, {"delivery": None})
        message = claimed.message
        decision = self.quota.reserve(
            (
                QuotaScope("provider", message.channel),
                QuotaScope("account", message.channel_account),
                QuotaScope("conversation", message.target_ref),
                QuotaScope("user", claimed.owner_user_id),
            ),
            now=now,
        )
        if not decision.allowed:
            self.outbox.defer_for_quota(
                claimed,
                next_attempt_at=decision.next_available_at,
                now=now,
                reason_code=decision.reason_code,
            )
            return AdminResponse(200, {"delivery": None})
        rendered = render_for_capabilities(message, capabilities)
        return AdminResponse(
            200,
            {
                "delivery": {
                    "id": claimed.id,
                    "lease_token": claimed.lease_token,
                    "channel": rendered.channel,
                    "channel_account": rendered.channel_account,
                    "target_ref": rendered.target_ref,
                    "kind": rendered.kind.value,
                    "text": rendered.text,
                    "buttons": [
                        {
                            "label": button.label,
                            "action": button.action,
                            "kind": button.kind.value,
                        }
                        for button in rendered.buttons
                    ],
                    "attachment_url": rendered.attachment_url,
                    "calendar_preview": (
                        {
                            "year": rendered.calendar_preview.year,
                            "month": rendered.calendar_preview.month,
                            "busy_day_counts": [
                                [day, count]
                                for day, count in rendered.calendar_preview.busy_day_counts
                            ],
                            "today_day": rendered.calendar_preview.today_day,
                        }
                        if rendered.calendar_preview is not None
                        else None
                    ),
                    "daily_agenda_preview": (
                        {
                            "year": rendered.daily_agenda_preview.year,
                            "month": rendered.daily_agenda_preview.month,
                            "day": rendered.daily_agenda_preview.day,
                            "entries": [
                                [start, end, kind]
                                for start, end, kind in (
                                    rendered.daily_agenda_preview.entries
                                )
                            ],
                            "anniversary_day_numbers": list(
                                rendered.daily_agenda_preview.anniversary_day_numbers
                            ),
                        }
                        if rendered.daily_agenda_preview is not None
                        else None
                    ),
                    "reply_context_ref": rendered.reply_context_ref,
                    "classification": int(rendered.classification),
                }
            },
        )

    def _complete(self, payload: CompletePayload) -> AdminResponse:
        status = self.outbox.complete_lease(
            delivery_id=payload.delivery_id,
            lease_token=payload.lease_token,
            result=ChannelDeliveryResult(
                payload.ok,
                payload.retryable,
                payload.provider_code,
                payload.provider_message_id,
            ),
            now=self.assistant.services.clock.now(),
        )
        return AdminResponse(200, {"status": status})


def _error(status: int, code: str, message: str) -> AdminResponse:
    return AdminResponse(status, {"error": {"code": code, "message": message}})


def encode_payload(value: BaseModel | dict[str, Any]) -> bytes:
    if isinstance(value, BaseModel):
        return value.model_dump_json().encode()
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
