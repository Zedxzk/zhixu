"""Dependency-light private administration API with strict redaction."""

from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from zhixu.adapters.channels import (
    ChannelRegistry,
    OutboundTargetStore,
)
from zhixu.adapters.storage.sqlite import (
    AdminCredentialStore,
    AdminReadStore,
    AdminSessionStore,
    ChannelRouteStore,
    GrantRepository,
    GroupMode,
    IdentityLinkStore,
    UserRepository,
    acl_action,
)
from zhixu.application.commands import (
    CancelReminder,
    CreateAgenda,
    CreateAnniversary,
    CreateNote,
    CreateReminder,
    CreateTask,
    DeleteAgenda,
    DeleteNote,
    DeleteTask,
    PostponeTask,
    SetAgendaException,
    TransitionTask,
    UpdateAgenda,
    UpdateNote,
    UpdateTask,
)
from zhixu.application.queries import ListAnniversaries, ListReminders
from zhixu.application.services import ZhixuServices, random_id
from zhixu.channels import MessageKind, OutboundMessage
from zhixu.delivery import OutboxStore
from zhixu.domain import (
    Action,
    AuthenticationStrength,
    CalendarSystem,
    CommandContext,
    DataClassification,
    EncryptedIdentifier,
    ExceptionAction,
    ImportantDayKind,
    MissedReminderPolicy,
    NoteAttachment,
    PolicyEngine,
    RequestChannel,
    ResourceRef,
    TaskStatus,
)
from zhixu.domain.errors import (
    ClassificationNotSupported,
    ConcurrencyConflict,
    ConfirmationRequired,
    ConflictError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
    ZhixuError,
)
from zhixu.ports import Clock
from zhixu.security import FieldCipher, OpaqueReferenceFactory
from zhixu.vault_client import CapabilityGrantIssuer, UnixVaultClient

MAX_BODY_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class AdminResponse:
    status: int
    body: dict[str, object] | list[object] | bytes
    headers: tuple[tuple[str, str], ...] = ()


Probe = Callable[[], bool]


@dataclass(slots=True)
class HealthRegistry:
    """Tracks optional dependencies without coupling core readiness to them."""

    storage_probe: Probe
    optional_probes: Mapping[str, Probe] = field(default_factory=dict)

    def snapshot(self) -> dict[str, object]:
        storage = self._probe(self.storage_probe)
        optional = {
            name: ("available" if self._probe(probe) else "degraded")
            for name, probe in sorted(self.optional_probes.items())
        }
        return {
            "core": "ready" if storage else "unavailable",
            "ready": storage,
            "degraded": any(value != "available" for value in optional.values()),
            "components": optional,
        }

    @staticmethod
    def _probe(probe: Probe) -> bool:
        try:
            return bool(probe())
        except Exception:
            return False


