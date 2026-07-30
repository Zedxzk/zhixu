"""Bot-scoped, encrypted QQ contact discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from zhixu.adapters.storage.sqlite.database import Database
from zhixu.domain.agenda import require_aware
from zhixu.domain.errors import NotFoundError, ValidationError
from zhixu.security import FieldCipher, OpaqueReferenceFactory


@dataclass(frozen=True, slots=True)
class ResolvedQQTarget:
    channel_account: str
    kind: str
    identifier: str = field(repr=False)
    opaque_ref: str


class QQContactStore:
    def __init__(
        self,
        database: Database,
        cipher: FieldCipher,
        references: OpaqueReferenceFactory,
    ) -> None:
        self.database = database
        self.cipher = cipher
        self.references = references

    def register_account(
        self,
        account_id: str,
        *,
        label: str,
        config_ref: str,
        now: datetime,
    ) -> None:
        require_aware(now, "now")
        if not account_id.strip() or not config_ref.strip():
            raise ValidationError("channel account and config reference are required")
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO channel_accounts(
                    id,channel,label,config_ref,enabled,created_at
                ) VALUES(?,'qq',?,?,1,?)
                ON CONFLICT(id) DO UPDATE SET
                    label=excluded.label,config_ref=excluded.config_ref,enabled=1
                """,
                (account_id, label, config_ref, now.astimezone(UTC).isoformat()),
            )

    def record(
        self,
        *,
        channel_account: str,
        kind: str,
        external_identifier: str,
        now: datetime,
    ) -> str:
        require_aware(now, "now")
        if kind not in {"private", "group", "channel", "actor"}:
            raise ValidationError("unsupported QQ contact kind")
        raw = external_identifier.strip()
        if not raw:
            raise ValidationError("QQ external identifier is required")
        opaque = (
            self.references.create("identity", "qq", channel_account, raw)
            if kind in {"private", "actor"}
            else self.references.create("qqc", channel_account, kind, raw)
        )
        context = f"qq-contact:{channel_account}:{kind}:{opaque}"
        encrypted = self.cipher.encrypt(raw, context=context)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO channel_contacts(
                    id,channel_account_id,opaque_ref,external_target_enc,
                    kind,last_seen_at,commands_enabled
                ) VALUES(?,?,?,?,?,?,0)
                ON CONFLICT(channel_account_id,opaque_ref) DO UPDATE SET
                    external_target_enc=excluded.external_target_enc,
                    kind=excluded.kind,last_seen_at=excluded.last_seen_at
                """,
                (
                    opaque,
                    channel_account,
                    opaque,
                    encrypted,
                    kind,
                    now.astimezone(UTC).isoformat(),
                ),
            )
        return opaque

    def set_commands_enabled(
        self,
        channel_account: str,
        opaque_ref: str,
        *,
        enabled: bool,
    ) -> bool:
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE channel_contacts SET commands_enabled=?
                WHERE channel_account_id=? AND opaque_ref=?
                """,
                (int(enabled), channel_account, opaque_ref),
            ).rowcount
        return changed == 1

    def commands_enabled(self, channel_account: str, opaque_ref: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT commands_enabled FROM channel_contacts
                WHERE channel_account_id=? AND opaque_ref=?
                """,
                (channel_account, opaque_ref),
            ).fetchone()
        return bool(row["commands_enabled"]) if row is not None else False

    def resolve(self, channel_account: str, opaque_ref: str) -> ResolvedQQTarget:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT channel_account_id,kind,opaque_ref,external_target_enc
                FROM channel_contacts
                WHERE channel_account_id=? AND opaque_ref=?
                """,
                (channel_account, opaque_ref),
            ).fetchone()
        if row is None:
            raise NotFoundError("QQ contact was not found")
        kind = str(row["kind"])
        context = f"qq-contact:{channel_account}:{kind}:{opaque_ref}"
        identifier = self.cipher.decrypt(str(row["external_target_enc"]), context=context)
        return ResolvedQQTarget(channel_account, kind, identifier, opaque_ref)
