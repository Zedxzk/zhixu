"""Persistent scheduled jobs and idempotent run records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

from .agenda import require_aware, require_timezone
from .errors import ValidationError


class JobRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    id: str
    owner_user_id: str
    job_kind: str
    schedule_spec: str
    timezone: str
    enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (self.id, self.owner_user_id, self.job_kind, self.schedule_spec)
        ):
            raise ValidationError("scheduled job fields must not be empty")
        require_timezone(self.timezone)
        try:
            rrulestr(self.schedule_spec, dtstart=datetime.now(ZoneInfo(self.timezone)))
        except (TypeError, ValueError) as exc:
            raise ValidationError("invalid scheduled job rule") from exc


@dataclass(frozen=True, slots=True)
class JobRun:
    id: str
    scheduled_job_id: str
    scheduled_for: datetime
    status: JobRunStatus = JobRunStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.scheduled_job_id.strip():
            raise ValidationError("job run id and scheduled job id are required")
        require_aware(self.scheduled_for, "scheduled_for")
        if self.started_at is not None:
            require_aware(self.started_at, "started_at")
        if self.completed_at is not None:
            require_aware(self.completed_at, "completed_at")
