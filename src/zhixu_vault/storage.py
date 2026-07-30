"""Envelope-encrypted secret records and exact ACL storage."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from .audit import AuditEvent, VaultAuditLog
from .crypto import VaultKeyring, open_sealed, seal
from .database import VaultDatabase
from .policy import SecretKind, VaultAction, VaultClassification
from .types import SecretValue


@dataclass(frozen=True, slots=True)
class SecretMetadata:
    id: str
    owner_user_id: str
    label: str
    kind: SecretKind
    classification: VaultClassification
    version: int
    created_at: datetime
    updated_at: datetime


class VaultRepository:
    def __init__(
        self,
        database: VaultDatabase,
        keyring: VaultKeyring,
        now: Callable[[], datetime],
    ) -> None:
        self.database = database
        self.keyring = keyring
        self.now = now
        self.audit = VaultAuditLog(database)

    def record_access(
        self,
        *,
        actor: str,
        action: str,
        secret_id: str,
        outcome: str,
        reason_code: str = "",
    ) -> None:
        """Append a non-secret authorization/access decision to the audit chain."""

        now = self.now().astimezone(UTC)
        with self.database.transaction() as connection:
            self.audit.append(
                connection,
                AuditEvent(
                    now,
                    actor[:160] or "unknown",
                    action[:80],
                    secret_id[:160],
                    outcome[:40],
                    reason_code[:80],
                ),
                audit_key=self.keyring.audit_key(),
            )

    @staticmethod
    def _metadata(row: sqlite3.Row) -> SecretMetadata:
        return SecretMetadata(
            id=str(row["id"]),
            owner_user_id=str(row["owner_user_id"]),
            label=str(row["label"]),
            kind=SecretKind(str(row["secret_kind"])),
            classification=VaultClassification(str(row["classification"])),
            version=int(row["version"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    def create(
        self,
        *,
        secret_id: str,
        owner_user_id: str,
        label: str,
        kind: SecretKind,
        classification: VaultClassification,
        value: SecretValue,
        actor: str,
    ) -> SecretMetadata:
        key_version, master_key = self.keyring.active()
        data_key = os.urandom(32)
        now = self.now().astimezone(UTC)
        ciphertext = seal(
            data_key,
            value.bytes(),
            context=f"secret:{secret_id}:value:1",
        )
        wrapped = seal(
            master_key,
            data_key,
            context=f"secret:{secret_id}:dek:{key_version}",
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO secret_records(
                    id,owner_user_id,label,secret_kind,classification,
                    ciphertext,wrapped_data_key,
                    key_version,version,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,1,?,?)
                """,
                (
                    secret_id,
                    owner_user_id,
                    label,
                    kind.value,
                    classification.value,
                    ciphertext,
                    wrapped,
                    key_version,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            owner_actions = [
                VaultAction.LIST_METADATA,
                VaultAction.UPDATE,
                VaultAction.DELETE,
                VaultAction.EXPORT,
                VaultAction.GRANT,
                VaultAction.ROTATE,
                VaultAction.USE if kind is SecretKind.MACHINE else VaultAction.REVEAL,
            ]
            for action in owner_actions:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO secret_acl(secret_id,subject,action,created_at)
                    VALUES(?,?,?,?)
                    """,
                    (secret_id, owner_user_id, action.value, now.isoformat()),
                )
            self.audit.append(
                connection,
                AuditEvent(now, actor, "create", secret_id, "completed"),
                audit_key=self.keyring.audit_key(),
            )
        return self.get_metadata(secret_id)

    def get_metadata(self, secret_id: str) -> SecretMetadata:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM secret_records WHERE id=?",
                (secret_id,),
            ).fetchone()
        if row is None:
            raise KeyError("secret not found")
        return self._metadata(row)

    def list_metadata(self, owner_user_id: str) -> list[SecretMetadata]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM secret_records WHERE owner_user_id=?
                ORDER BY label,id
                """,
                (owner_user_id,),
            ).fetchall()
        return [self._metadata(row) for row in rows]

    def decrypt(self, secret_id: str) -> SecretValue:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM secret_records WHERE id=?",
                (secret_id,),
            ).fetchone()
        if row is None:
            raise KeyError("secret not found")
        key_version = int(row["key_version"])
        data_key = open_sealed(
            self.keyring.key(key_version),
            str(row["wrapped_data_key"]),
            context=f"secret:{secret_id}:dek:{key_version}",
        )
        plaintext = open_sealed(
            data_key,
            str(row["ciphertext"]),
            context=f"secret:{secret_id}:value:{int(row['version'])}",
        )
        return SecretValue(bytearray(plaintext))

    def update(
        self,
        secret_id: str,
        value: SecretValue,
        *,
        expected_version: int,
        actor: str,
    ) -> SecretMetadata:
        key_version, master_key = self.keyring.active()
        data_key = os.urandom(32)
        new_version = expected_version + 1
        now = self.now().astimezone(UTC)
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE secret_records
                SET ciphertext=?,wrapped_data_key=?,key_version=?,version=?,updated_at=?
                WHERE id=? AND version=?
                """,
                (
                    seal(
                        data_key,
                        value.bytes(),
                        context=f"secret:{secret_id}:value:{new_version}",
                    ),
                    seal(
                        master_key,
                        data_key,
                        context=f"secret:{secret_id}:dek:{key_version}",
                    ),
                    key_version,
                    new_version,
                    now.isoformat(),
                    secret_id,
                    expected_version,
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError("secret version conflict")
            self.audit.append(
                connection,
                AuditEvent(now, actor, "update", secret_id, "completed"),
                audit_key=self.keyring.audit_key(),
            )
        return self.get_metadata(secret_id)

    def delete(self, secret_id: str, *, actor: str) -> None:
        now = self.now().astimezone(UTC)
        with self.database.transaction() as connection:
            changed = connection.execute(
                "DELETE FROM secret_records WHERE id=?",
                (secret_id,),
            ).rowcount
            if changed != 1:
                raise KeyError("secret not found")
            self.audit.append(
                connection,
                AuditEvent(now, actor, "delete", secret_id, "completed"),
                audit_key=self.keyring.audit_key(),
            )

    def grant(
        self,
        secret_id: str,
        subject: str,
        action: VaultAction,
        *,
        actor: str,
    ) -> None:
        now = self.now().astimezone(UTC)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO secret_acl(secret_id,subject,action,created_at)
                VALUES(?,?,?,?)
                """,
                (secret_id, subject, action.value, now.isoformat()),
            )
            self.audit.append(
                connection,
                AuditEvent(now, actor, "grant", secret_id, "completed"),
                audit_key=self.keyring.audit_key(),
            )

    def has_acl(self, secret_id: str, subject: str, action: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM secret_acl
                WHERE secret_id=? AND subject=? AND action=?
                """,
                (secret_id, subject, action),
            ).fetchone()
        return row is not None

    def rewrap_data_keys(self, new_key_version: int, *, actor: str) -> None:
        new_master = self.keyring.key(new_key_version)
        now = self.now().astimezone(UTC)
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT id,key_version,wrapped_data_key FROM secret_records"
            ).fetchall()
            for row in rows:
                secret_id = str(row["id"])
                old_version = int(row["key_version"])
                data_key = open_sealed(
                    self.keyring.key(old_version),
                    str(row["wrapped_data_key"]),
                    context=f"secret:{secret_id}:dek:{old_version}",
                )
                connection.execute(
                    """
                    UPDATE secret_records SET wrapped_data_key=?,key_version=?,updated_at=?
                    WHERE id=?
                    """,
                    (
                        seal(
                            new_master,
                            data_key,
                            context=f"secret:{secret_id}:dek:{new_key_version}",
                        ),
                        new_key_version,
                        now.isoformat(),
                        secret_id,
                    ),
                )
            self.audit.append(
                connection,
                AuditEvent(now, actor, "rotate", "*", "completed"),
                audit_key=self.keyring.audit_key(),
            )
