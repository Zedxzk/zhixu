"""Notes and tags eligible for ordinary L0-L2 storage and FTS."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .agenda import require_aware
from .classification import DataClassification, require_ordinary_storage
from .errors import ValidationError


@dataclass(frozen=True, slots=True)
class Note:
    id: str
    owner_user_id: str
    title: str
    body: str
    tags: tuple[str, ...] = ()
    classification: DataClassification = DataClassification.PERSONAL
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.owner_user_id.strip():
            raise ValidationError("note id and owner are required")
        if not self.title.strip() and not self.body.strip():
            raise ValidationError("note title or body is required")
        if self.version < 1:
            raise ValidationError("version must be positive")
        if self.created_at is not None:
            require_aware(self.created_at, "created_at")
        if self.updated_at is not None:
            require_aware(self.updated_at, "updated_at")
        if any(not tag.strip() for tag in self.tags):
            raise ValidationError("tags must not be empty")
        if len(set(self.tags)) != len(self.tags):
            raise ValidationError("tags must be unique")
        require_ordinary_storage(self.classification)
