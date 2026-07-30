"""HMAC-chained audit events that expose no secret material."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .database import VaultDatabase

_CHECKPOINT_FORMAT = "zhixu-vault-audit-checkpoint-v1"
_MAX_CHECKPOINT_BYTES = 64 * 1024


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

    def write_checkpoint(
        self,
        directory: str | Path,
        *,
        audit_key: bytes,
        now: datetime,
        keep: int = 90,
    ) -> Path:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("audit checkpoint time must include a timezone")
        if not 2 <= keep <= 3650:
            raise ValueError("audit checkpoint retention is invalid")
        if not self.verify(audit_key=audit_key):
            raise RuntimeError("vault audit chain is invalid")
        sequence, event_mac = self._head()
        body = {
            "format": _CHECKPOINT_FORMAT,
            "sequence": sequence,
            "head_event_mac": event_mac,
            "created_at": now.astimezone(UTC).isoformat(),
        }
        checkpoint_mac = hmac.new(
            audit_key,
            b"vault-audit-checkpoint:" + _canonical(body),
            hashlib.sha256,
        ).hexdigest()
        payload = json.dumps(
            {**body, "checkpoint_mac": checkpoint_mac},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        target_directory = Path(directory)
        target_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not stat.S_ISDIR(target_directory.lstat().st_mode):
            raise PermissionError("audit checkpoint directory must not be a symlink")
        target = target_directory / (
            f"audit-{sequence:020d}-"
            f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-"
            f"{secrets.token_hex(6)}.zac"
        )
        _atomic_create(target, payload)
        self._retain(target_directory, keep)
        return target

    def verify_latest_checkpoint(
        self,
        directory: str | Path,
        *,
        audit_key: bytes,
    ) -> bool:
        target_directory = Path(directory)
        try:
            if not stat.S_ISDIR(target_directory.lstat().st_mode):
                return False
            candidates = self._checkpoint_files(target_directory)
            if not candidates:
                return False
            checkpoint = _read_checkpoint(candidates[0])
            body = {
                "format": checkpoint["format"],
                "sequence": checkpoint["sequence"],
                "head_event_mac": checkpoint["head_event_mac"],
                "created_at": checkpoint["created_at"],
            }
            expected = hmac.new(
                audit_key,
                b"vault-audit-checkpoint:" + _canonical(body),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(
                expected,
                str(checkpoint["checkpoint_mac"]),
            ):
                return False
            if not self.verify(audit_key=audit_key):
                return False
            sequence = int(checkpoint["sequence"])
            event_mac = str(checkpoint["head_event_mac"])
            if sequence == 0:
                return event_mac == ""
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT event_mac FROM vault_audit WHERE sequence=?",
                    (sequence,),
                ).fetchone()
            return row is not None and hmac.compare_digest(
                event_mac,
                str(row["event_mac"]),
            )
        except (OSError, ValueError):
            return False

    def _head(self) -> tuple[int, str]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT sequence,event_mac
                FROM vault_audit ORDER BY sequence DESC LIMIT 1
                """
            ).fetchone()
        if row is None:
            return 0, ""
        return int(row["sequence"]), str(row["event_mac"])

    @staticmethod
    def _checkpoint_files(directory: Path) -> list[Path]:
        candidates: list[Path] = []
        for path in directory.glob("audit-*.zac"):
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("audit checkpoint entry is invalid")
            candidates.append(path)
        return sorted(
            candidates,
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )

    def _retain(self, directory: Path, keep: int) -> None:
        for path in self._checkpoint_files(directory)[keep:]:
            path.unlink()
        _fsync_directory(directory)


def _canonical(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _read_checkpoint(path: Path) -> dict[str, Any]:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > _MAX_CHECKPOINT_BYTES
    ):
        raise ValueError("audit checkpoint file is invalid")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("audit checkpoint contains a duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("audit checkpoint JSON is invalid") from exc
    expected = {
        "format",
        "sequence",
        "head_event_mac",
        "created_at",
        "checkpoint_mac",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("audit checkpoint schema is invalid")
    if value["format"] != _CHECKPOINT_FORMAT:
        raise ValueError("audit checkpoint format is unsupported")
    if (
        isinstance(value["sequence"], bool)
        or not isinstance(value["sequence"], int)
        or value["sequence"] < 0
        or not all(
            isinstance(value[name], str)
            for name in ("head_event_mac", "created_at", "checkpoint_mac")
        )
        or len(value["head_event_mac"]) > 64
        or len(value["checkpoint_mac"]) != 64
    ):
        raise ValueError("audit checkpoint values are invalid")
    created_at = datetime.fromisoformat(value["created_at"])
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("audit checkpoint time is invalid")
    return value


def _atomic_create(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".partial",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(payload)
            output.flush()
            os.fsync(descriptor)
        os.link(temporary, path)
        _fsync_directory(path.parent)
    finally:
        with suppress(OSError):
            os.close(descriptor)
        with suppress(OSError):
            temporary.unlink()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
