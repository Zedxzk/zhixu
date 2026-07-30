"""Transactional SQLite outbox with leases, backoff and dead letters."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from zhixu.adapters.storage.sqlite.database import Database
from zhixu.channels import (
    ChannelDeliveryResult,
    MessageButton,
    MessageKind,
    OutboundMessage,
)
from zhixu.domain import DataClassification
from zhixu.domain.agenda import require_aware
from zhixu.domain.errors import ConflictError, ValidationError

DEFAULT_BACKOFF = (5, 30, 120, 600, 1800)


def _dump(value: datetime) -> str:
    require_aware(value, "datetime")
    return value.astimezone(UTC).isoformat()


def _load(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _message_json(message: OutboundMessage) -> str:
    return json.dumps(
        {
            "text": message.text,
            "buttons": [
                {"label": button.label, "action": button.action}
                for button in message.buttons
            ],
            "attachment_url": message.attachment_url,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _message_from_row(row: object) -> OutboundMessage:
    payload = json.loads(str(row["payload_json"]))  # type: ignore[index]
    return OutboundMessage(
        channel=str(row["channel"]),  # type: ignore[index]
        channel_account=str(row["channel_account"]),  # type: ignore[index]
        target_ref=str(row["target_ref"]),  # type: ignore[index]
        kind=MessageKind(str(row["message_kind"])),  # type: ignore[index]
        text=str(payload.get("text") or payload.get("title") or ""),
        buttons=tuple(
            MessageButton(str(item["label"]), str(item["action"]))
            for item in payload.get("buttons", [])
        ),
        attachment_url=payload.get("attachment_url"),
        classification=DataClassification(int(row["classification"])),  # type: ignore[index]
    )


@dataclass(frozen=True, slots=True)
class ClaimedDelivery:
    id: str
    owner_user_id: str
    idempotency_key: str
    message: OutboundMessage
    priority: int
    attempts: int
    max_attempts: int
    lease_token: str
    lease_expires_at: datetime


class OutboxStore:
    def __init__(
        self,
        database: Database,
        *,
        backoff_seconds: tuple[int, ...] = DEFAULT_BACKOFF,
    ) -> None:
        if not backoff_seconds or any(delay < 0 for delay in backoff_seconds):
            raise ValidationError("at least one non-negative backoff is required")
        self.database = database
        self.backoff_seconds = backoff_seconds

    def enqueue(
        self,
        *,
        delivery_id: str,
        idempotency_key: str,
        owner_user_id: str,
        message: OutboundMessage,
        now: datetime,
        priority: int = 30,
        max_attempts: int = 5,
        actor_user_id: str = "service:application",
    ) -> bool:
        require_aware(now, "now")
        if max_attempts < 1:
            raise ValidationError("max_attempts must be positive")
        now_text = _dump(now)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO outbox_deliveries(
                    id,idempotency_key,owner_user_id,target_ref,message_kind,
                    payload_json,classification,priority,status,attempts,max_attempts,
                    next_attempt_at,last_error_code,created_at,updated_at,
                    channel,channel_account,lease_owner,lease_token,lease_expires_at,
                    provider_message_id
                ) VALUES(?,?,?,?,?,?,?,?, 'pending',0,?,?,?,?,?,
                         ?,?,'','',NULL,'')
                """,
                (
                    delivery_id,
                    idempotency_key,
                    owner_user_id,
                    message.target_ref,
                    message.kind.value,
                    _message_json(message),
                    int(message.classification),
                    priority,
                    max_attempts,
                    now_text,
                    "",
                    now_text,
                    now_text,
                    message.channel,
                    message.channel_account,
                ),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """
                    INSERT INTO audit_events(
                        occurred_at,actor_user_id,action,resource_kind,resource_id,
                        outcome,reason_code
                    ) VALUES(?,?,'enqueue','outbox_delivery',?,'completed','')
                    """,
                    (now_text, actor_user_id, delivery_id),
                )
                return True
        return False

    def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta = timedelta(seconds=30),
    ) -> ClaimedDelivery | None:
        require_aware(now, "now")
        if not worker_id.strip() or lease_for <= timedelta(0):
            raise ValidationError("worker and positive lease duration are required")
        now_text = _dump(now)
        lease_expires = now + lease_for
        token = secrets.token_urlsafe(24)
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM outbox_deliveries
                WHERE (
                    status IN ('pending','retry_wait','quota_wait')
                    AND next_attempt_at<=?
                ) OR (
                    status='sending'
                    AND lease_expires_at IS NOT NULL
                    AND lease_expires_at<=?
                )
                ORDER BY priority,created_at,id
                LIMIT 1
                """,
                (now_text, now_text),
            ).fetchone()
            if row is None:
                return None
            changed = connection.execute(
                """
                UPDATE outbox_deliveries
                SET status='sending',attempts=attempts+1,lease_owner=?,lease_token=?,
                    lease_expires_at=?,updated_at=?
                WHERE id=? AND (
                    status IN ('pending','retry_wait','quota_wait')
                    OR (status='sending' AND lease_expires_at<=?)
                )
                """,
                (
                    worker_id,
                    token,
                    _dump(lease_expires),
                    now_text,
                    str(row["id"]),
                    now_text,
                ),
            ).rowcount
            if changed != 1:
                return None
            refreshed = connection.execute(
                "SELECT * FROM outbox_deliveries WHERE id=?",
                (str(row["id"]),),
            ).fetchone()
            assert refreshed is not None
        return ClaimedDelivery(
            id=str(refreshed["id"]),
            owner_user_id=str(refreshed["owner_user_id"]),
            idempotency_key=str(refreshed["idempotency_key"]),
            message=_message_from_row(refreshed),
            priority=int(refreshed["priority"]),
            attempts=int(refreshed["attempts"]),
            max_attempts=int(refreshed["max_attempts"]),
            lease_token=token,
            lease_expires_at=lease_expires,
        )

    def defer_for_quota(
        self,
        claimed: ClaimedDelivery,
        *,
        next_attempt_at: datetime,
        now: datetime,
        reason_code: str,
    ) -> None:
        require_aware(next_attempt_at, "next_attempt_at")
        require_aware(now, "now")
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE outbox_deliveries
                SET status='quota_wait',attempts=MAX(0,attempts-1),
                    next_attempt_at=?,last_error_code=?,lease_owner='',
                    lease_token='',lease_expires_at=NULL,updated_at=?
                WHERE id=? AND status='sending' AND lease_token=?
                """,
                (
                    _dump(next_attempt_at),
                    reason_code[:80],
                    _dump(now),
                    claimed.id,
                    claimed.lease_token,
                ),
            ).rowcount
            if changed != 1:
                raise ConflictError("outbox lease is no longer owned")

    def complete(
        self,
        claimed: ClaimedDelivery,
        result: ChannelDeliveryResult,
        *,
        now: datetime,
    ) -> str:
        require_aware(now, "now")
        now_text = _dump(now)
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM outbox_deliveries
                WHERE id=? AND status='sending' AND lease_token=?
                """,
                (claimed.id, claimed.lease_token),
            ).fetchone()
            if row is None:
                raise ConflictError("outbox lease is no longer owned")
            if result.ok:
                connection.execute(
                    """
                    UPDATE outbox_deliveries
                    SET status='sent',last_error_code='',provider_message_id=?,
                        lease_owner='',lease_token='',lease_expires_at=NULL,updated_at=?
                    WHERE id=?
                    """,
                    (result.provider_message_id[:160], now_text, claimed.id),
                )
                return "sent"
            attempts = int(row["attempts"])
            if result.retryable and attempts < int(row["max_attempts"]):
                index = min(max(attempts - 1, 0), len(self.backoff_seconds) - 1)
                connection.execute(
                    """
                    UPDATE outbox_deliveries
                    SET status='retry_wait',next_attempt_at=?,last_error_code=?,
                        lease_owner='',lease_token='',lease_expires_at=NULL,updated_at=?
                    WHERE id=?
                    """,
                    (
                        _dump(now + timedelta(seconds=self.backoff_seconds[index])),
                        result.provider_code[:80],
                        now_text,
                        claimed.id,
                    ),
                )
                return "retry_wait"
            connection.execute(
                """
                UPDATE outbox_deliveries
                SET status='dead',last_error_code=?,lease_owner='',lease_token='',
                    lease_expires_at=NULL,updated_at=?
                WHERE id=?
                """,
                (result.provider_code[:80], now_text, claimed.id),
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO dead_letters(
                    id,delivery_id,reason_code,created_at,retried_at
                ) VALUES(?,?,?,?,NULL)
                """,
                (f"dead_{claimed.id}", claimed.id, result.provider_code[:80], now_text),
            )
            return "dead"

    def retry_dead(self, dead_id: str, *, actor_user_id: str, now: datetime) -> bool:
        require_aware(now, "now")
        now_text = _dump(now)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT delivery_id FROM dead_letters WHERE id=?",
                (dead_id,),
            ).fetchone()
            if row is None:
                return False
            delivery_id = str(row["delivery_id"])
            connection.execute(
                """
                UPDATE outbox_deliveries
                SET status='pending',attempts=0,next_attempt_at=?,last_error_code='',
                    lease_owner='',lease_token='',lease_expires_at=NULL,updated_at=?
                WHERE id=?
                """,
                (now_text, now_text, delivery_id),
            )
            connection.execute(
                "UPDATE dead_letters SET retried_at=? WHERE id=?",
                (now_text, dead_id),
            )
            connection.execute(
                """
                INSERT INTO audit_events(
                    occurred_at,actor_user_id,action,resource_kind,resource_id,
                    outcome,reason_code
                ) VALUES(?,?,'retry','dead_letter',?,'completed','')
                """,
                (now_text, actor_user_id, dead_id),
            )
        return True

    def get_status(self, delivery_id: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status FROM outbox_deliveries WHERE id=?",
                (delivery_id,),
            ).fetchone()
        return str(row["status"]) if row is not None else None

    def with_message(
        self,
        claimed: ClaimedDelivery,
        message: OutboundMessage,
    ) -> ClaimedDelivery:
        """Return an in-memory capability-degraded claim without changing storage."""

        return replace(claimed, message=message)
