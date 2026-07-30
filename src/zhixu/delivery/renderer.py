"""Capability-aware message degradation."""

from __future__ import annotations

from dataclasses import replace

from zhixu.channels import ChannelCapabilities, MessageKind, OutboundMessage
from zhixu.domain.errors import ValidationError


def render_for_capabilities(
    message: OutboundMessage,
    capabilities: ChannelCapabilities,
) -> OutboundMessage:
    if not capabilities.outbound_text:
        raise ValidationError("channel does not support outbound text")
    text = message.text
    buttons = message.buttons
    attachment_url = message.attachment_url
    kind = message.kind
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
    return replace(
        message,
        kind=kind,
        text=text,
        buttons=buttons,
        attachment_url=attachment_url,
    )
