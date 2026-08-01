"""SQLite pending-plan state for continuous assistant conversations."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from zhixu.domain.agenda import require_aware
from zhixu.ports import StoredPendingPlan

from .database import Database


def _dump(value: datetime) -> str:
    require_aware(value, "datetime")
    return value.astimezone(UTC).isoformat()


class PendingPlanStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _from_row(row) -> StoredPendingPlan:
        return StoredPendingPlan(
            id=str(row["id"]),
            actor_user_id=str(row["actor_user_id"]),
            target_ref=str(row["target_ref"]),
            action=str(row["action"]),
            payload_json=str(row["payload_json"]),
            state=str(row["state"]),
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
        )

    def put(
        self,
        *,
        actor_user_id: str,
        target_ref: str,
        action: str,
        payload_json: str,
        now: datetime,
        ttl: timedelta = timedelta(minutes=30),
    ) -> StoredPendingPlan:
        require_aware(now, "now")
        plan_id = "plan_" + secrets.token_urlsafe(12)
        expires_at = now + ttl
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE assistant_pending_plans SET state='accepted'
                WHERE actor_user_id=? AND target_ref=?
                  AND state IN ('pending','revising')
                """,
                (actor_user_id, target_ref),
            )
            connection.execute(
                """
                INSERT INTO assistant_pending_plans(
                    id,actor_user_id,target_ref,action,payload_json,state,
                    created_at,expires_at
                ) VALUES(?,?,?,?,?,'pending',?,?)
                """,
                (
                    plan_id,
                    actor_user_id,
                    target_ref,
                    action,
                    payload_json,
                    _dump(now),
                    _dump(expires_at),
                ),
            )
        return StoredPendingPlan(
            plan_id,
            actor_user_id,
            target_ref,
            action,
            payload_json,
            "pending",
            expires_at,
        )

    def get(
        self,
        plan_id: str,
        *,
        actor_user_id: str,
        target_ref: str,
        now: datetime,
    ) -> StoredPendingPlan | None:
        require_aware(now, "now")
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM assistant_pending_plans
                WHERE id=? AND actor_user_id=? AND target_ref=?
                  AND state IN ('pending','revising') AND expires_at>?
                """,
                (plan_id, actor_user_id, target_ref, _dump(now)),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def revising(
        self,
        *,
        actor_user_id: str,
        target_ref: str,
        now: datetime,
    ) -> StoredPendingPlan | None:
        require_aware(now, "now")
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM assistant_pending_plans
                WHERE actor_user_id=? AND target_ref=? AND state='revising'
                  AND expires_at>?
                ORDER BY created_at DESC LIMIT 1
                """,
                (actor_user_id, target_ref, _dump(now)),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def current(
        self,
        *,
        actor_user_id: str,
        target_ref: str,
        now: datetime,
    ) -> StoredPendingPlan | None:
        require_aware(now, "now")
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM assistant_pending_plans
                WHERE actor_user_id=? AND target_ref=?
                  AND state IN ('pending','revising') AND expires_at>?
                ORDER BY created_at DESC LIMIT 1
                """,
                (actor_user_id, target_ref, _dump(now)),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def reject(self, plan_id: str, *, now: datetime) -> None:
        require_aware(now, "now")
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE assistant_pending_plans SET state='revising'
                WHERE id=? AND state='pending' AND expires_at>?
                """,
                (plan_id, _dump(now)),
            )

    def consume(self, plan_id: str, *, now: datetime) -> bool:
        require_aware(now, "now")
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE assistant_pending_plans SET state='accepted'
                WHERE id=? AND state IN ('pending','revising')
                """,
                (plan_id,),
            ).rowcount
        return changed == 1
