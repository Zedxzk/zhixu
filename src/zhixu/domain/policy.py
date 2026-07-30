"""Fine-grained authorization independent of HTTP, channels, and storage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum

from .classification import DataClassification, require_ordinary_storage
from .errors import ConfirmationRequired, PermissionDenied, ValidationError


class Action(StrEnum):
    LIST_METADATA = "list_metadata"
    READ = "read"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    USE = "use"
    REVEAL = "reveal"
    EXPORT = "export"
    GRANT = "grant"
    ROTATE = "rotate"


class AuthenticationStrength(IntEnum):
    CHANNEL = 0
    PASSWORD = 1
    MFA = 2
    STEP_UP = 3


class RequestChannel(StrEnum):
    PRIVATE_CHAT = "private_chat"
    GROUP_CHAT = "group_chat"
    ADMIN_WEB = "admin_web"
    SERVICE = "service"


@dataclass(frozen=True, slots=True)
class CommandContext:
    actor_user_id: str
    roles: frozenset[str] = field(default_factory=frozenset)
    authentication: AuthenticationStrength = AuthenticationStrength.CHANNEL
    request_channel: RequestChannel = RequestChannel.PRIVATE_CHAT
    confirmed: bool = False
    now: datetime | None = None

    def __post_init__(self) -> None:
        if not self.actor_user_id.strip():
            raise ValidationError("actor_user_id is required")
        if self.now is not None and self.now.tzinfo is None:
            raise ValidationError("context time must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ResourceRef:
    kind: str
    id: str
    owner_user_id: str
    classification: DataClassification = DataClassification.PERSONAL

    def __post_init__(self) -> None:
        if not self.kind.strip() or not self.id.strip() or not self.owner_user_id.strip():
            raise ValidationError("resource reference fields must not be empty")


@dataclass(frozen=True, slots=True)
class AuthorizedAction:
    """Capability minted by PolicyEngine and required by repository writes."""

    actor_user_id: str
    action: Action
    resource: ResourceRef
    authorized_at: datetime


GrantLookup = Callable[[str, Action, ResourceRef], bool]


class PolicyEngine:
    """Default-deny policy for ordinary L0-L2 application data."""

    def __init__(self, grant_lookup: GrantLookup | None = None) -> None:
        self._grant_lookup = grant_lookup or (lambda _actor, _action, _resource: False)

    def require(
        self,
        context: CommandContext,
        action: Action,
        resource: ResourceRef,
    ) -> AuthorizedAction:
        require_ordinary_storage(resource.classification)

        if action in {Action.USE, Action.REVEAL, Action.EXPORT, Action.ROTATE}:
            raise PermissionDenied(f"{action.value} is unavailable for ordinary storage")

        is_owner = context.actor_user_id == resource.owner_user_id
        has_explicit_grant = self._grant_lookup(context.actor_user_id, action, resource)
        if not is_owner and not has_explicit_grant:
            raise PermissionDenied("the actor has no grant for this resource")

        if (
            resource.classification >= DataClassification.CONFIDENTIAL
            and context.request_channel is RequestChannel.GROUP_CHAT
        ):
            raise PermissionDenied("confidential resources are not available in group chat")

        if (
            resource.classification >= DataClassification.CONFIDENTIAL
            and context.authentication < AuthenticationStrength.STEP_UP
        ):
            raise PermissionDenied("step-up authentication is required")

        if action is Action.GRANT and (
            context.authentication < AuthenticationStrength.STEP_UP
            or context.request_channel is not RequestChannel.ADMIN_WEB
        ):
            raise PermissionDenied("ACL changes require admin step-up authentication")

        if action is Action.DELETE and not context.confirmed:
            raise ConfirmationRequired("delete requires explicit confirmation")

        authorized_at = context.now
        if authorized_at is None:
            raise ValidationError("policy evaluation requires an injected current time")
        return AuthorizedAction(
            actor_user_id=context.actor_user_id,
            action=action,
            resource=resource,
            authorized_at=authorized_at,
        )
