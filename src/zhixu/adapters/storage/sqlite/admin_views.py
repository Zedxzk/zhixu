"""Privacy-minimized read models for the private administration API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from zhixu.domain import Action, DataClassification, ResourceRef
from zhixu.domain.errors import NotFoundError, PermissionDenied, ValidationError

from .database import Database


def _bounded_limit(limit: int) -> int:
    if not 1 <= limit <= 200:
        raise ValidationError("limit must be between 1 and 200")
    return limit


@dataclass(frozen=True, slots=True)
class AdminReadStore:
    """Returns only fields deliberately approved for authenticated administration."""

    database: Database

    def resource_ref(
        self,
        *,
        kind: str,
        resource_id: str,
        owner_user_id: str,
    ) -> ResourceRef:
        """Resolve classification server-side so clients cannot downgrade policy."""

        ordinary = {
            "agenda": ("agenda_items", "owner_user_id"),
            "task": ("tasks", "owner_user_id"),
            "note": ("notes", "owner_user_id"),
            "reminder": ("reminders", "owner_user_id"),
        }
        if kind in ordinary:
            table, owner_column = ordinary[kind]
            with self.database.connect() as connection:
                row = connection.execute(
                    f"""
                    SELECT {owner_column} AS owner_user_id,classification
                    FROM {table} WHERE id=?
                    """,
                    (resource_id,),
                ).fetchone()
            if row is None:
                raise NotFoundError("ACL resource was not found")
            if str(row["owner_user_id"]) != owner_user_id:
                raise PermissionDenied("ACL resource is not owned by the principal")
            return ResourceRef(
                kind,
                resource_id,
                owner_user_id,
                DataClassification(int(row["classification"])),
            )
        if kind == "external_identity":
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT user_id FROM external_identities WHERE id=?",
                    (resource_id,),
                ).fetchone()
            if row is None:
                raise NotFoundError("ACL resource was not found")
            if str(row["user_id"]) != owner_user_id:
                raise PermissionDenied("ACL resource is not owned by the principal")
            return ResourceRef(kind, resource_id, owner_user_id)
        raise ValidationError("unsupported ACL resource kind")

    def status(self) -> dict[str, object]:
        """Return aggregate status without paths, hosts, identifiers, or payloads."""

        with self.database.connect() as connection:
            connection.execute("SELECT 1").fetchone()
            schema_row = connection.execute(
                "SELECT COALESCE(MAX(version),0) AS version FROM schema_migrations"
            ).fetchone()
            entity_counts = {
                name: int(
                    connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
                )
                for name, table in (
                    ("users", "users"),
                    ("identities", "external_identities"),
                    ("agenda", "agenda_items"),
                    ("tasks", "tasks"),
                    ("notes", "notes"),
                    ("reminders", "reminders"),
                )
            }
            delivery_counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    """
                    SELECT status,COUNT(*) AS count
                    FROM outbox_deliveries GROUP BY status ORDER BY status
                    """
                )
            }
        return {
            "schema_version": int(schema_row["version"]) if schema_row else 0,
            "storage": "available",
            "entities": entity_counts,
            "delivery": delivery_counts,
        }

    def identities(self, user_id: str) -> list[dict[str, object]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id,channel,channel_account,opaque_ref,created_at
                FROM external_identities
                WHERE user_id=?
                ORDER BY channel,channel_account,opaque_ref
                """,
                (user_id,),
            ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "channel": str(row["channel"]),
                "channel_account": str(row["channel_account"]),
                "opaque_ref": str(row["opaque_ref"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def grants(
        self,
        *,
        owner_user_id: str,
        resource: ResourceRef,
    ) -> list[dict[str, object]]:
        if resource.owner_user_id != owner_user_id:
            raise PermissionDenied("ACL owner does not match the current principal")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT subject_user_id,action,created_at
                FROM resource_acl
                WHERE resource_kind=? AND resource_id=? AND granted_by=?
                ORDER BY subject_user_id,action
                """,
                (resource.kind, resource.id, owner_user_id),
            ).fetchall()
        return [
            {
                "subject_user_id": str(row["subject_user_id"]),
                "action": str(row["action"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def outbox(self, user_id: str, *, limit: int = 100) -> list[dict[str, object]]:
        """Do not expose target refs, idempotency keys, or message payloads."""

        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id,channel,message_kind,classification,priority,status,
                       attempts,max_attempts,next_attempt_at,last_error_code,
                       created_at,updated_at
                FROM outbox_deliveries
                WHERE owner_user_id=?
                ORDER BY created_at DESC,id
                LIMIT ?
                """,
                (user_id, _bounded_limit(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def audit(self, user_id: str, *, limit: int = 100) -> list[dict[str, object]]:
        """Audit details contain stable internal resource IDs, never message bodies."""

        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT occurred_at,action,resource_kind,resource_id,outcome,reason_code
                FROM audit_events
                WHERE actor_user_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, _bounded_limit(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_channel_session(
        self,
        *,
        session_id: str,
        identity_id: str,
        user_id: str,
        channel: str,
        channel_account: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        if created_at.tzinfo is None or expires_at.tzinfo is None:
            raise ValidationError("channel session times must be timezone-aware")
        if expires_at <= created_at:
            raise ValidationError("channel session expiry must be after creation")
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT user_id,channel,channel_account
                FROM external_identities WHERE id=?
                """,
                (identity_id,),
            ).fetchone()
            if row is None:
                raise PermissionDenied("identity is unavailable")
            if (
                str(row["user_id"]) != user_id
                or str(row["channel"]) != channel
                or str(row["channel_account"]) != channel_account
            ):
                raise PermissionDenied("channel session identity mismatch")
            connection.execute(
                """
                INSERT INTO channel_sessions(
                    id,external_identity_id,user_id,channel,channel_account,
                    created_at,expires_at,revoked_at
                ) VALUES(?,?,?,?,?,?,?,NULL)
                """,
                (
                    session_id,
                    identity_id,
                    user_id,
                    channel,
                    channel_account,
                    created_at.astimezone(UTC).isoformat(),
                    expires_at.astimezone(UTC).isoformat(),
                ),
            )

    def channel_session_active(self, session_id: str, *, now: datetime) -> bool:
        if now.tzinfo is None:
            raise ValidationError("channel session time must be timezone-aware")
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM channel_sessions
                WHERE id=? AND revoked_at IS NULL AND expires_at>?
                """,
                (session_id, now.astimezone(UTC).isoformat()),
            ).fetchone()
        return row is not None


def acl_resource(
    *,
    kind: str,
    resource_id: str,
    owner_user_id: str,
    classification: int,
) -> ResourceRef:
    try:
        level = DataClassification(classification)
    except ValueError as exc:
        raise ValidationError("invalid resource classification") from exc
    return ResourceRef(kind, resource_id, owner_user_id, level)


def acl_action(value: str) -> Action:
    try:
        action = Action(value)
    except ValueError as exc:
        raise ValidationError("invalid ACL action") from exc
    if action in {Action.GRANT, Action.EXPORT, Action.REVEAL, Action.ROTATE, Action.USE}:
        raise ValidationError("this ACL action is not available for ordinary resources")
    return action
