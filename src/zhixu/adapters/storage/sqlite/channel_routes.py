"""Opaque channel routing metadata; external platform identifiers never enter this store."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from zhixu.domain.agenda import require_aware
from zhixu.domain.errors import PermissionDenied, ValidationError

from .database import Database


class GroupMode(StrEnum):
    DISABLED = "disabled"
    PUBLIC = "public"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class ChannelRoute:
    channel: str
    channel_account: str
    opaque_ref: str
    kind: str
    commands_enabled: bool
    owner_user_id: str | None
    group_mode: GroupMode
    shared_owner_user_id: str | None
    member_user_ids: tuple[str, ...]
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
                    commands_enabled,owner_user_id,group_mode,
                    shared_owner_user_id,last_seen_at
                ) VALUES(?,?,?,?,0,NULL,'disabled',NULL,?)
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
        members = self._members(channel, channel_account, opaque_ref)
        return ChannelRoute(
            str(row["channel"]),
            str(row["channel_account"]),
            str(row["opaque_ref"]),
            str(row["route_kind"]),
            bool(row["commands_enabled"]),
            str(row["owner_user_id"]) if row["owner_user_id"] is not None else None,
            GroupMode(str(row["group_mode"])),
            (
                str(row["shared_owner_user_id"])
                if row["shared_owner_user_id"] is not None
                else None
            ),
            members,
            datetime.fromisoformat(str(row["last_seen_at"])),
        )

    def _members(
        self,
        channel: str,
        channel_account: str,
        opaque_ref: str,
    ) -> tuple[str, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT user_id FROM channel_route_members
                WHERE channel=? AND channel_account=? AND opaque_ref=?
                ORDER BY user_id
                """,
                (channel, channel_account, opaque_ref),
            ).fetchall()
        return tuple(str(row["user_id"]) for row in rows)

    def is_member(
        self,
        channel: str,
        channel_account: str,
        opaque_ref: str,
        user_id: str,
    ) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM channel_route_members
                WHERE channel=? AND channel_account=? AND opaque_ref=? AND user_id=?
                """,
                (channel, channel_account, opaque_ref, user_id),
            ).fetchone()
        return row is not None

    def shared_owners_for_member(self, user_id: str) -> tuple[str, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT routes.shared_owner_user_id
                FROM channel_route_members AS members
                JOIN channel_routes AS routes
                  ON routes.channel=members.channel
                 AND routes.channel_account=members.channel_account
                 AND routes.opaque_ref=members.opaque_ref
                WHERE members.user_id=?
                  AND routes.commands_enabled=1
                  AND routes.group_mode='internal'
                  AND routes.shared_owner_user_id IS NOT NULL
                ORDER BY routes.shared_owner_user_id
                """,
                (user_id,),
            ).fetchall()
        return tuple(str(row["shared_owner_user_id"]) for row in rows)

    def set_commands_enabled(
        self,
        *,
        channel: str,
        channel_account: str,
        opaque_ref: str,
        enabled: bool,
        actor_user_id: str,
        now: datetime,
        group_mode: GroupMode | None = None,
        member_user_ids: tuple[str, ...] | None = None,
    ) -> bool:
        require_aware(now, "now")
        with self.database.transaction() as connection:
            route = connection.execute(
                """
                SELECT route_kind,owner_user_id,shared_owner_user_id FROM channel_routes
                WHERE channel=? AND channel_account=? AND opaque_ref=?
                """,
                (channel, channel_account, opaque_ref),
            ).fetchone()
            if (
                route is not None
                and route["owner_user_id"] is not None
                and str(route["owner_user_id"]) != actor_user_id
            ):
                raise PermissionDenied("channel route is owned by another user")
            if route is None:
                mode = GroupMode.DISABLED
                shared_owner_user_id = None
            elif str(route["route_kind"]) == "group":
                mode = (group_mode or GroupMode.INTERNAL) if enabled else GroupMode.DISABLED
                shared_owner_user_id = (
                    str(route["shared_owner_user_id"])
                    if route["shared_owner_user_id"] is not None
                    else None
                )
                if mode is GroupMode.INTERNAL and shared_owner_user_id is None:
                    shared_owner_user_id = f"group_{uuid.uuid4().hex}"
                    connection.execute(
                        """
                        INSERT INTO users(id,display_name,status,created_at)
                        VALUES(?,'Shared group workspace','active',?)
                        """,
                        (shared_owner_user_id, now.astimezone(UTC).isoformat()),
                    )
            else:
                if group_mode not in {None, GroupMode.DISABLED} or member_user_ids:
                    raise ValidationError("group settings are only valid for group routes")
                mode = GroupMode.DISABLED
                shared_owner_user_id = None
            if mode is not GroupMode.INTERNAL and member_user_ids:
                raise ValidationError("members are only valid for internal groups")
            changed = connection.execute(
                """
                UPDATE channel_routes SET
                    commands_enabled=?,
                    owner_user_id=CASE WHEN ? THEN ? ELSE owner_user_id END,
                    group_mode=?,
                    shared_owner_user_id=COALESCE(shared_owner_user_id, ?)
                WHERE channel=? AND channel_account=? AND opaque_ref=?
                """,
                (
                    int(enabled),
                    int(enabled),
                    actor_user_id,
                    mode.value,
                    shared_owner_user_id,
                    channel,
                    channel_account,
                    opaque_ref,
                ),
            ).rowcount
            if changed and route is not None and str(route["route_kind"]) == "group":
                if mode is GroupMode.INTERNAL:
                    members = set(member_user_ids or ())
                    members.add(actor_user_id)
                    if member_user_ids is not None:
                        placeholders = ",".join("?" for _ in members)
                        connection.execute(
                            f"""
                            DELETE FROM channel_route_members
                            WHERE channel=? AND channel_account=? AND opaque_ref=?
                              AND user_id NOT IN ({placeholders})
                            """,
                            (channel, channel_account, opaque_ref, *sorted(members)),
                        )
                    for member_user_id in sorted(members):
                        active = connection.execute(
                            "SELECT 1 FROM users WHERE id=? AND status='active'",
                            (member_user_id,),
                        ).fetchone()
                        if active is None:
                            raise ValidationError("internal group member is not active")
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO channel_route_members(
                                channel,channel_account,opaque_ref,user_id,
                                added_by_user_id,created_at
                            ) VALUES(?,?,?,?,?,?)
                            """,
                            (
                                channel,
                                channel_account,
                                opaque_ref,
                                member_user_id,
                                actor_user_id,
                                now.astimezone(UTC).isoformat(),
                            ),
                        )
                else:
                    connection.execute(
                        """
                        DELETE FROM channel_route_members
                        WHERE channel=? AND channel_account=? AND opaque_ref=?
                        """,
                        (channel, channel_account, opaque_ref),
                    )
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
            route
            for row in rows
            if (
                route := self.get(
                    str(row["channel"]),
                    str(row["channel_account"]),
                    str(row["opaque_ref"]),
                )
            )
            is not None
        ]
