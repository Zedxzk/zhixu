"""Capability-checked vault service and non-revealing executor path."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

from .crypto import Argon2Parameters, seal
from .grants import CapabilityGrantVerifier, VerifiedGrant
from .policy import (
    L4_HUMAN_STORAGE_OVERRIDE,
    SecretKind,
    VaultAction,
    VaultClassification,
    VaultPolicy,
)
from .storage import SecretMetadata, VaultRepository
from .types import SecretValue


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    ok: bool
    code: str
    data: dict[str, Any]


class MachineSecretExecutor(Protocol):
    def execute(
        self,
        secret: SecretValue,
        request: dict[str, Any],
    ) -> ExecutionResult: ...


class VaultService:
    def __init__(
        self,
        repository: VaultRepository,
        verifier: CapabilityGrantVerifier,
        *,
        policy: VaultPolicy | None = None,
        executors: dict[str, MachineSecretExecutor] | None = None,
    ) -> None:
        self.repository = repository
        self.verifier = verifier
        self.policy = policy or VaultPolicy()
        self.executors = executors or {}

    def create_secret(
        self,
        *,
        secret_id: str,
        owner_user_id: str,
        label: str,
        kind: SecretKind,
        value: SecretValue,
        authentication: str,
        classification: VaultClassification | None = None,
        policy_override: str | None = None,
    ) -> SecretMetadata:
        if (
            not secret_id.strip()
            or len(secret_id) > 160
            or not owner_user_id.strip()
            or len(owner_user_id) > 160
            or not label.strip()
            or len(label) > 200
            or not value.bytes()
        ):
            value.clear()
            raise ValueError("secret metadata or value is invalid")
        if authentication != "step_up":
            value.clear()
            raise PermissionError("secret creation requires step-up authentication")
        selected = classification or (
            VaultClassification.MACHINE_SECRET
            if kind is SecretKind.MACHINE
            else VaultClassification.HUMAN_SECRET
        )
        stored_classification = selected
        stored_override: str | None = None
        if selected is VaultClassification.PROHIBITED:
            if (
                kind is not SecretKind.HUMAN
                or policy_override != L4_HUMAN_STORAGE_OVERRIDE
            ):
                value.clear()
                raise PermissionError("L4 secrets require an explicit owner override")
            stored_classification = VaultClassification.HUMAN_SECRET
            stored_override = policy_override
        elif (
            selected is VaultClassification.L4_HUMAN_OVERRIDE
            or policy_override is not None
        ):
            value.clear()
            raise ValueError("L4 policy override is invalid")
        if (
            kind is SecretKind.MACHINE
            and stored_classification is not VaultClassification.MACHINE_SECRET
        ) or (
            kind is SecretKind.HUMAN
            and stored_classification is not VaultClassification.HUMAN_SECRET
        ):
            value.clear()
            raise ValueError("secret kind and classification do not match")
        try:
            return self.repository.create(
                secret_id=secret_id,
                owner_user_id=owner_user_id,
                label=label,
                kind=kind,
                classification=stored_classification,
                policy_override=stored_override,
                value=value,
                actor=owner_user_id,
            )
        finally:
            value.clear()

    def create_with_grant(
        self,
        token: str,
        *,
        secret_id: str,
        owner_user_id: str,
        label: str,
        kind: SecretKind,
        value: SecretValue,
        classification: VaultClassification | None = None,
        policy_override: str | None = None,
    ) -> SecretMetadata:
        grant: VerifiedGrant | None = None
        try:
            grant = self.verifier.verify_and_consume(
                token,
                secret_id=secret_id,
                action=VaultAction.CREATE.value,
            )
            if grant.subject != owner_user_id or grant.authentication != "step_up":
                raise PermissionError("secret creation grant is invalid")
            return self.create_secret(
                secret_id=secret_id,
                owner_user_id=owner_user_id,
                label=label,
                kind=kind,
                value=value,
                authentication=grant.authentication,
                classification=classification,
                policy_override=policy_override,
            )
        except Exception:
            value.clear()
            self._record_denial(
                actor=grant.subject if grant is not None else "unknown",
                action=VaultAction.CREATE,
                secret_id=secret_id,
            )
            raise

    def list_owned_metadata(self, token: str) -> list[SecretMetadata]:
        grant: VerifiedGrant | None = None
        try:
            grant = self.verifier.verify_and_consume(
                token,
                secret_id="*",
                action=VaultAction.LIST_METADATA.value,
            )
            values = self.repository.list_metadata(grant.subject)
            self._record_success(grant, VaultAction.LIST_METADATA, "*")
            return values
        except Exception:
            self._record_denial(
                actor=grant.subject if grant is not None else "unknown",
                action=VaultAction.LIST_METADATA,
                secret_id="*",
            )
            raise

    def list_metadata(self, token: str, secret_id: str) -> list[SecretMetadata]:
        grant, metadata = self._authorize(token, secret_id, VaultAction.LIST_METADATA)
        self._record_success(grant, VaultAction.LIST_METADATA, secret_id)
        return [metadata]

    def reveal(self, token: str, secret_id: str) -> SecretValue:
        grant, _metadata = self._authorize(token, secret_id, VaultAction.REVEAL)
        value = self.repository.decrypt(secret_id)
        try:
            self._record_success(grant, VaultAction.REVEAL, secret_id)
        except Exception:
            value.clear()
            raise
        return value

    def use(
        self,
        token: str,
        secret_id: str,
        *,
        executor_name: str,
        request: dict[str, Any],
    ) -> ExecutionResult:
        grant, _metadata = self._authorize(token, secret_id, VaultAction.USE)
        executor = self.executors.get(executor_name)
        if executor is None:
            self._record_denial(
                actor=grant.subject,
                action=VaultAction.USE,
                secret_id=secret_id,
                reason_code="executor_unavailable",
            )
            raise KeyError("machine secret executor is unavailable")
        with self.repository.decrypt(secret_id) as secret:
            result = executor.execute(secret, request)
            if not isinstance(result, ExecutionResult):
                raise TypeError("executor returned an unsupported result")
            serialized = json.dumps(
                {"code": result.code, "data": result.data},
                ensure_ascii=False,
                default=lambda _value: "<unsupported>",
            )
            if secret.text() and secret.text() in serialized:
                self._record_denial(
                    actor=grant.subject,
                    action=VaultAction.USE,
                    secret_id=secret_id,
                    reason_code="secret_echo_blocked",
                )
                raise PermissionError("executor attempted to return secret material")
        self._record_success(grant, VaultAction.USE, secret_id)
        return result

    def update(
        self,
        token: str,
        secret_id: str,
        value: SecretValue,
        *,
        expected_version: int,
    ) -> SecretMetadata:
        if not value.bytes():
            value.clear()
            raise ValueError("secret value must not be empty")
        try:
            grant, _metadata = self._authorize(token, secret_id, VaultAction.UPDATE)
            return self.repository.update(
                secret_id,
                value,
                expected_version=expected_version,
                actor=grant.subject,
            )
        finally:
            value.clear()

    def delete(self, token: str, secret_id: str) -> None:
        grant, _metadata = self._authorize(token, secret_id, VaultAction.DELETE)
        self.repository.delete(secret_id, actor=grant.subject)

    def export_encrypted(
        self,
        token: str,
        secret_id: str,
        *,
        export_passphrase: str,
    ) -> bytes:
        grant, _metadata = self._authorize(token, secret_id, VaultAction.EXPORT)
        salt = os.urandom(16)
        export_key = Argon2Parameters().derive(export_passphrase, salt)
        metadata = self.repository.get_metadata(secret_id)
        with self.repository.decrypt(secret_id) as secret:
            payload = json.dumps(
                {
                    "id": metadata.id,
                    "label": metadata.label,
                    "kind": metadata.kind.value,
                    "classification": metadata.classification.value,
                    "value": base64.urlsafe_b64encode(secret.bytes()).decode(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        envelope = {
            "format": "zhixu-secret-export-v1",
            "salt": base64.urlsafe_b64encode(salt).decode(),
            "ciphertext": seal(
                export_key,
                payload,
                context=f"secret-export:{secret_id}",
            ),
        }
        result = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        self._record_success(grant, VaultAction.EXPORT, secret_id)
        return result

    def rotate_keys(
        self,
        token: str,
        secret_id: str,
        *,
        passphrase: str,
    ) -> int:
        grant, _metadata = self._authorize(token, secret_id, VaultAction.ROTATE)
        version = self.repository.keyring.add_key_version(passphrase)
        self.repository.rewrap_data_keys(version, actor=grant.subject)
        return version

    def grant_acl(
        self,
        token: str,
        secret_id: str,
        *,
        subject: str,
        action: VaultAction,
    ) -> None:
        grant, _metadata = self._authorize(token, secret_id, VaultAction.GRANT)
        self.repository.grant(secret_id, subject, action, actor=grant.subject)

    def _authorize(
        self,
        token: str,
        secret_id: str,
        action: VaultAction,
    ) -> tuple[VerifiedGrant, SecretMetadata]:
        grant: VerifiedGrant | None = None
        try:
            grant = self.verifier.verify_and_consume(
                token,
                secret_id=secret_id,
                action=action.value,
            )
            metadata = self.repository.get_metadata(secret_id)
            self.policy.require(
                grant,
                kind=metadata.kind,
                classification=metadata.classification,
                owner_user_id=metadata.owner_user_id,
                acl_allowed=self.repository.has_acl(
                    secret_id,
                    grant.subject,
                    action.value,
                ),
            )
            return grant, metadata
        except Exception:
            self._record_denial(
                actor=grant.subject if grant is not None else "unknown",
                action=action,
                secret_id=secret_id,
            )
            raise

    def _record_success(
        self,
        grant: VerifiedGrant,
        action: VaultAction,
        secret_id: str,
    ) -> None:
        self.repository.record_access(
            actor=grant.subject,
            action=action.value,
            secret_id=secret_id,
            outcome="completed",
        )

    def _record_denial(
        self,
        *,
        actor: str,
        action: VaultAction,
        secret_id: str,
        reason_code: str = "authorization_denied",
    ) -> None:
        try:
            self.repository.record_access(
                actor=actor,
                action=action.value,
                secret_id=secret_id,
                outcome="denied",
                reason_code=reason_code,
            )
        except Exception:
            # Denials remain fail-closed even when a sealed vault cannot derive
            # the audit MAC key. The Unix runtime emits only the exception type.
            return
