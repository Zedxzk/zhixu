"""Conversation-bound storage for plans awaiting explicit user approval."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol


@dataclass(frozen=True, slots=True)
class StoredPendingPlan:
    id: str
    actor_user_id: str
    target_ref: str
    action: str
    payload_json: str
    state: str
    expires_at: datetime


class PendingPlanStorePort(Protocol):
    def put(
        self,
        *,
        actor_user_id: str,
        target_ref: str,
        action: str,
        payload_json: str,
        now: datetime,
        ttl: timedelta = timedelta(minutes=30),
    ) -> StoredPendingPlan: ...

    def get(
        self,
        plan_id: str,
        *,
        actor_user_id: str,
        target_ref: str,
        now: datetime,
    ) -> StoredPendingPlan | None: ...

    def revising(
        self,
        *,
        actor_user_id: str,
        target_ref: str,
        now: datetime,
    ) -> StoredPendingPlan | None: ...

    def reject(self, plan_id: str, *, now: datetime) -> None: ...

    def consume(self, plan_id: str, *, now: datetime) -> bool: ...
