"""Reminder domain model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .action_link import ActionLink
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
    action_links: tuple[ActionLink, ...] = field(default_factory=tuple, repr=False)
    status: ReminderStatus = ReminderStatus.PENDING
    missed_policy: MissedReminderPolicy = MissedReminderPolicy.FIRE
    classification: DataClassification = DataClassification.PERSONAL
    related_kind: str | None = None
    related_id: str | None = None
    # When this reminder announces something that starts later, the moment
    # it starts. Distinct from fire_at, which is when the reminder speaks.
    related_start_at: datetime | None = None
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
        if self.related_start_at is not None:
            require_aware(self.related_start_at, "related_start_at")
            if self.related_id is None:
                raise ValidationError(
                    "a related start time needs the resource it belongs to"
                )
        if self.version < 1:
            raise ValidationError("version must be positive")
        require_ordinary_storage(self.classification)
        if len(self.action_links) > 8:
            raise ValidationError("reminder action link limit exceeded")
