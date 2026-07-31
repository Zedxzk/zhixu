"""Reminder domain model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .agenda import require_aware
from .classification import DataClassification, require_ordinary_storage
from .errors import ValidationError


class ReminderStatus(StrEnum):
    PENDING = "pending"
    FIRED = "fired"
    CANCELLED = "cancelled"


class MissedReminderPolicy(StrEnum):
    FIRE = "fire"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class Reminder:
    id: str
    owner_user_id: str
    title: str
    fire_at: datetime
    target_ref: str
    creator_user_id: str | None = None
    status: ReminderStatus = ReminderStatus.PENDING
    missed_policy: MissedReminderPolicy = MissedReminderPolicy.FIRE
    classification: DataClassification = DataClassification.PERSONAL
    related_kind: str | None = None
    related_id: str | None = None
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        required = (self.id, self.owner_user_id, self.title, self.target_ref)
        if any(not value.strip() for value in required):
            raise ValidationError("reminder id, owner, title, and target are required")
        if self.creator_user_id is not None and not self.creator_user_id.strip():
            raise ValidationError("reminder creator must not be empty")
        require_aware(self.fire_at, "fire_at")
        if (self.related_kind is None) != (self.related_id is None):
            raise ValidationError("related kind and id must be supplied together")
        if self.version < 1:
            raise ValidationError("version must be positive")
        require_ordinary_storage(self.classification)
