"""Notes and tags eligible for ordinary L0-L2 storage and FTS."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .agenda import require_aware
from .classification import DataClassification, require_ordinary_storage
from .errors import ValidationError


@dataclass(frozen=True, slots=True)
class NoteAttachment:
    """Metadata-only attachment reference; binary content lives outside ordinary storage."""

    id: str
    filename: str
    media_type: str
    size_bytes: int
    content_ref: str

    def __post_init__(self) -> None:
        if (
            not self.id.strip()
            or len(self.id) > 160
            or not self.filename.strip()
            or len(self.filename) > 500
            or not self.media_type.strip()
            or len(self.media_type) > 200
            or not self.content_ref.strip()
            or len(self.content_ref) > 500
        ):
            raise ValidationError("note attachment metadata is invalid")
        if self.size_bytes < 0 or self.size_bytes > 10 * 1024 * 1024 * 1024:
            raise ValidationError("note attachment size is invalid")


@dataclass(frozen=True, slots=True)
class NoteField:
    """One named value inside a repeatable note content block."""

    name: str
    value: str

    def __post_init__(self) -> None:
        if not self.name.strip() or len(self.name) > 80:
            raise ValidationError("note field name is invalid")
        if not self.value.strip() or len(self.value) > 200_000:
            raise ValidationError("note field value is invalid")


@dataclass(frozen=True, slots=True)
class NoteContentBlock:
    """A freely named group of text and fields beneath one note entry."""

    name: str
    body: str = ""
    fields: tuple[NoteField, ...] = ()
    id: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip() or len(self.name) > 120:
            raise ValidationError("note content block name is invalid")
        if self.id and len(self.id) > 160:
            raise ValidationError("note content block id is invalid")
        if len(self.body) > 200_000 or (not self.body.strip() and not self.fields):
            raise ValidationError("note content block requires body or fields")
        names = [item.name for item in self.fields]
        if len(set(names)) != len(names):
            raise ValidationError("note field names must be unique within a block")


@dataclass(frozen=True, slots=True)
class Note:
    id: str
    owner_user_id: str
    title: str
    body: str
    category_path: tuple[str, ...] = ("未分类",)
    content_blocks: tuple[NoteContentBlock, ...] = ()
    creator_user_id: str | None = None
    tags: tuple[str, ...] = ()
    attachments: tuple[NoteAttachment, ...] = ()
    classification: DataClassification = DataClassification.PERSONAL
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.owner_user_id.strip():
            raise ValidationError("note id and owner are required")
        if self.creator_user_id is not None and not self.creator_user_id.strip():
            raise ValidationError("note creator must not be empty")
        if not self.title.strip() and not self.body.strip():
            raise ValidationError("note title or body is required")
        if not self.category_path or len(self.category_path) > 8:
            raise ValidationError("note category path is invalid")
        if any(not part.strip() or len(part) > 80 for part in self.category_path):
            raise ValidationError("note category path is invalid")
        block_ids = [item.id for item in self.content_blocks if item.id]
        if len(set(block_ids)) != len(block_ids):
            raise ValidationError("note content block ids must be unique")
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
        attachment_ids = [item.id for item in self.attachments]
        if len(set(attachment_ids)) != len(attachment_ids):
            raise ValidationError("note attachment ids must be unique")
        require_ordinary_storage(self.classification)
