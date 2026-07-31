"""Task state machine with optimistic concurrency metadata."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from .agenda import require_aware
from .classification import DataClassification, require_ordinary_storage
from .errors import InvalidTransition, ValidationError


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


ALLOWED_TRANSITIONS = {
    TaskStatus.PENDING: {
        TaskStatus.IN_PROGRESS,
        TaskStatus.COMPLETED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.IN_PROGRESS: {
        TaskStatus.PENDING,
        TaskStatus.COMPLETED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.COMPLETED: {TaskStatus.ARCHIVED},
    TaskStatus.CANCELLED: {TaskStatus.ARCHIVED},
    TaskStatus.ARCHIVED: set(),
}


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    owner_user_id: str
    title: str
    creator_user_id: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 0
    due_at: datetime | None = None
    description: str = ""
    classification: DataClassification = DataClassification.PERSONAL
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.owner_user_id.strip() or not self.title.strip():
            raise ValidationError("task id, owner, and title are required")
        if self.creator_user_id is not None and not self.creator_user_id.strip():
            raise ValidationError("task creator must not be empty")
        if not 0 <= self.priority <= 4:
            raise ValidationError("priority must be between 0 and 4")
        if self.due_at is not None:
            require_aware(self.due_at, "due_at")
        if self.version < 1:
            raise ValidationError("version must be positive")
        require_ordinary_storage(self.classification)

    def transition(self, status: TaskStatus, *, now: datetime) -> Task:
        require_aware(now, "now")
        if status not in ALLOWED_TRANSITIONS[self.status]:
            raise InvalidTransition(f"cannot transition from {self.status} to {status}")
        return replace(self, status=status, version=self.version + 1, updated_at=now)

    def postpone(self, due_at: datetime, *, now: datetime) -> Task:
        require_aware(due_at, "due_at")
        require_aware(now, "now")
        if self.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.ARCHIVED}:
            raise InvalidTransition(f"cannot postpone a {self.status} task")
        return replace(self, due_at=due_at, version=self.version + 1, updated_at=now)
