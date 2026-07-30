"""Encrypted outbound target registry shared by non-QQ adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from zhixu.adapters.storage.sqlite import Database
from zhixu.domain.agenda import require_aware
from zhixu.domain.errors import NotFoundError, ValidationError
from zhixu.security import FieldCipher, OpaqueReferenceFactory


@dataclass(frozen=True, slots=True)
class ResolvedOutboundTarget:
    channel: str
    channel_account: str
    kind: str
    value: str = field(repr=False)
    opaque_ref: str


class OutboundTargetResolver:
    def __init__(
        self,
        database: Database,
        cipher: FieldCipher,
    ) -> None:
        self.database = database
        self.cipher = cipher

    def resolve(
        self,
        *,
        channel: str,
        channel_account: str,
        opaque_ref: str,
    ) -> ResolvedOutboundTarget:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT channel,channel_account,opaque_ref,target_enc,target_kind
                FROM outbound_targets
                WHERE channel=? AND channel_account=? AND opaque_ref=?
                """,
                (channel, channel_account, opaque_ref),
            ).fetchone()
        if row is None:
            raise NotFoundError("outbound target was not found")
        kind = str(row["target_kind"])
        context = f"outbound-target:{channel}:{channel_account}:{kind}:{opaque_ref}"
        target = self.cipher.decrypt(str(row["target_enc"]), context=context)
        return ResolvedOutboundTarget(
            channel,
            channel_account,
            kind,
            target,
            opaque_ref,
        )


class OutboundTargetStore(OutboundTargetResolver):
    def __init__(
        self,
        database: Database,
        cipher: FieldCipher,
        references: OpaqueReferenceFactory,
    ) -> None:
        super().__init__(database, cipher)
        self.references = references

    def register(
        self,
        *,
        channel: str,
        channel_account: str,
        kind: str,
        target: str,
        now: datetime,
    ) -> str:
        require_aware(now, "now")
        values = (channel, channel_account, kind, target)
        if any(not value.strip() for value in values):
            raise ValidationError("outbound target fields must not be empty")
        opaque_ref = self.references.create(
            "target",
            channel,
            channel_account,
            kind,
            target,
        )
        context = f"outbound-target:{channel}:{channel_account}:{kind}:{opaque_ref}"
        encrypted = self.cipher.encrypt(target, context=context)
        now_text = now.astimezone(UTC).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO outbound_targets(
                    id,channel,channel_account,opaque_ref,target_enc,target_kind,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(channel,channel_account,opaque_ref) DO UPDATE SET
                    target_enc=excluded.target_enc,
                    target_kind=excluded.target_kind,
                    updated_at=excluded.updated_at
                """,
                (
                    opaque_ref,
                    channel,
                    channel_account,
                    opaque_ref,
                    encrypted,
                    kind,
                    now_text,
                    now_text,
                ),
            )
        return opaque_ref
