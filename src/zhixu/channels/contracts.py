"""Normalized channel input and capability contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ConversationKind(StrEnum):
    PRIVATE = "private"
    GROUP = "group"
    CHANNEL = "channel"


class MessageKind(StrEnum):
    TEXT = "text"
    BUTTON = "button"
    ATTACHMENT = "attachment"
    VOICE = "voice"


@dataclass(frozen=True, slots=True)
class ChannelCapabilities:
    inbound_text: bool = False
    outbound_text: bool = False
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
    text: str | None = None
