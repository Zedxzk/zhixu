"""Authenticated loopback broker separating channel network code from domain data."""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhixu.adapters.channels import InboundReceiptStore
from zhixu.adapters.storage.sqlite import ChannelRouteStore, UserRepository
from zhixu.application import AssistantEngine
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
    AuthenticationStrength,
    CommandContext,
    RequestChannel,
)
from zhixu.domain.errors import ConflictError, PermissionDenied, ValidationError
from zhixu.security import OpaqueReferenceFactory

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
            metadata={"mentioned": payload.mentioned},
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
                authentication=AuthenticationStrength.CHANNEL,
                request_channel=(
                    RequestChannel.GROUP_CHAT
                    if event.conversation_kind is ConversationKind.GROUP
                    else RequestChannel.PRIVATE_CHAT
                ),
            )
            reply = self.assistant.handle(
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
                message=self._reply_message(event, reply.text),
                now=event.received_at,
            )
            return AdminResponse(202, {"accepted": True, "reason_code": "accepted"})
        finally:
            self.receipts.complete(event, decision, intent_kind=intent_kind)

    def _admit(self, event: InboundEvent) -> ChannelAdmissionDecision:
        identity = self.users.identity_by_opaque_ref(
            event.channel,
            event.channel_account,
            event.external_actor_ref,
        )
        if identity is None:
            return ChannelAdmissionDecision(False, None, "identity_unbound")
        if event.conversation_kind is ConversationKind.GROUP:
            explicitly_addressed = bool(event.metadata.get("mentioned")) or (
                event.text or ""
            ).lstrip().startswith("/")
            if not explicitly_addressed:
                return ChannelAdmissionDecision(
                    False,
                    identity.user_id,
                    "group_trigger_required",
                )
            if not self.routes.commands_enabled(
                event.channel,
                event.channel_account,
                event.external_conversation_ref,
            ):
                return ChannelAdmissionDecision(
                    False,
                    identity.user_id,
                    "conversation_disabled",
                )
        return ChannelAdmissionDecision(True, identity.user_id, "accepted")

    @staticmethod
    def _reply_message(event: InboundEvent, text: str):
        from zhixu.channels import OutboundMessage

        return OutboundMessage(
            event.channel,
            event.channel_account,
            event.external_conversation_ref,
            MessageKind.TEXT,
            text,
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
                        {"label": button.label, "action": button.action}
                        for button in rendered.buttons
                    ],
                    "attachment_url": rendered.attachment_url,
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
