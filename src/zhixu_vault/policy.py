"""Independent vault actions and human/machine secret policy."""

from __future__ import annotations

from enum import StrEnum

from .grants import VerifiedGrant


class VaultAction(StrEnum):
    LIST_METADATA = "list_metadata"
    CREATE = "create"
    USE = "use"
    REVEAL = "reveal"
    UPDATE = "update"
    DELETE = "delete"
    EXPORT = "export"
    GRANT = "grant"
    ROTATE = "rotate"


class SecretKind(StrEnum):
    MACHINE = "machine"
    HUMAN = "human"


class VaultClassification(StrEnum):
    MACHINE_SECRET = "l3_machine"  # pragma: allowlist secret
    HUMAN_SECRET = "l3_human"  # pragma: allowlist secret
    L4_HUMAN_OVERRIDE = "l4_human_override"
    PROHIBITED = "l4_prohibited"


L4_HUMAN_STORAGE_OVERRIDE = "owner_explicit_human_storage"


class VaultPolicy:
    def require(
        self,
        grant: VerifiedGrant,
        *,
        kind: SecretKind,
        classification: VaultClassification,
        owner_user_id: str,
        acl_allowed: bool,
    ) -> None:
        try:
            action = VaultAction(grant.action)
        except ValueError as exc:
            raise PermissionError("unknown vault action") from exc
        if not acl_allowed:
            raise PermissionError("vault ACL denied the action")
        if classification is VaultClassification.L4_HUMAN_OVERRIDE:
            if kind is not SecretKind.HUMAN or grant.subject != owner_user_id:
                raise PermissionError("L4 override is restricted to its owner")
            if action in {VaultAction.USE, VaultAction.GRANT}:
                raise PermissionError("L4 override cannot be automated or shared")
        if action is VaultAction.REVEAL:
            if kind is not SecretKind.HUMAN:
                raise PermissionError("machine secrets cannot be revealed")
            if grant.authentication != "step_up":
                raise PermissionError("reveal requires step-up authentication")
        if action is VaultAction.USE and kind is not SecretKind.MACHINE:
            raise PermissionError("human secrets cannot be used by an executor")
        if action in {
            VaultAction.UPDATE,
            VaultAction.DELETE,
            VaultAction.EXPORT,
            VaultAction.GRANT,
            VaultAction.ROTATE,
        } and grant.authentication != "step_up":
            raise PermissionError("high-risk vault action requires step-up authentication")
