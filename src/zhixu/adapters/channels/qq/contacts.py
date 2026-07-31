"""Bot-scoped, encrypted QQ contact discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

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


@dataclass(frozen=True, slots=True)
class ResolvedQQReplyContext:
    field: str
    identifier: str = field(repr=False)


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

    def record_reply_context(
        self,
        *,
        channel_account: str,
        target_ref: str,
        external_context: str,
        context_kind: str,
        now: datetime,
    ) -> str:
        require_aware(now, "now")
        if context_kind not in {"msg_id", "event_id"}:
            raise ValidationError("QQ reply context kind is invalid")
        raw = external_context.strip()
        if not raw or not target_ref.strip():
            raise ValidationError("QQ reply context is incomplete")
        opaque_ref = self.references.create(
            "qqr",
            channel_account,
            context_kind,
            raw,
        )
        encrypted = self.cipher.encrypt(
            raw,
            context=f"qq-reply:{channel_account}:{opaque_ref}",
        )
        cutoff = now - timedelta(minutes=10)
        with self.database.transaction() as connection:
            connection.execute(
                """
                DELETE FROM qq_reply_contexts
                WHERE channel_account=? AND received_at<?
                """,
                (
                    channel_account,
                    cutoff.astimezone(UTC).isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO qq_reply_contexts(
                    channel_account,opaque_ref,target_ref,context_kind,
                    external_context_enc,received_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(channel_account,opaque_ref) DO UPDATE SET
                    target_ref=excluded.target_ref,
                    context_kind=excluded.context_kind,
                    external_context_enc=excluded.external_context_enc,
                    received_at=excluded.received_at
                """,
                (
                    channel_account,
                    opaque_ref,
                    target_ref,
                    context_kind,
                    encrypted,
                    now.astimezone(UTC).isoformat(),
                ),
            )
        return opaque_ref

    def resolve_reply_context(
        self,
        channel_account: str,
        opaque_ref: str,
        *,
        target_ref: str,
        now: datetime | None = None,
    ) -> ResolvedQQReplyContext | None:
        current = now or datetime.now(UTC)
        cutoff = current - timedelta(minutes=10)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT context_kind,external_context_enc,received_at
                FROM qq_reply_contexts
                WHERE channel_account=? AND opaque_ref=? AND target_ref=?
                """,
                (channel_account, opaque_ref, target_ref),
            ).fetchone()
        if row is None or datetime.fromisoformat(str(row["received_at"])) < cutoff:
            return None
        identifier = self.cipher.decrypt(
            str(row["external_context_enc"]),
            context=f"qq-reply:{channel_account}:{opaque_ref}",
        )
        return ResolvedQQReplyContext(str(row["context_kind"]), identifier)

    def remove_reply_context(self, channel_account: str, opaque_ref: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                DELETE FROM qq_reply_contexts
                WHERE channel_account=? AND opaque_ref=?
                """,
                (channel_account, opaque_ref),
            )
