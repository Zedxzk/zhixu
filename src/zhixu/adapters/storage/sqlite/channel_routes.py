"""Opaque channel routing metadata; external platform identifiers never enter this store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from zhixu.domain.agenda import require_aware
from zhixu.domain.errors import ValidationError

from .database import Database


@dataclass(frozen=True, slots=True)
class ChannelRoute:
    channel: str
    channel_account: str
    opaque_ref: str
    kind: str
    commands_enabled: bool
    last_seen_at: datetime


class ChannelRouteStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def observe(
        self,
        *,
        channel: str,
        channel_account: str,
        opaque_ref: str,
        kind: str,
        now: datetime,
    ) -> None:
        require_aware(now, "now")
        if kind not in {"private", "group", "channel", "actor"}:
            raise ValidationError("channel route kind is invalid")
        if any(not value.strip() for value in (channel, channel_account, opaque_ref)):
            raise ValidationError("channel route fields must not be empty")
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO channel_routes(
                    channel,channel_account,opaque_ref,route_kind,
                    commands_enabled,last_seen_at
                ) VALUES(?,?,?,?,0,?)
                ON CONFLICT(channel,channel_account,opaque_ref) DO UPDATE SET
                    route_kind=excluded.route_kind,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    channel,
                    channel_account,
                    opaque_ref,
                    kind,
                    now.astimezone(UTC).isoformat(),
                ),
            )

    def commands_enabled(
        self,
        channel: str,
        channel_account: str,
        opaque_ref: str,
    ) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT commands_enabled FROM channel_routes
                WHERE channel=? AND channel_account=? AND opaque_ref=?
                """,
                (channel, channel_account, opaque_ref),
            ).fetchone()
        return bool(row["commands_enabled"]) if row else False

    def get(
        self,
        channel: str,
        channel_account: str,
        opaque_ref: str,
    ) -> ChannelRoute | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM channel_routes
                WHERE channel=? AND channel_account=? AND opaque_ref=?
                """,
                (channel, channel_account, opaque_ref),
            ).fetchone()
        if row is None:
            return None
        return ChannelRoute(
            str(row["channel"]),
            str(row["channel_account"]),
            str(row["opaque_ref"]),
            str(row["route_kind"]),
            bool(row["commands_enabled"]),
            datetime.fromisoformat(str(row["last_seen_at"])),
        )

    def set_commands_enabled(
        self,
        *,
        channel: str,
        channel_account: str,
        opaque_ref: str,
        enabled: bool,
        actor_user_id: str,
        now: datetime,
    ) -> bool:
        require_aware(now, "now")
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE channel_routes SET commands_enabled=?
                WHERE channel=? AND channel_account=? AND opaque_ref=?
                """,
                (int(enabled), channel, channel_account, opaque_ref),
            ).rowcount
            connection.execute(
                """
                INSERT INTO audit_events(
                    occurred_at,actor_user_id,action,resource_kind,resource_id,
                    outcome,reason_code
                ) VALUES(?,?,'update','channel_route',?,?,?)
                """,
                (
                    now.astimezone(UTC).isoformat(),
                    actor_user_id,
                    opaque_ref,
                    "completed" if changed else "not_found",
                    "commands_enabled" if enabled else "commands_disabled",
                ),
            )
        return changed == 1

    def list(self, *, limit: int = 200) -> list[ChannelRoute]:
        if not 1 <= limit <= 500:
            raise ValidationError("channel route limit is invalid")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM channel_routes
                ORDER BY last_seen_at DESC,channel,channel_account,opaque_ref
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            ChannelRoute(
                str(row["channel"]),
                str(row["channel_account"]),
                str(row["opaque_ref"]),
                str(row["route_kind"]),
                bool(row["commands_enabled"]),
                datetime.fromisoformat(str(row["last_seen_at"])),
            )
            for row in rows
        ]
