"""Anniversaries and user-configured daily briefing schedules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from .agenda import require_aware, require_timezone
from .classification import DataClassification, require_ordinary_storage
from .errors import ValidationError


@dataclass(frozen=True, slots=True)
class Anniversary:
    id: str
    owner_user_id: str
    creator_user_id: str
    title: str
    anchor_date: date
    timezone: str
    classification: DataClassification = DataClassification.PERSONAL
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (self.id, self.owner_user_id, self.creator_user_id, self.title)
        ):
            raise ValidationError("anniversary fields must not be empty")
        require_timezone(self.timezone)
        require_ordinary_storage(self.classification)
        if self.created_at is not None:
            require_aware(self.created_at, "created_at")

    def day_number(self, today: date) -> int:
        return (today - self.anchor_date).days + 1


@dataclass(frozen=True, slots=True)
class DailyBriefing:
    id: str
    owner_user_id: str
    creator_user_id: str
    target_ref: str
    time_of_day: time
    timezone: str
    classification: DataClassification = DataClassification.PERSONAL
    enabled: bool = True
    last_sent_on: date | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.id,
                self.owner_user_id,
                self.creator_user_id,
                self.target_ref,
            )
        ):
            raise ValidationError("daily briefing fields must not be empty")
        if self.time_of_day.tzinfo is not None:
            raise ValidationError("daily briefing wall time must not include timezone")
        require_timezone(self.timezone)
        require_ordinary_storage(self.classification)
        if self.created_at is not None:
            require_aware(self.created_at, "created_at")
        if self.updated_at is not None:
            require_aware(self.updated_at, "updated_at")
