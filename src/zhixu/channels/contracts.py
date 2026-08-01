"""Normalized channel input and capability contracts."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from zhixu.domain import DataClassification
from zhixu.domain.agenda import require_aware
from zhixu.domain.errors import ValidationError


class ConversationKind(StrEnum):
    PRIVATE = "private"
    GROUP = "group"
    CHANNEL = "channel"


class MessageKind(StrEnum):
    TEXT = "text"
    MARKDOWN = "markdown"
    BUTTON = "button"
    ATTACHMENT = "attachment"
    VOICE = "voice"


@dataclass(frozen=True, slots=True)
class ChannelCapabilities:
    inbound_text: bool = False
    outbound_text: bool = False
    markdown: bool = False
    proactive_push: bool = False
    buttons: bool = False
    attachments: bool = False
    voice: bool = False
    groups: bool = False


@dataclass(frozen=True, slots=True)
class InboundEvent:
    event_id: str
    channel: str
    channel_account: str
    external_actor_ref: str
    external_conversation_ref: str
    conversation_kind: ConversationKind
    message_kind: MessageKind
    received_at: datetime
    text: str | None = field(default=None, repr=False)
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        required = (
            self.event_id,
            self.channel,
            self.channel_account,
            self.external_actor_ref,
            self.external_conversation_ref,
        )
        if any(not value.strip() for value in required):
            raise ValidationError("inbound event references must not be empty")
        require_aware(self.received_at, "received_at")
        if self.message_kind is MessageKind.TEXT and not (self.text or "").strip():
            raise ValidationError("text events require non-empty text")


@dataclass(frozen=True, slots=True)
class MessageButton:
    label: str
    action: str

    def __post_init__(self) -> None:
        if not self.label.strip() or not self.action.strip():
            raise ValidationError("button label and action are required")


@dataclass(frozen=True, slots=True)
class CalendarPreview:
    """Privacy-minimized data needed to render a monthly calendar image."""

    year: int
    month: int
    busy_day_counts: tuple[tuple[int, int], ...] = field(default_factory=tuple)
    today_day: int | None = None

    def __post_init__(self) -> None:
        if not 1970 <= self.year <= 2100 or not 1 <= self.month <= 12:
            raise ValidationError("calendar preview month is invalid")
        maximum_day = monthrange(self.year, self.month)[1]
        if self.today_day is not None and not 1 <= self.today_day <= maximum_day:
            raise ValidationError("calendar preview today is invalid")
        days = [day for day, _count in self.busy_day_counts]
        if (
            len(days) != len(set(days))
            or days != sorted(days)
            or any(
                not 1 <= day <= maximum_day or not 1 <= count <= 999
                for day, count in self.busy_day_counts
            )
        ):
            raise ValidationError("calendar preview busy-day counts are invalid")


@dataclass(frozen=True, slots=True)
class DailyAgendaPreview:
    """Privacy-minimized timeline data for a daily briefing image."""

    year: int
    month: int
    day: int
    entries: tuple[tuple[int, int, str], ...] = field(default_factory=tuple)
    anniversary_day_numbers: tuple[int, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        try:
            datetime(self.year, self.month, self.day)
        except ValueError as exc:
            raise ValidationError("daily agenda preview date is invalid") from exc
        if any(
            not 0 <= start < 1440
            or not start < end <= 1440
            or kind not in {"agenda", "reminder"}
            for start, end, kind in self.entries
        ):
            raise ValidationError("daily agenda preview entry is invalid")
        if any(value < 1 for value in self.anniversary_day_numbers):
            raise ValidationError("daily agenda anniversary count is invalid")


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    channel: str
    channel_account: str
    target_ref: str
    kind: MessageKind
    text: str = field(repr=False)
    buttons: tuple[MessageButton, ...] = field(default_factory=tuple, repr=False)
    attachment_url: str | None = field(default=None, repr=False)
    reply_context_ref: str = field(default="", repr=False)
    classification: DataClassification = DataClassification.PERSONAL
    calendar_preview: CalendarPreview | None = field(default=None, repr=False)
    daily_agenda_preview: DailyAgendaPreview | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.channel.strip() or not self.channel_account.strip():
            raise ValidationError("outbound channel and account are required")
        if not self.target_ref.strip():
            raise ValidationError("outbound target_ref is required")
        if (
            not self.text.strip()
            and self.attachment_url is None
            and self.calendar_preview is None
            and self.daily_agenda_preview is None
        ):
            raise ValidationError("outbound message content is required")
        if len(self.reply_context_ref) > 160:
            raise ValidationError("reply context reference is too long")
        if self.classification > DataClassification.CONFIDENTIAL:
            raise ValidationError("high-sensitivity data cannot enter channel output")


@dataclass(frozen=True, slots=True)
class ChannelDeliveryResult:
    ok: bool
    retryable: bool = False
    provider_code: str = ""
    provider_message_id: str = ""
