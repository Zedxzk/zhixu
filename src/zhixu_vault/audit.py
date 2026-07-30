"""HMAC-chained audit events that expose no secret material."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from .database import VaultDatabase


@dataclass(frozen=True, slots=True)
class AuditEvent:
    occurred_at: datetime
    actor: str
    action: str
    secret_id: str
    outcome: str
    reason_code: str = ""


def _event_bytes(event: AuditEvent, previous: str) -> bytes:
    return json.dumps(
        {
            "occurred_at": event.occurred_at.astimezone(UTC).isoformat(),
            "actor": event.actor,
            "action": event.action,
            "secret_id": event.secret_id,
            "outcome": event.outcome,
            "reason_code": event.reason_code,
            "previous_mac": previous,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


class VaultAuditLog:
    def __init__(self, database: VaultDatabase) -> None:
        self.database = database

    def append(
        self,
        connection: sqlite3.Connection,
        event: AuditEvent,
        *,
        audit_key: bytes,
    ) -> None:
        row = connection.execute(
            "SELECT event_mac FROM vault_audit ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous = str(row["event_mac"]) if row is not None else ""
        event_mac = hmac.new(
            audit_key,
            _event_bytes(event, previous),
            hashlib.sha256,
        ).hexdigest()
        connection.execute(
            """
            INSERT INTO vault_audit(
                occurred_at,actor,action,secret_id,outcome,reason_code,
                previous_mac,event_mac
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                event.occurred_at.astimezone(UTC).isoformat(),
                event.actor,
                event.action,
                event.secret_id,
                event.outcome,
                event.reason_code,
                previous,
                event_mac,
            ),
        )

    def verify(self, *, audit_key: bytes) -> bool:
        previous = ""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM vault_audit ORDER BY sequence"
            ).fetchall()
        for row in rows:
            if str(row["previous_mac"]) != previous:
                return False
            event = AuditEvent(
                occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
                actor=str(row["actor"]),
                action=str(row["action"]),
                secret_id=str(row["secret_id"]),
                outcome=str(row["outcome"]),
                reason_code=str(row["reason_code"]),
            )
            expected = hmac.new(
                audit_key,
                _event_bytes(event, previous),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, str(row["event_mac"])):
                return False
            previous = expected
        return True

    def verify_or_alert(
        self,
        *,
        audit_key: bytes,
        alert: Callable[[str], None],
    ) -> bool:
        valid = self.verify(audit_key=audit_key)
        if not valid:
            alert("vault_audit_chain_invalid")
        return valid
