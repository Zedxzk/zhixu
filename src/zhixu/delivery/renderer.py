"""Capability-aware message degradation."""

from __future__ import annotations

import re
from dataclasses import replace

from zhixu.channels import ChannelCapabilities, MessageKind, OutboundMessage
from zhixu.domain.errors import ValidationError


def _plain_markdown(value: str) -> str:
    text = re.sub(r"(?m)^#{1,6}\s*", "", value)
    text = text.replace("**", "").replace("__", "")
    return re.sub(r"\\([\\`*_{}\[\]()#+\-.!>|])", r"\1", text)


def render_for_capabilities(
    message: OutboundMessage,
    capabilities: ChannelCapabilities,
) -> OutboundMessage:
    if not capabilities.outbound_text:
        raise ValidationError("channel does not support outbound text")
    text = message.text
    buttons = message.buttons
    attachment_url = message.attachment_url
    calendar_preview = message.calendar_preview
    daily_agenda_preview = message.daily_agenda_preview
    kind = message.kind
    if kind is MessageKind.MARKDOWN and not capabilities.markdown:
        text = _plain_markdown(text)
        kind = MessageKind.TEXT
    if buttons and not capabilities.buttons:
        choices = "\n".join(
            f"{index}. {button.label} — {button.action}"
            for index, button in enumerate(buttons, start=1)
        )
        text = f"{text}\n{choices}".strip()
        buttons = ()
        kind = MessageKind.TEXT
    if attachment_url and not capabilities.attachments:
        text = f"{text}\n附件：{attachment_url}".strip()
        attachment_url = None
        kind = MessageKind.TEXT
    if calendar_preview and not capabilities.attachments:
        calendar_preview = None
    if daily_agenda_preview and not capabilities.attachments:
        daily_agenda_preview = None
    return replace(
        message,
        kind=kind,
        text=text,
        buttons=buttons,
        attachment_url=attachment_url,
        calendar_preview=calendar_preview,
        daily_agenda_preview=daily_agenda_preview,
    )
