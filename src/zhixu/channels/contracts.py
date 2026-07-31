"""Normalized channel input and capability contracts."""

from __future__ import annotations

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

    def __post_init__(self) -> None:
        if not self.channel.strip() or not self.channel_account.strip():
            raise ValidationError("outbound channel and account are required")
        if not self.target_ref.strip():
            raise ValidationError("outbound target_ref is required")
        if not self.text.strip() and self.attachment_url is None:
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
