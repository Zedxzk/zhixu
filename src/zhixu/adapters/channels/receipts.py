"""Body-free, idempotent receipt reservation for all inbound channels."""

from __future__ import annotations

from datetime import UTC
from typing import Protocol

from zhixu.adapters.storage.sqlite.database import Database
from zhixu.channels import InboundEvent
from zhixu.security import OpaqueReferenceFactory


class AdmissionDecisionLike(Protocol):
    accepted: bool
    reason_code: str


class InboundReceiptStore:
    """Reserves an event before execution and never persists its message body."""

    def __init__(
        self,
        database: Database,
        references: OpaqueReferenceFactory,
    ) -> None:
        self.database = database
        self.references = references

    def record(
        self,
        event: InboundEvent,
        decision: AdmissionDecisionLike,
        *,
        intent_kind: str = "",
    ) -> bool:
        inserted = self.reserve(event, decision)
        if inserted:
            self.complete(event, decision, intent_kind=intent_kind)
        return inserted

    def reserve(self, event: InboundEvent, decision: AdmissionDecisionLike) -> bool:
        event_hash, message_hash = self._hashes(event)
        reason_code = decision.reason_code
        accepted = (
            decision.accepted
            if hasattr(decision, "accepted")
            else reason_code == "accepted"
        )
        outcome = "processing" if accepted else reason_code
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO inbound_event_receipts(
                    channel,channel_account,event_id_hash,message_hash,
                    actor_ref,conversation_ref,intent_kind,outcome,received_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    event.channel,
                    event.channel_account,
                    event_hash,
                    message_hash,
                    event.external_actor_ref,
                    event.external_conversation_ref,
                    "",
                    outcome,
                    event.received_at.astimezone(UTC).isoformat(),
                ),
            )
        return cursor.rowcount == 1

    def complete(
        self,
        event: InboundEvent,
        decision: AdmissionDecisionLike,
        *,
        intent_kind: str,
    ) -> None:
        event_hash, _message_hash = self._hashes(event)
        reason_code = decision.reason_code
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE inbound_event_receipts
                SET intent_kind=?,outcome=?
                WHERE channel=? AND channel_account=? AND event_id_hash=?
                """,
                (
                    intent_kind,
                    reason_code,
                    event.channel,
                    event.channel_account,
                    event_hash,
                ),
            )

    def _hashes(self, event: InboundEvent) -> tuple[str, str]:
        event_hash = self.references.create(
            "evt",
            event.channel,
            event.channel_account,
            event.event_id,
        )
        message_hash = self.references.create(
            "msg",
            event.channel,
            event.channel_account,
            event.text or "",
        )
        return event_hash, message_hash