class AdminAPI:
    """Authenticated private API. The runtime must bind it to loopback only."""

    def __init__(
        self,
        *,
        services: ZhixuServices,
        policy: PolicyEngine,
        users: UserRepository,
        grants: GrantRepository,
        sessions: AdminSessionStore,
        identity_links: IdentityLinkStore,
        reads: AdminReadStore,
        clock: Clock,
        field_cipher: FieldCipher,
        references: OpaqueReferenceFactory,
        health: HealthRegistry | None = None,
        channels: ChannelRegistry | None = None,
        credentials: AdminCredentialStore | None = None,
        channel_routes: ChannelRouteStore | None = None,
        outbox: OutboxStore | None = None,
        outbound_targets: OutboundTargetStore | None = None,
        outbound_target_kinds: Mapping[tuple[str, str], str] | None = None,
        vault_client: UnixVaultClient | None = None,
        grant_issuer: CapabilityGrantIssuer | None = None,
    ) -> None:
        self.services = services
        self.policy = policy
        self.users = users
        self.grants = grants
        self.sessions = sessions
        self.identity_links = identity_links
        self.reads = reads
        self.clock = clock
        self.field_cipher = field_cipher
        self.references = references
        self.channels = channels or ChannelRegistry()
        self.credentials = credentials
        self.channel_routes = channel_routes
        self.outbox = outbox
        self.outbound_targets = outbound_targets
        self.outbound_target_kinds = dict(outbound_target_kinds or {})
        self.vault_client = vault_client
        self.grant_issuer = grant_issuer
        self.health = health or HealthRegistry(
            storage_probe=lambda: self.reads.status()["storage"] == "available"
        )

    def dispatch(
        self,
        method: str,
        target: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> AdminResponse:
        method = method.upper()
        headers = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlsplit(target)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if method == "GET" and path == "/health/live":
                return AdminResponse(200, {"status": "live"})
            if method == "GET" and path == "/health/ready":
                snapshot = self.health.snapshot()
                return AdminResponse(
                    200 if bool(snapshot["ready"]) else 503,
                    {"status": "ready" if snapshot["ready"] else "unavailable", **snapshot},
                )
            if method == "POST" and path == "/admin/session":
                return self._login(self._object_body(body))

            principal = self._authenticate(headers)
            context = self._admin_context(
                principal,
                confirmed=headers.get("x-zhixu-confirm", "").lower() == "true",
            )

            if method == "GET" and path == "/admin/status":
                return AdminResponse(
                    200,
                    {
                        "health": self.health.snapshot(),
                        "application": self.reads.status(),
                    },
                )
            if method == "GET" and path == "/admin/workspaces":
                return AdminResponse(
                    200,
                    [item for _owner, item in self._workspace_records(context)],
                )
            if method == "DELETE" and path == "/admin/session":
                raw_token = headers["authorization"].partition(" ")[2]
                return AdminResponse(
                    200,
                    {"revoked": self.sessions.revoke(raw_token, now=self.clock.now())},
                )
            if path == "/admin/passkeys/registration/options" and method == "POST":
                return self._begin_passkey_registration(principal.user_id)
            if path == "/admin/passkeys/registration/verify" and method == "POST":
                return self._finish_passkey_registration(
                    principal.user_id,
                    self._object_body(body),
                )
            if path == "/admin/passkeys/authentication/options" and method == "POST":
                return self._begin_passkey_authentication(principal.user_id)
            if path == "/admin/passkeys/authentication/verify" and method == "POST":
                return self._finish_passkey_authentication(
                    principal.user_id,
                    self._object_body(body),
                )
            if path == "/admin/vault/secrets":
                if method == "GET":
                    return self._vault_list(principal)
                if method == "POST":
                    return self._vault_create(
                        principal,
                        self._object_body(body),
                        confirmed=context.confirmed,
                    )
            if path.startswith("/admin/vault/secrets/"):
                parts = path.strip("/").split("/")
                if len(parts) == 4:
                    secret_id = parts[3]
                    if method == "PUT":
                        return self._vault_update(
                            principal,
                            secret_id,
                            self._object_body(body),
                        )
                    if method == "DELETE":
                        if not context.confirmed:
                            raise ConfirmationRequired("vault deletion requires confirmation")
                        return self._vault_delete(principal, secret_id)
                if len(parts) == 5 and method == "POST":
                    secret_id = parts[3]
                    action = parts[4]
                    data = self._object_body(body)
                    if action == "reveal":
                        return self._vault_reveal(principal, secret_id, data)
                    if action == "use":
                        return self._vault_use(principal, secret_id, data)
                    if action == "export":
                        return self._vault_export(principal, secret_id, data)
                    if action == "grant":
                        return self._vault_grant_acl(principal, secret_id, data)
            if method == "GET" and path == "/admin/channels":
                return AdminResponse(
                    200,
                    [
                        {
                            "channel": item.channel,
                            "channel_account": item.channel_account,
                            "mode": item.mode,
                            "capabilities": item.capabilities,
                        }
                        for item in self.channels.describe()
                    ],
                )
            if path == "/admin/channel-routes" and self.channel_routes is not None:
                if method == "GET":
                    return AdminResponse(
                        200,
                        [
                            {
                                "channel": route.channel,
                                "channel_account": route.channel_account,
                                "opaque_ref": route.opaque_ref,
                                "kind": route.kind,
                                "commands_enabled": route.commands_enabled,
                                "group_mode": route.group_mode.value,
                                "member_user_ids": list(route.member_user_ids),
                                "last_seen_at": route.last_seen_at.isoformat(),
                            }
                            for route in self.channel_routes.list()
                        ],
                    )
                if method == "PATCH":
                    return self._update_channel_route(
                        self._object_body(body),
                        context,
                    )
            if method == "GET" and path == "/admin/identities":
                return AdminResponse(200, self.reads.identities(principal.user_id))
            if method == "POST" and path == "/admin/identity-challenges":
                return self._issue_identity_challenge(
                    self._object_body(body),
                    principal.user_id,
                )
            if method == "POST" and path == "/admin/identities":
                return self._consume_identity_challenge(
                    self._object_body(body),
                    context,
                )
            if method == "DELETE" and path.startswith("/admin/identities/"):
                return self._unbind_identity(path.rsplit("/", 1)[-1], context)
            if path == "/admin/acl":
                if method == "GET":
                    return self._list_acl(query, context)
                if method in {"POST", "DELETE"}:
                    return self._change_acl(
                        self._object_body(body),
                        context,
                        revoke=method == "DELETE",
                    )
            if path == "/admin/agenda":
                if method == "GET":
                    return self._list_agenda(context)
                if method == "POST":
                    return self._create_agenda(self._object_body(body), context)
            if path == "/admin/important-days":
                if method == "GET":
                    return self._list_important_days(context)
                if method == "POST":
                    return self._create_important_day(
                        self._object_body(body),
                        context,
                    )
            if path.startswith("/admin/agenda/") and path.endswith("/exceptions"):
                parts = path.strip("/").split("/")
                if method == "POST" and len(parts) == 4:
                    return self._set_agenda_exception(
                        parts[2],
                        self._object_body(body),
                        context,
                    )
            if path.startswith("/admin/agenda/"):
                item_id = path.rsplit("/", 1)[-1]
                if method == "PUT":
                    return self._update_agenda(item_id, self._object_body(body), context)
                if method == "DELETE":
                    self.services.delete_agenda(DeleteAgenda(item_id), context)
                    return AdminResponse(200, {"deleted": True, "id": item_id})
            if path == "/admin/tasks":
                if method == "GET":
                    return self._list_tasks(context)
                if method == "POST":
                    return self._create_task(self._object_body(body), context)
            if path.startswith("/admin/tasks/") and method == "POST":
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[3] == "transition":
                    return self._transition_task(parts[2], self._object_body(body), context)
                if len(parts) == 4 and parts[3] == "postpone":
                    return self._postpone_task(parts[2], self._object_body(body), context)
            if path.startswith("/admin/tasks/"):
                task_id = path.rsplit("/", 1)[-1]
                if method == "PUT":
                    return self._update_task(task_id, self._object_body(body), context)
                if method == "DELETE":
                    self.services.delete_task(DeleteTask(task_id), context)
                    return AdminResponse(200, {"deleted": True, "id": task_id})
            if path == "/admin/notes":
                if method == "GET":
                    return self._list_notes(context)
                if method == "POST":
                    return self._create_note(self._object_body(body), context)
            if path.startswith("/admin/notes/") and method == "PUT":
                return self._update_note(
                    path.rsplit("/", 1)[-1],
                    self._object_body(body),
                    context,
                )
            if path.startswith("/admin/notes/") and method == "DELETE":
                note_id = path.rsplit("/", 1)[-1]
                self.services.delete_note(DeleteNote(note_id), context)
                return AdminResponse(200, {"deleted": True, "id": note_id})
            if path == "/admin/reminders":
                if method == "GET":
                    return self._list_reminders(context)
                if method == "POST":
                    return self._create_reminder(self._object_body(body), context)
            if path.startswith("/admin/reminders/") and method == "DELETE":
                if not context.confirmed:
                    raise ConfirmationRequired(
                        "reminder cancellation requires confirmation"
                    )
                return self._cancel_reminder(
                    path.rsplit("/", 1)[-1],
                    context,
                )
            if method == "GET" and path == "/admin/outbox":
                return AdminResponse(
                    200,
                    self.reads.outbox(
                        principal.user_id,
                        limit=self._query_limit(query),
                    ),
                )
            if method == "GET" and path == "/admin/audit":
                return AdminResponse(
                    200,
                    self.reads.audit(
                        principal.user_id,
                        limit=self._query_limit(query),
                    ),
                )
            if method == "GET" and path == "/admin/llm-usage":
                return AdminResponse(
                    200,
                    self.reads.llm_calls(
                        principal.user_id,
                        limit=self._query_limit(query),
                    ),
                )
            return self._error(404, "not_found", "route was not found")
        except Exception as exc:
            return self._exception_response(exc)

    def _admin_context(self, principal: Any, *, confirmed: bool) -> CommandContext:
        shared_owners = (
            self.channel_routes.shared_owners_for_member(principal.user_id)
            if self.channel_routes is not None
            else ()
        )
        roles: set[str] = set()
        if shared_owners:
            roles.add("shared_workspace_member")
        if self.users.has_role(principal.user_id, "project_admin"):
            roles.add("project_admin")
        return CommandContext(
            actor_user_id=principal.user_id,
            roles=frozenset(roles),
            readable_shared_owner_user_ids=shared_owners,
            authentication=principal.authentication,
            request_channel=RequestChannel.ADMIN_WEB,
            confirmed=confirmed,
            now=self.clock.now(),
        )

    def _workspace_records(
        self,
        context: CommandContext,
    ) -> list[tuple[str, dict[str, object]]]:
        records: list[tuple[str, dict[str, object]]] = [
            (
                context.actor_user_id,
                {"id": "private", "kind": "private", "label": "私人空间"},
            )
        ]
        if self.channel_routes is None:
            return records
        readable = set(context.readable_shared_owner_user_ids)
        routes = sorted(
            (
                route
                for route in self.channel_routes.list()
                if route.commands_enabled
                and route.group_mode is GroupMode.INTERNAL
                and route.shared_owner_user_id in readable
                and context.actor_user_id in route.member_user_ids
            ),
            key=lambda route: (route.channel, route.channel_account, route.opaque_ref),
        )
        seen: set[str] = set()
        for route in routes:
            owner = route.shared_owner_user_id
            if owner is None or owner in seen:
                continue
            seen.add(owner)
            records.append(
                (
                    owner,
                    {
                        "id": f"group:{route.opaque_ref}",
                        "kind": "group",
                        "label": f"内部群 {len(records)}",
                        "channel": route.channel,
                    },
                )
            )
        return records

    def _workspace_for_owner(
        self,
        context: CommandContext,
        owner_user_id: str,
    ) -> dict[str, object]:
        for owner, workspace in self._workspace_records(context):
            if owner == owner_user_id:
                return workspace
        raise PermissionDenied("workspace is not readable by the current administrator")

    @staticmethod
    def _read_owner_ids(context: CommandContext) -> tuple[str, ...]:
        return (context.actor_user_id, *context.readable_shared_owner_user_ids)

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope.get("type") != "http":
            return
        body = bytearray()
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > MAX_BODY_BYTES:
                response = self._error(413, "body_too_large", "request body is too large")
                break
            if not message.get("more_body", False):
                headers = {
                    key.decode("latin-1"): value.decode("latin-1")
                    for key, value in scope.get("headers", ())
                }
                query = scope.get("query_string", b"").decode("ascii")
                target = str(scope.get("path", "/"))
                if query:
                    target = f"{target}?{query}"
                response = self.dispatch(
                    str(scope.get("method", "GET")),
                    target,
                    headers=headers,
                    body=bytes(body),
                )
                break
        payload = json.dumps(
            response.body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        response_headers = [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
            *[(key.encode("latin-1"), value.encode("latin-1")) for key, value in response.headers],
        ]
        await send(
            {
                "type": "http.response.start",
                "status": response.status,
                "headers": response_headers,
            }
        )
        await send({"type": "http.response.body", "body": payload})

    def _authenticate(self, headers: Mapping[str, str]):
        value = headers.get("authorization", "")
        scheme, _, token = value.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise PermissionDenied("a valid admin session is required")
        principal = self.sessions.authenticate(token, now=self.clock.now())
        if principal is None:
            raise PermissionDenied("a valid admin session is required")
        return principal

    def _login(self, data: dict[str, object]) -> AdminResponse:
        if self.credentials is None:
            return self._error(503, "authentication_unavailable", "login is unavailable")
        self._fields(data, required={"user_id", "password"})
        user_id = self._string(data, "user_id", maximum=160)
        submitted_password = self._string(data, "password", maximum=1024)
        if not self.credentials.verify(
            user_id,
            submitted_password,
            now=self.clock.now(),
        ):
            raise PermissionDenied("invalid admin credentials")
        token = self.sessions.create(
            user_id=user_id,
            authentication=AuthenticationStrength.PASSWORD,
            now=self.clock.now(),
        )
        return AdminResponse(
            201,
            {
                "access_token": token.value,
                "token_type": "Bearer",
                "expires_at": token.expires_at.isoformat(),
            },
        )

    def _begin_passkey_registration(self, user_id: str) -> AdminResponse:
        user = self.users.get(user_id)
        if user is None:
            raise PermissionDenied("admin principal is unavailable")
        result = self._vault_call(
            "passkey_begin_registration",
            {
                "user_id": user.id,
                "user_name": user.id,
                "display_name": user.display_name,
            },
        )
        options = result.get("options")
        if not isinstance(options, dict):
            raise RuntimeError("vault returned invalid Passkey options")
        return AdminResponse(200, {"publicKey": options})

    def _finish_passkey_registration(
        self,
        user_id: str,
        data: dict[str, object],
    ) -> AdminResponse:
        self._fields(data, required={"credential"})
        credential = data.get("credential")
        if not isinstance(credential, dict):
            raise ValidationError("credential must be an object")
        self._vault_call(
            "passkey_finish_registration",
            {"user_id": user_id, "credential": credential},
        )
        return AdminResponse(201, {"registered": True})

    def _begin_passkey_authentication(self, user_id: str) -> AdminResponse:
        result = self._vault_call(
            "passkey_begin_authentication",
            {"user_id": user_id},
        )
        options = result.get("options")
        if not isinstance(options, dict):
            raise RuntimeError("vault returned invalid Passkey options")
        return AdminResponse(200, {"publicKey": options})

    def _finish_passkey_authentication(
        self,
        user_id: str,
        data: dict[str, object],
    ) -> AdminResponse:
        self._fields(data, required={"credential"})
        credential = data.get("credential")
        if not isinstance(credential, dict):
            raise ValidationError("credential must be an object")
        result = self._vault_call(
            "passkey_finish_authentication",
            {"user_id": user_id, "credential": credential},
        )
        if str(result.get("user_id") or "") != user_id:
            raise PermissionDenied("Passkey identity mismatch")
        try:
            expires_at = datetime.fromisoformat(str(result["expires_at"]))
        except (KeyError, ValueError) as exc:
            raise RuntimeError("vault returned invalid Passkey proof") from exc
        now = self.clock.now()
        lifetime = expires_at - now
        if lifetime.total_seconds() <= 0 or lifetime.total_seconds() > 600:
            raise PermissionDenied("Passkey proof is expired")
        token = self.sessions.create(
            user_id=user_id,
            authentication=AuthenticationStrength.STEP_UP,
            now=now,
            lifetime=lifetime,
        )
        return AdminResponse(
            201,
            {
                "access_token": token.value,
                "token_type": "Bearer",
                "expires_at": token.expires_at.isoformat(),
                "authentication": "step_up",
            },
        )

    def _vault_call(
        self,
        method: str,
        params: dict[str, object],
    ) -> dict[str, Any]:
        if self.vault_client is None:
            raise RuntimeError("vault client is unavailable")
        try:
            return self.vault_client.call(method, params)
        except PermissionError as exc:
            raise PermissionDenied("vault operation was rejected") from exc

    def _vault_list(self, principal: Any) -> AdminResponse:
        result = self._vault_call(
            "list_owned_metadata",
            {
                "grant": self._issue_vault_grant(
                    principal,
                    secret_id="*",
                    action="list_metadata",
                ),
            },
        )
        items = result.get("items")
        if not isinstance(items, list):
            raise RuntimeError("vault returned invalid metadata")
        return AdminResponse(200, items)

    def _vault_create(
        self,
        principal: Any,
        data: dict[str, object],
        *,
        confirmed: bool,
    ) -> AdminResponse:
        self._require_step_up(principal)
        self._fields(
            data,
            required={"label", "kind", "value"},
            optional={"classification", "policy_override"},
        )
        kind = self._string(data, "kind", maximum=20)
        if kind not in {"machine", "human"}:
            raise ValidationError("vault secret kind is invalid")
        classification = (
            self._string(data, "classification", maximum=40)
            if "classification" in data
            else None
        )
        policy_override = (
            self._string(data, "policy_override", maximum=80)
            if "policy_override" in data
            else None
        )
        if classification == "l4_prohibited":
            if not confirmed:
                raise ConfirmationRequired("L4 storage override requires confirmation")
            if (
                kind != "human"
                or policy_override != "owner_explicit_human_storage"
            ):
                raise ValidationError("L4 storage override is invalid")
        elif policy_override is not None:
            raise ValidationError("vault policy override is invalid")
        secret_id = f"secret_{secrets.token_urlsafe(18)}"
        params: dict[str, object] = {
            "grant": self._issue_vault_grant(
                principal,
                secret_id=secret_id,
                action="create",
            ),
            "secret_id": secret_id,
            "owner_user_id": principal.user_id,
            "label": self._string(data, "label", maximum=200),
            "kind": kind,
            "value": self._string(data, "value", maximum=60_000),
        }
        if classification is not None:
            params["classification"] = classification
        if policy_override is not None:
            params["policy_override"] = policy_override
        result = self._vault_call(
            "create",
            params,
        )
        item = result.get("item")
        if not isinstance(item, dict):
            raise RuntimeError("vault returned invalid metadata")
        return AdminResponse(201, item)

    def _vault_update(
        self,
        principal: Any,
        secret_id: str,
        data: dict[str, object],
    ) -> AdminResponse:
        self._require_step_up(principal)
        self._fields(data, required={"expected_version", "value"})
        result = self._vault_call(
            "update",
            {
                "grant": self._issue_vault_grant(
                    principal,
                    secret_id=secret_id,
                    action="update",
                ),
                "secret_id": secret_id,
                "expected_version": self._integer(
                    data,
                    "expected_version",
                    minimum=1,
                ),
                "value": self._string(data, "value", maximum=60_000),
            },
        )
        item = result.get("item")
        if not isinstance(item, dict):
            raise RuntimeError("vault returned invalid metadata")
        return AdminResponse(200, item)

    def _vault_delete(self, principal: Any, secret_id: str) -> AdminResponse:
        self._require_step_up(principal)
        self._vault_call(
            "delete",
            {
                "grant": self._issue_vault_grant(
                    principal,
                    secret_id=secret_id,
                    action="delete",
                ),
                "secret_id": secret_id,
            },
        )
        return AdminResponse(200, {"deleted": True, "id": secret_id})

    def _vault_reveal(
        self,
        principal: Any,
        secret_id: str,
        data: dict[str, object],
    ) -> AdminResponse:
        self._require_step_up(principal)
        self._fields(data, required=set())
        result = self._vault_call(
            "reveal",
            {
                "grant": self._issue_vault_grant(
                    principal,
                    secret_id=secret_id,
                    action="reveal",
                ),
                "secret_id": secret_id,
            },
        )
        value = result.get("value")
        if not isinstance(value, str):
            raise RuntimeError("vault returned an invalid reveal result")
        return AdminResponse(200, {"value": value, "expires_in_seconds": 60})

    def _vault_use(
        self,
        principal: Any,
        secret_id: str,
        data: dict[str, object],
    ) -> AdminResponse:
        self._fields(data, required={"executor", "request"})
        request = data.get("request")
        if not isinstance(request, dict):
            raise ValidationError("request must be an object")
        return AdminResponse(
            200,
            self._vault_call(
                "use",
                {
                    "grant": self._issue_vault_grant(
                        principal,
                        secret_id=secret_id,
                        action="use",
                    ),
                    "secret_id": secret_id,
                    "executor": self._string(data, "executor", maximum=80),
                    "request": request,
                },
            ),
        )

    def _vault_export(
        self,
        principal: Any,
        secret_id: str,
        data: dict[str, object],
    ) -> AdminResponse:
        self._require_step_up(principal)
        self._fields(data, required={"export_passphrase"})
        result = self._vault_call(
            "export",
            {
                "grant": self._issue_vault_grant(
                    principal,
                    secret_id=secret_id,
                    action="export",
                ),
                "secret_id": secret_id,
                "export_passphrase": self._string(
                    data,
                    "export_passphrase",
                    maximum=1024,
                ),
            },
        )
        encrypted = result.get("encrypted_export")
        if not isinstance(encrypted, str):
            raise RuntimeError("vault returned an invalid export")
        return AdminResponse(200, {"encrypted_export": encrypted})

    def _vault_grant_acl(
        self,
        principal: Any,
        secret_id: str,
        data: dict[str, object],
    ) -> AdminResponse:
        self._require_step_up(principal)
        self._fields(data, required={"subject_user_id", "action"})
        action = self._string(data, "action", maximum=40)
        allowed = {
            "list_metadata",
            "use",
            "reveal",
            "update",
            "delete",
            "export",
            "grant",
            "rotate",
        }
        if action not in allowed:
            raise ValidationError("vault ACL action is invalid")
        subject = self._string(data, "subject_user_id", maximum=160)
        self._vault_call(
            "grant",
            {
                "grant": self._issue_vault_grant(
                    principal,
                    secret_id=secret_id,
                    action="grant",
                ),
                "secret_id": secret_id,
                "subject": subject,
                "action": action,
            },
        )
        return AdminResponse(
            201,
            {"granted": True, "subject_user_id": subject, "action": action},
        )

    def _issue_vault_grant(
        self,
        principal: Any,
        *,
        secret_id: str,
        action: str,
    ) -> str:
        if self.grant_issuer is None:
            raise RuntimeError("vault grant issuer is unavailable")
        return self.grant_issuer.issue(
            subject=principal.user_id,
            secret_id=secret_id,
            action=action,
            audience="zhixu-vault",
            expires_at=self.clock.now() + timedelta(seconds=60),
            authentication=principal.authentication.name.lower(),
        )

    @staticmethod
    def _require_step_up(principal: Any) -> None:
        if principal.authentication < AuthenticationStrength.STEP_UP:
            raise PermissionDenied("current Passkey step-up is required")

    def _issue_identity_challenge(
        self,
        data: dict[str, object],
        user_id: str,
    ) -> AdminResponse:
        if self.outbox is None:
            return self._error(
                503,
                "delivery_unavailable",
                "identity verification delivery is unavailable",
            )
        self._fields(
            data,
            required={"channel", "channel_account"},
            optional={"external_subject", "opaque_ref"},
        )
        channel = self._string(data, "channel", maximum=40)
        account = self._string(data, "channel_account", maximum=160)
        registered = {
            (item.channel, item.channel_account): item for item in self.channels.describe()
        }
        descriptor = registered.get((channel, account))
        if descriptor is None:
            raise ValidationError("channel account is not registered")
        target_kind = self.outbound_target_kinds.get((channel, account))
        if target_kind is not None:
            if "opaque_ref" in data:
                raise ValidationError("outbound identity binding requires an external subject")
            subject = self._string(data, "external_subject", maximum=512)
            if self.outbound_targets is None:
                raise RuntimeError("outbound target registry is unavailable")
            opaque = self.outbound_targets.register(
                channel=channel,
                channel_account=account,
                kind=target_kind,
                target=subject,
                now=self.clock.now(),
            )
        elif channel == "qq" and descriptor.mode == "conversational":
            if "external_subject" in data:
                raise ValidationError("QQ identity binding requires an observed opaque route")
            if self.channel_routes is None:
                raise RuntimeError("channel route registry is unavailable")
            opaque = self._string(data, "opaque_ref", maximum=160)
            route = self.channel_routes.get(channel, account, opaque)
            if route is None or route.kind != "private":
                raise ValidationError("QQ identity binding requires an observed private route")
            # The raw QQ OpenID remains exclusively in the QQ process database.
            # Admission uses the stable opaque reference, so the ordinary
            # application only needs an encrypted copy of that same reference.
            subject = opaque
        else:
            raise ValidationError("channel account cannot deliver identity verification")
        context = f"external-identity:{channel}:{account}:{opaque}"
        encrypted = EncryptedIdentifier(self.field_cipher.encrypt(subject, context=context))
        challenge = self.identity_links.issue(
            user_id=user_id,
            channel=channel,
            channel_account=account,
            opaque_ref=opaque,
            encrypted_subject=encrypted,
            now=self.clock.now(),
        )
        self.outbox.enqueue(
            delivery_id=f"out_{secrets.token_urlsafe(18)}",
            idempotency_key=challenge.id,
            owner_user_id=user_id,
            message=OutboundMessage(
                channel,
                account,
                opaque,
                MessageKind.TEXT,
                f"知序身份绑定验证码：{challenge.code}。请勿转发。",
            ),
            now=self.clock.now(),
            actor_user_id=user_id,
        )
        return AdminResponse(
            201,
            {
                "challenge_id": challenge.id,
                "expires_at": challenge.expires_at.isoformat(),
                "opaque_ref": opaque,
                "delivery": "queued",
            },
        )

    def _update_channel_route(
        self,
        data: dict[str, object],
        context: CommandContext,
    ) -> AdminResponse:
        assert self.channel_routes is not None
        self._fields(
            data,
            required={
                "channel",
                "channel_account",
                "opaque_ref",
                "commands_enabled",
            },
            optional={"group_mode", "member_user_ids"},
        )
        opaque_ref = self._string(data, "opaque_ref", maximum=160)
        self.policy.require(
            context,
            Action.UPDATE,
            ResourceRef(
                "channel_route",
                opaque_ref,
                context.actor_user_id,
            ),
        )
        changed = self.channel_routes.set_commands_enabled(
            channel=self._string(data, "channel", maximum=40),
            channel_account=self._string(data, "channel_account", maximum=160),
            opaque_ref=opaque_ref,
            enabled=self._boolean(data, "commands_enabled", default=False),
            actor_user_id=context.actor_user_id,
            now=self.clock.now(),
            group_mode=(
                GroupMode(
                    self._string(data, "group_mode", maximum=40)
                )
                if "group_mode" in data
                else None
            ),
            member_user_ids=self._string_tuple(data, "member_user_ids", maximum=160),
        )
        if not changed:
            raise NotFoundError("channel route was not found")
        return AdminResponse(200, {"updated": True, "opaque_ref": opaque_ref})

    def _consume_identity_challenge(
        self,
        data: dict[str, object],
        context: CommandContext,
    ) -> AdminResponse:
        self._fields(data, required={"challenge_id", "verification_code"})
        identity_id = random_id("identity")
        authorization = self.policy.require(
            context,
            Action.CREATE,
            ResourceRef(
                "external_identity",
                identity_id,
                context.actor_user_id,
            ),
        )
        identity = self.identity_links.consume(
            challenge_id=self._string(data, "challenge_id", maximum=128),
            code=self._string(data, "verification_code", maximum=32),
            identity_id=identity_id,
            authorization=authorization,
        )
        return AdminResponse(
            201,
            {
                "id": identity.id,
                "channel": identity.channel,
                "channel_account": identity.channel_account,
                "opaque_ref": identity.opaque_ref,
                "created_at": identity.created_at.isoformat(),
            },
        )

    def _unbind_identity(
        self,
        identity_id: str,
        context: CommandContext,
    ) -> AdminResponse:
        identity = next(
            (
                item
                for item in self.users.list_identities(context.actor_user_id)
                if item.id == identity_id
            ),
            None,
        )
        if identity is None:
            raise NotFoundError("identity was not found")
        authorization = self.policy.require(
            context,
            Action.DELETE,
            ResourceRef(
                "external_identity",
                identity.id,
                identity.user_id,
                DataClassification.PERSONAL,
            ),
        )
        deleted = self.users.unbind_identity(identity_id, authorization)
        return AdminResponse(200, {"deleted": deleted, "id": identity_id})

    def _list_acl(
        self,
        query: Mapping[str, list[str]],
        context: CommandContext,
    ) -> AdminResponse:
        resource = self.reads.resource_ref(
            kind=self._query_value(query, "resource_kind"),
            resource_id=self._query_value(query, "resource_id"),
            owner_user_id=context.actor_user_id,
        )
        self.policy.require(context, Action.LIST_METADATA, resource)
        return AdminResponse(
            200,
            self.reads.grants(
                owner_user_id=context.actor_user_id,
                resource=resource,
            ),
        )

    def _change_acl(
        self,
        data: dict[str, object],
        context: CommandContext,
        *,
        revoke: bool,
    ) -> AdminResponse:
        self._fields(
            data,
            required={"resource_kind", "resource_id", "subject_user_id", "action"},
        )
        resource = self.reads.resource_ref(
            kind=self._string(data, "resource_kind", maximum=80),
            resource_id=self._string(data, "resource_id", maximum=160),
            owner_user_id=context.actor_user_id,
        )
        action = acl_action(self._string(data, "action", maximum=40))
        subject = self._string(data, "subject_user_id", maximum=160)
        authorization = self.policy.require(context, Action.GRANT, resource)
        if revoke:
            changed = self.grants.revoke(
                subject_user_id=subject,
                action=action,
                authorization=authorization,
            )
            return AdminResponse(200, {"revoked": changed})
        self.grants.grant(
            subject_user_id=subject,
            action=action,
            authorization=authorization,
        )
        return AdminResponse(201, {"granted": True})

    def _list_agenda(self, context: CommandContext) -> AdminResponse:
        items = [
            item
            for owner_user_id in self._read_owner_ids(context)
            for item in self.services.agenda.list_for_owner(owner_user_id)
        ]
        for item in items:
            self.policy.require(
                context,
                Action.READ,
                ResourceRef("agenda", item.id, item.owner_user_id, item.classification),
            )
        items.sort(key=lambda item: (item.start_at, item.id))
        return AdminResponse(
            200,
            [self._agenda_json(item, context) for item in items],
        )

    def _list_important_days(self, context: CommandContext) -> AdminResponse:
        items = self.services.query_bus().execute(ListAnniversaries(), context)
        return AdminResponse(
            200,
            [
                self._important_day_json(
                    item,
                    context,
                    now=context.now or self.clock.now(),
                )
                for item in items
            ],
        )

    def _create_important_day(
        self,
        data: dict[str, object],
        context: CommandContext,
    ) -> AdminResponse:
        self._fields(
            data,
            required={"title", "anchor_date", "timezone", "kind", "calendar"},
            optional={
                "lunar_month",
                "lunar_day",
                "lunar_leap",
                "advance_days",
                "classification",
                "private",
            },
        )
        try:
            kind = ImportantDayKind(self._string(data, "kind", maximum=40))
            calendar = CalendarSystem(self._string(data, "calendar", maximum=40))
        except ValueError as exc:
            raise ValidationError("invalid important day kind or calendar") from exc
        item = self.services.create_anniversary(
            CreateAnniversary(
                title=self._string(data, "title", maximum=500),
                anchor_date=self._date(data, "anchor_date"),
                timezone=self._string(data, "timezone", maximum=80),
                kind=kind,
                calendar=calendar,
                lunar_month=self._nullable_integer(
                    data,
                    "lunar_month",
                    minimum=1,
                    maximum=12,
                ),
                lunar_day=self._nullable_integer(
                    data,
                    "lunar_day",
                    minimum=1,
                    maximum=30,
                ),
                lunar_leap=self._boolean(data, "lunar_leap", default=False),
                advance_days=self._integer_tuple(
                    data,
                    "advance_days",
                    minimum=1,
                    maximum=366,
                    maximum_items=8,
                ),
                classification=self._classification(data),
                private=self._boolean(data, "private", default=True),
                allow_duplicate=context.confirmed,
            ),
            context,
        )
        return AdminResponse(
            201,
            self._important_day_json(
                item,
                context,
                now=context.now or self.clock.now(),
            ),
        )

    def _create_agenda(
        self,
        data: dict[str, object],
        context: CommandContext,
    ) -> AdminResponse:
        self._fields(
            data,
            required={"title", "start_at", "end_at", "timezone"},
            optional={
                "description",
                "all_day",
                "recurrence_rule",
                "classification",
            },
        )
        item = self.services.create_agenda(
            CreateAgenda(
                title=self._string(data, "title", maximum=500),
                start_at=self._datetime(data, "start_at"),
                end_at=self._datetime(data, "end_at"),
                timezone=self._string(data, "timezone", maximum=80),
                description=self._optional_string(data, "description", maximum=10_000),
                all_day=self._boolean(data, "all_day", default=False),
                recurrence_rule=self._nullable_string(
                    data,
                    "recurrence_rule",
                    maximum=1_000,
                ),
                classification=self._classification(data),
            ),
            context,
        )
        return AdminResponse(201, self._agenda_json(item, context))

    def _update_agenda(
        self,
        item_id: str,
        data: dict[str, object],
        context: CommandContext,
    ) -> AdminResponse:
        self._fields(
            data,
            required={"expected_version", "title", "start_at", "end_at", "timezone"},
            optional={
                "description",
                "all_day",
                "recurrence_rule",
                "classification",
            },
        )
        item = self.services.update_agenda(
            UpdateAgenda(
                item_id=item_id,
                expected_version=self._integer(data, "expected_version", minimum=1),
                title=self._string(data, "title", maximum=500),
                start_at=self._datetime(data, "start_at"),
                end_at=self._datetime(data, "end_at"),
                timezone=self._string(data, "timezone", maximum=80),
                description=self._optional_string(data, "description", maximum=10_000),
                all_day=self._boolean(data, "all_day", default=False),
                recurrence_rule=self._nullable_string(
                    data,
                    "recurrence_rule",
                    maximum=1_000,
                ),
                classification=self._classification(data),
            ),
            context,
        )
        return AdminResponse(200, self._agenda_json(item, context))

    def _set_agenda_exception(
        self,
        item_id: str,
        data: dict[str, object],
        context: CommandContext,
    ) -> AdminResponse:
        self._fields(
            data,
            required={"occurrence_at", "action"},
            optional={"replacement_start", "replacement_end"},
        )
        try:
            action = ExceptionAction(self._string(data, "action", maximum=20))
        except ValueError as exc:
            raise ValidationError("invalid recurrence exception action") from exc
        replacement_start = self._nullable_datetime(data, "replacement_start")
        replacement_end = self._nullable_datetime(data, "replacement_end")
        self.services.set_agenda_exception(
            SetAgendaException(
                item_id=item_id,
                occurrence_at=self._datetime(data, "occurrence_at"),
                action=action,
                replacement_start=replacement_start,
                replacement_end=replacement_end,
            ),
            context,
        )
        return AdminResponse(
            201,
            {
                "agenda_item_id": item_id,
                "occurrence_at": self._datetime(data, "occurrence_at").isoformat(),
                "action": action.value,
                "replacement_start": (
                    replacement_start.isoformat() if replacement_start else None
                ),
                "replacement_end": (
                    replacement_end.isoformat() if replacement_end else None
                ),
            },
        )

    def _list_tasks(self, context: CommandContext) -> AdminResponse:
        items = [
            item
            for owner_user_id in self._read_owner_ids(context)
            for item in self.services.tasks.list_for_owner(owner_user_id)
        ]
        for item in items:
            self.policy.require(
                context,
                Action.READ,
                ResourceRef("task", item.id, item.owner_user_id, item.classification),
            )
        return AdminResponse(
            200,
            [self._task_json(item, context) for item in items],
        )

    def _create_task(
        self,
        data: dict[str, object],
        context: CommandContext,
    ) -> AdminResponse:
        self._fields(
            data,
            required={"title"},
            optional={"description", "priority", "due_at", "classification"},
        )
        task = self.services.create_task(
            CreateTask(
                title=self._string(data, "title", maximum=500),
                description=self._optional_string(data, "description", maximum=10_000),
                priority=self._integer(data, "priority", minimum=0, maximum=4, default=0),
                due_at=self._nullable_datetime(data, "due_at"),
                classification=self._classification(data),
            ),
            context,
        )
        return AdminResponse(201, self._task_json(task, context))

    def _transition_task(
        self,
        task_id: str,
        data: dict[str, object],
        context: CommandContext,
    ) -> AdminResponse:
        self._fields(data, required={"expected_version", "status"})
        try:
            status = TaskStatus(self._string(data, "status", maximum=40))
        except ValueError as exc:
            raise ValidationError("invalid task status") from exc
        task = self.services.transition_task(
            TransitionTask(
                task_id,
                self._integer(data, "expected_version", minimum=1),
                status,
            ),
            context,
        )
        return AdminResponse(200, self._task_json(task, context))

    def _update_task(
        self,
        task_id: str,
        data: dict[str, object],
        context: CommandContext,
    ) -> AdminResponse:
        self._fields(
            data,
            required={"expected_version", "title"},
            optional={"description", "priority", "due_at", "classification"},
        )
        task = self.services.update_task(
            UpdateTask(
                task_id=task_id,
                expected_version=self._integer(data, "expected_version", minimum=1),
                title=self._string(data, "title", maximum=500),
                description=self._optional_string(data, "description", maximum=10_000),
                priority=self._integer(data, "priority", minimum=0, maximum=4, default=0),
                due_at=self._nullable_datetime(data, "due_at"),
                classification=self._classification(data),
            ),
            context,
        )
        return AdminResponse(200, self._task_json(task, context))

    def _postpone_task(
        self,
        task_id: str,
        data: dict[str, object],
        context: CommandContext,
    ) -> AdminResponse:
        self._fields(data, required={"expected_version", "due_at"})
        task = self.services.postpone_task(
            PostponeTask(
                task_id,
                self._integer(data, "expected_version", minimum=1),
                self._datetime(data, "due_at"),
            ),
            context,
        )
        return AdminResponse(200, self._task_json(task, context))

    def _list_notes(self, context: CommandContext) -> AdminResponse:
        notes = [
            note
            for owner_user_id in self._read_owner_ids(context)
            for note in self.services.notes.list_for_owner(owner_user_id)
        ]
        for note in notes:
            self.policy.require(
                context,
                Action.READ,
                ResourceRef("note", note.id, note.owner_user_id, note.classification),
            )
        return AdminResponse(
            200,
            [self._note_json(note, context) for note in notes],
        )

    def _create_note(
        self,
        data: dict[str, object],
        context: CommandContext,
    ) -> AdminResponse:
        self._fields(
            data,
            required={"title", "body"},
            optional={"tags", "attachments", "classification"},
        )
        note = self.services.create_note(
            CreateNote(
                title=self._string(data, "title", maximum=500, allow_empty=True),
                body=self._string(data, "body", maximum=200_000, allow_empty=True),
                tags=self._tags(data),
                attachments=self._attachments(data),
                classification=self._classification(data),
            ),
            context,
        )
        return AdminResponse(201, self._note_json(note, context))

    def _update_note(
        self,
        note_id: str,
        data: dict[str, object],
        context: CommandContext,
    ) -> AdminResponse:
        self._fields(
            data,
            required={"expected_version", "title", "body"},
            optional={"tags", "attachments", "classification"},
        )
        note = self.services.update_note(
            UpdateNote(
                note_id=note_id,
                expected_version=self._integer(data, "expected_version", minimum=1),
                title=self._string(data, "title", maximum=500, allow_empty=True),
                body=self._string(data, "body", maximum=200_000, allow_empty=True),
                tags=self._tags(data),
                attachments=self._attachments(data),
                classification=self._classification(data),
            ),
            context,
        )
        return AdminResponse(200, self._note_json(note, context))

    def _list_reminders(self, context: CommandContext) -> AdminResponse:
        reminders = self.services.query_bus().execute(
            ListReminders(include_inactive=True),
            context,
        )
        return AdminResponse(
            200,
            [self._reminder_json(reminder, context) for reminder in reminders],
        )

    def _create_reminder(
        self,
        data: dict[str, object],
        context: CommandContext,
    ) -> AdminResponse:
        self._fields(
            data,
            required={"title", "fire_at", "target_ref"},
            optional={
                "missed_policy",
                "classification",
                "related_kind",
                "related_id",
            },
        )
        target_ref = self._string(data, "target_ref", maximum=160)
        allowed_targets = {
            identity.opaque_ref
            for identity in self.users.list_identities(context.actor_user_id)
        }
        if target_ref not in allowed_targets:
            raise PermissionDenied("reminder target is not bound to the current user")
        fire_at = self._datetime(data, "fire_at")
        if fire_at <= self.clock.now():
            raise ValidationError("reminder time must be in the future")
        try:
            missed_policy = MissedReminderPolicy(
                self._optional_string(data, "missed_policy", maximum=20)
                or MissedReminderPolicy.FIRE.value
            )
        except ValueError as exc:
            raise ValidationError("invalid missed reminder policy") from exc
        reminder = self.services.create_reminder(
            CreateReminder(
                title=self._string(data, "title", maximum=500),
                fire_at=fire_at,
                target_ref=target_ref,
                missed_policy=missed_policy,
                classification=self._classification(data),
                related_kind=self._nullable_string(
                    data,
                    "related_kind",
                    maximum=80,
                ),
                related_id=self._nullable_string(
                    data,
                    "related_id",
                    maximum=160,
                ),
            ),
            context,
        )
        return AdminResponse(201, self._reminder_json(reminder, context))

    def _cancel_reminder(
        self,
        reminder_id: str,
        context: CommandContext,
    ) -> AdminResponse:
        reminder = self.services.cancel_reminder(
            CancelReminder(reminder_id),
            context,
        )
        return AdminResponse(200, self._reminder_json(reminder, context))

    def _agenda_json(
        self,
        item: Any,
        context: CommandContext,
    ) -> dict[str, object]:
        return {
            "id": item.id,
            "title": item.title,
            "description": item.description,
            "start_at": item.start_at.isoformat(),
            "end_at": item.end_at.isoformat(),
            "timezone": item.timezone,
            "all_day": item.all_day,
            "classification": int(item.classification),
            "recurrence_rule": item.recurrence.value if item.recurrence else None,
            "version": item.version,
            "workspace": self._workspace_for_owner(context, item.owner_user_id),
        }

    def _important_day_json(
        self,
        item: Any,
        context: CommandContext,
        *,
        now: datetime,
    ) -> dict[str, object]:
        local_today = now.astimezone(ZoneInfo(item.timezone)).date()
        occurrence = item.next_occurrence(local_today)
        return {
            "id": item.id,
            "title": item.title,
            "kind": item.kind.value,
            "calendar": item.calendar.value,
            "anchor_date": item.anchor_date.isoformat(),
            "lunar_month": item.lunar_month,
            "lunar_day": item.lunar_day,
            "lunar_leap": item.lunar_leap,
            "timezone": item.timezone,
            "advance_days": list(item.advance_days),
            "next_occurrence": occurrence.isoformat() if occurrence else None,
            "classification": int(item.classification),
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "workspace": self._workspace_for_owner(context, item.owner_user_id),
        }

    def _task_json(
        self,
        item: Any,
        context: CommandContext,
    ) -> dict[str, object]:
        return {
            "id": item.id,
            "title": item.title,
            "description": item.description,
            "status": item.status.value,
            "priority": item.priority,
            "due_at": item.due_at.isoformat() if item.due_at else None,
            "classification": int(item.classification),
            "version": item.version,
            "workspace": self._workspace_for_owner(context, item.owner_user_id),
        }

    def _note_json(
        self,
        item: Any,
        context: CommandContext,
    ) -> dict[str, object]:
        return {
            "id": item.id,
            "title": item.title,
            "body": item.body,
            "category_path": list(item.category_path),
            "content_blocks": [
                {
                    "id": block.id,
                    "name": block.name,
                    "body": block.body,
                    "fields": [
                        {"name": field.name, "value": field.value}
                        for field in block.fields
                    ],
                }
                for block in item.content_blocks
            ],
            "tags": list(item.tags),
            "attachments": [
                {
                    "id": attachment.id,
                    "filename": attachment.filename,
                    "media_type": attachment.media_type,
                    "size_bytes": attachment.size_bytes,
                    "content_ref": attachment.content_ref,
                }
                for attachment in item.attachments
            ],
            "classification": int(item.classification),
            "version": item.version,
            "workspace": self._workspace_for_owner(context, item.owner_user_id),
        }

    def _reminder_json(
        self,
        item: Any,
        context: CommandContext,
    ) -> dict[str, object]:
        return {
            "id": item.id,
            "title": item.title,
            "fire_at": item.fire_at.isoformat(),
            "target_ref": item.target_ref,
            "status": item.status.value,
            "missed_policy": item.missed_policy.value,
            "classification": int(item.classification),
            "related_kind": item.related_kind,
            "related_id": item.related_id,
            "version": item.version,
            "workspace": self._workspace_for_owner(context, item.owner_user_id),
        }

    @staticmethod
    def _object_body(body: bytes) -> dict[str, object]:
        if len(body) > MAX_BODY_BYTES:
            raise ValidationError("request body is too large")
        try:
            value = json.loads(body or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("request body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise ValidationError("request body must be a JSON object")
        return value

    @staticmethod
    def _fields(
        data: Mapping[str, object],
        *,
        required: set[str],
        optional: set[str] | None = None,
    ) -> None:
        optional = optional or set()
        missing = required - data.keys()
        extra = data.keys() - required - optional
        if missing:
            raise ValidationError(f"missing field: {sorted(missing)[0]}")
        if extra:
            raise ValidationError(f"unknown field: {sorted(extra)[0]}")

    @staticmethod
    def _string_tuple(
        data: Mapping[str, object],
        key: str,
        *,
        maximum: int,
    ) -> tuple[str, ...] | None:
        if key not in data:
            return None
        value = data[key]
        if not isinstance(value, list):
            raise ValidationError(f"{key} must be a list")
        result: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip() or len(item) > maximum:
                raise ValidationError(f"{key} contains an invalid value")
            result.append(item)
        if len(result) != len(set(result)):
            raise ValidationError(f"{key} must not contain duplicates")
        return tuple(result)

    @staticmethod
    def _string(
        data: Mapping[str, object],
        key: str,
        *,
        maximum: int,
        allow_empty: bool = False,
    ) -> str:
        value = data.get(key)
        if not isinstance(value, str):
            raise ValidationError(f"{key} must be a string")
        if (not allow_empty and not value.strip()) or len(value) > maximum:
            raise ValidationError(f"{key} has an invalid length")
        return value

    @classmethod
    def _optional_string(
        cls,
        data: Mapping[str, object],
        key: str,
        *,
        maximum: int,
    ) -> str:
        if key not in data:
            return ""
        return cls._string(data, key, maximum=maximum, allow_empty=True)

    @classmethod
    def _nullable_string(
        cls,
        data: Mapping[str, object],
        key: str,
        *,
        maximum: int,
    ) -> str | None:
        if key not in data or data[key] is None:
            return None
        return cls._string(data, key, maximum=maximum)

    @staticmethod
    def _boolean(
        data: Mapping[str, object],
        key: str,
        *,
        default: bool,
    ) -> bool:
        if key not in data:
            return default
        value = data[key]
        if not isinstance(value, bool):
            raise ValidationError(f"{key} must be a boolean")
        return value

    @staticmethod
    def _integer(
        data: Mapping[str, object],
        key: str,
        *,
        minimum: int,
        maximum: int | None = None,
        default: int | None = None,
    ) -> int:
        if key not in data and default is not None:
            return default
        value = data.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(f"{key} must be an integer")
        if value < minimum or (maximum is not None and value > maximum):
            raise ValidationError(f"{key} is outside the allowed range")
        return value

    @classmethod
    def _nullable_integer(
        cls,
        data: Mapping[str, object],
        key: str,
        *,
        minimum: int,
        maximum: int | None = None,
    ) -> int | None:
        if key not in data or data[key] is None:
            return None
        return cls._integer(
            data,
            key,
            minimum=minimum,
            maximum=maximum,
        )

    @staticmethod
    def _integer_tuple(
        data: Mapping[str, object],
        key: str,
        *,
        minimum: int,
        maximum: int,
        maximum_items: int,
    ) -> tuple[int, ...] | None:
        if key not in data:
            return None
        value = data[key]
        if not isinstance(value, list) or len(value) > maximum_items:
            raise ValidationError(f"{key} must be a bounded list")
        result: list[int] = []
        for item in value:
            if (
                not isinstance(item, int)
                or isinstance(item, bool)
                or not minimum <= item <= maximum
            ):
                raise ValidationError(f"{key} contains an invalid value")
            result.append(item)
        if len(result) != len(set(result)):
            raise ValidationError(f"{key} must not contain duplicates")
        return tuple(result)

    @staticmethod
    def _date(data: Mapping[str, object], key: str) -> date:
        value = data.get(key)
        if not isinstance(value, str):
            raise ValidationError(f"{key} must be an ISO date")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError(f"{key} must be an ISO date") from exc

    @staticmethod
    def _datetime(data: Mapping[str, object], key: str) -> datetime:
        value = data.get(key)
        if not isinstance(value, str):
            raise ValidationError(f"{key} must be an ISO datetime")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError(f"{key} must be an ISO datetime") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValidationError(f"{key} must include a timezone")
        return parsed

    @classmethod
    def _nullable_datetime(
        cls,
        data: Mapping[str, object],
        key: str,
    ) -> datetime | None:
        if key not in data or data[key] is None:
            return None
        return cls._datetime(data, key)

    @staticmethod
    def _classification(data: Mapping[str, object]) -> DataClassification:
        value = data.get("classification", int(DataClassification.PERSONAL))
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError("classification must be an integer")
        try:
            return DataClassification(value)
        except ValueError as exc:
            raise ValidationError("invalid classification") from exc

    @staticmethod
    def _tags(data: Mapping[str, object]) -> tuple[str, ...]:
        value = data.get("tags", [])
        if not isinstance(value, list) or any(not isinstance(tag, str) for tag in value):
            raise ValidationError("tags must be a list of strings")
        if len(value) > 100 or any(not tag.strip() or len(tag) > 80 for tag in value):
            raise ValidationError("tags contain an invalid value")
        return tuple(value)

    @classmethod
    def _attachments(
        cls,
        data: Mapping[str, object],
    ) -> tuple[NoteAttachment, ...]:
        value = data.get("attachments", [])
        if not isinstance(value, list) or len(value) > 100:
            raise ValidationError("attachments must be a bounded list")
        attachments: list[NoteAttachment] = []
        for item in value:
            if not isinstance(item, dict):
                raise ValidationError("attachment metadata must be an object")
            cls._fields(
                item,
                required={
                    "id",
                    "filename",
                    "media_type",
                    "size_bytes",
                    "content_ref",
                },
            )
            attachments.append(
                NoteAttachment(
                    id=cls._string(item, "id", maximum=160),
                    filename=cls._string(item, "filename", maximum=500),
                    media_type=cls._string(item, "media_type", maximum=200),
                    size_bytes=cls._integer(
                        item,
                        "size_bytes",
                        minimum=0,
                        maximum=10 * 1024 * 1024 * 1024,
                    ),
                    content_ref=cls._string(item, "content_ref", maximum=500),
                )
            )
        return tuple(attachments)

    @staticmethod
    def _query_value(query: Mapping[str, list[str]], key: str) -> str:
        values = query.get(key, [])
        if len(values) != 1 or not values[0]:
            raise ValidationError(f"query parameter {key} is required exactly once")
        return values[0]

    @classmethod
    def _query_limit(cls, query: Mapping[str, list[str]]) -> int:
        if "limit" not in query:
            return 100
        value = cls._query_value(query, "limit")
        try:
            return int(value)
        except ValueError as exc:
            raise ValidationError("limit must be an integer") from exc

    @staticmethod
    def _error(status: int, code: str, message: str) -> AdminResponse:
        return AdminResponse(status, {"error": {"code": code, "message": message}})

    @classmethod
    def _exception_response(cls, exc: Exception) -> AdminResponse:
        if isinstance(exc, NotFoundError):
            return cls._error(404, exc.code, str(exc))
        if isinstance(exc, (ConcurrencyConflict, ConflictError, InvalidTransition)):
            return cls._error(409, exc.code, str(exc))
        if isinstance(exc, ConfirmationRequired):
            return cls._error(428, exc.code, str(exc))
        if isinstance(exc, PermissionDenied):
            return cls._error(403, exc.code, str(exc))
        if isinstance(exc, (ValidationError, ClassificationNotSupported)):
            return cls._error(422, exc.code, str(exc))
        if isinstance(exc, ZhixuError):
            return cls._error(400, exc.code, str(exc))
        return cls._error(500, "internal_error", "internal server error")


def new_channel_session_id() -> str:
    return f"session_{secrets.token_urlsafe(18)}"
