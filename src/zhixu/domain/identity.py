"""Internal users and channel-independent identity bindings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .errors import ValidationError


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class User:
    id: str
    display_name: str
    status: UserStatus
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValidationError("user id is required")
        if not self.display_name.strip():
            raise ValidationError("display name is required")
        if self.created_at.tzinfo is None:
            raise ValidationError("created_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class EncryptedIdentifier:
    """An external platform identifier already encrypted by a boundary adapter."""

    value: str

    def __post_init__(self) -> None:
        if not self.value.startswith("enc:"):
            raise ValidationError("external identifiers must be encrypted before storage")


@dataclass(frozen=True, slots=True)
class ExternalIdentity:
    id: str
    user_id: str
    channel: str
    channel_account: str
    encrypted_subject: EncryptedIdentifier
    opaque_ref: str
    created_at: datetime

    def __post_init__(self) -> None:
        required = (
            self.id,
            self.user_id,
            self.channel,
            self.channel_account,
            self.opaque_ref,
        )
        if any(not value.strip() for value in required):
            raise ValidationError("identity fields must not be empty")
        if self.created_at.tzinfo is None:
            raise ValidationError("created_at must be timezone-aware")
