"""QQ token acquisition and proactive HTTP delivery."""

from __future__ import annotations

import re
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from zhixu.channels import (
    ChannelCapabilities,
    ChannelDeliveryResult,
    MessageButton,
    MessageKind,
    OutboundMessage,
)
from zhixu.domain import DataClassification
from zhixu.domain.errors import ValidationError

from ..http import JsonTransport, UrllibJsonTransport
from .contacts import QQContactStore, ResolvedQQTarget

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
_KEYBOARD_COLUMNS = 4
_KEYBOARD_MAX_ROWS = 5


def _button_payload(button: MessageButton, index: int) -> dict[str, Any]:
    label = button.label
    action = button.action
    return {
        "id": f"zhixu-{index}",
        "render_data": {
            "label": label,
            "visited_label": label,
            "style": 1 if action.startswith("/提醒完成 ") else 0,
        },
        "action": {
            "type": 1,
            "data": action,
            "permission": {"type": 2},
            "unsupport_tips": "请发送对应文字命令",
        },
    }


def _plain_button_fallback(message: OutboundMessage) -> dict[str, Any]:
    text = re.sub(r"(?m)^#{1,6}\s*", "", message.text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"\\([\\`*_{}\[\]()#+\-.!>|])", r"\1", text)
    choices = "\n".join(
        f"{index}. {button.label} — {button.action}"
        for index, button in enumerate(message.buttons, start=1)
    )
    return {"content": f"{text}\n\n{choices}".strip(), "msg_type": 0}


@dataclass(frozen=True, slots=True)
class QQBotCredentials:
    channel_account: str
    app_id: str = field(repr=False)
    client_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.channel_account, self.app_id, self.client_secret)
        ):
            raise ValidationError("QQ bot credentials are incomplete")


class QQHttpAdapter:
    channel = "qq"
    capabilities = ChannelCapabilities(
        inbound_text=True,
        outbound_text=True,
        markdown=True,
        proactive_push=True,
        buttons=True,
        attachments=True,
        groups=True,
    )

    def __init__(
        self,
        credentials: QQBotCredentials,
        contacts: QQContactStore,
        *,
        transport: JsonTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.credentials = credentials
        self.contacts = contacts
        self.transport = transport or UrllibJsonTransport()
        self.clock = clock or (lambda: datetime.now(UTC))
        self._token_lock = threading.Lock()
        self._token = ""
        self._token_expires_at = datetime.min.replace(tzinfo=UTC)

    @property
    def channel_account(self) -> str:
        return self.credentials.channel_account

    def access_token(self, *, now: datetime | None = None) -> str:
        current = now or datetime.now(UTC)
        with self._token_lock:
            if self._token and current < self._token_expires_at:
                return self._token
            status, value = self.transport.request(
                TOKEN_URL,
                method="POST",
                payload={
                    "appId": self.credentials.app_id,
                    "clientSecret": self.credentials.client_secret,
                },
            )
            token = str(value.get("access_token") or "")
            if status != 200 or not token:
                raise RuntimeError("QQ token request failed")
            expires_in = max(120, int(value.get("expires_in") or 7200))
            self._token = token
            self._token_expires_at = current + timedelta(seconds=expires_in - 60)
            return token

    @staticmethod
    def _message_endpoint(target: ResolvedQQTarget) -> str:
        identifier = urllib.parse.quote(target.identifier, safe="")
        if target.kind == "private":
            return f"{API_BASE}/v2/users/{identifier}/messages"
        if target.kind == "group":
            return f"{API_BASE}/v2/groups/{identifier}/messages"
        if target.kind == "channel":
            return f"{API_BASE}/channels/{identifier}/messages"
        raise ValidationError("QQ target kind cannot receive messages")

    @staticmethod
    def _file_endpoint(target: ResolvedQQTarget) -> str:
        identifier = urllib.parse.quote(target.identifier, safe="")
        if target.kind == "private":
            return f"{API_BASE}/v2/users/{identifier}/files"
        if target.kind == "group":
            return f"{API_BASE}/v2/groups/{identifier}/files"
        raise ValidationError("QQ channel attachments use a different upload flow")

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": self.credentials.app_id,
        }

    def acknowledge_interaction(self, interaction_id: str) -> None:
        if not interaction_id.strip():
            raise ValidationError("QQ interaction id is required")
        token = self.access_token()
        identifier = urllib.parse.quote(interaction_id, safe="")
        status, _value = self.transport.request(
            f"{API_BASE}/interactions/{identifier}",
            method="PUT",
            payload={"code": 0},
            headers=self._headers(token),
        )
        if status < 200 or status >= 300:
            raise RuntimeError("QQ interaction acknowledgement failed")

    def send(self, message: OutboundMessage) -> ChannelDeliveryResult:
        if (
            message.channel != self.channel
            or message.channel_account != self.channel_account
        ):
            return ChannelDeliveryResult(False, False, "account_mismatch")
        if message.classification >= DataClassification.CONFIDENTIAL:
            return ChannelDeliveryResult(False, False, "classification_blocked")
        target = self.contacts.resolve(message.channel_account, message.target_ref)
        reply_context = (
            self.contacts.resolve_reply_context(
                message.channel_account,
                message.reply_context_ref,
                target_ref=message.target_ref,
                now=self.clock(),
            )
            if message.reply_context_ref
            else None
        )
        if message.reply_context_ref and reply_context is None:
            self.contacts.remove_reply_context(
                message.channel_account,
                message.reply_context_ref,
            )
            return ChannelDeliveryResult(False, False, "reply_context_unavailable")
        token = self.access_token()
        payload: dict[str, Any] = {"content": message.text}
        if target.kind in {"private", "group"}:
            payload["msg_type"] = 0
        if (
            message.buttons
            and len(message.buttons) > _KEYBOARD_COLUMNS * _KEYBOARD_MAX_ROWS
        ):
            return ChannelDeliveryResult(False, False, "keyboard_too_large")
        if message.kind is MessageKind.MARKDOWN or message.buttons:
            button_values = [
                _button_payload(button, index)
                for index, button in enumerate(message.buttons, start=1)
            ]
            payload = {"markdown": {"content": message.text}}
            if target.kind in {"private", "group"}:
                payload["msg_type"] = 2
            if button_values:
                payload["keyboard"] = {
                    "content": {
                        "rows": [
                            {"buttons": button_values[start : start + _KEYBOARD_COLUMNS]}
                            for start in range(0, len(button_values), _KEYBOARD_COLUMNS)
                        ]
                    }
                }
        if message.attachment_url:
            try:
                file_url = self._file_endpoint(target)
            except ValidationError:
                payload["image"] = message.attachment_url
            else:
                media_status, media_value = self.transport.request(
                    file_url,
                    method="POST",
                    payload={
                        "file_type": 1,
                        "url": message.attachment_url,
                        "srv_send_msg": False,
                    },
                    headers=self._headers(token),
                )
                file_info = str(media_value.get("file_info") or "")
                if media_status != 200 or not file_info:
                    return ChannelDeliveryResult(
                        False,
                        media_status >= 500 or media_status == 429,
                        f"http_{media_status}",
                    )
                payload = {"msg_type": 7, "media": {"file_info": file_info}}
            if message.kind is MessageKind.ATTACHMENT and message.text:
                payload.setdefault("content", message.text)
        if reply_context is not None:
            payload[reply_context.field] = reply_context.identifier
            payload["msg_seq"] = 1
        status, value = self.transport.request(
            self._message_endpoint(target),
            method="POST",
            payload=payload,
            headers=self._headers(token),
        )
        if (
            status in {400, 403}
            and (message.kind is MessageKind.MARKDOWN or message.buttons)
            and not message.attachment_url
        ):
            fallback = _plain_button_fallback(message)
            if reply_context is not None:
                fallback[reply_context.field] = reply_context.identifier
                fallback["msg_seq"] = 2
            status, value = self.transport.request(
                self._message_endpoint(target),
                method="POST",
                payload=fallback,
                headers=self._headers(token),
            )
        if 200 <= status < 300:
            if message.reply_context_ref:
                self.contacts.remove_reply_context(
                    message.channel_account,
                    message.reply_context_ref,
                )
            return ChannelDeliveryResult(
                True,
                provider_code="ok",
                provider_message_id=str(value.get("id") or ""),
            )
        retryable = status == 429 or status >= 500
        if message.reply_context_ref and not retryable:
            self.contacts.remove_reply_context(
                message.channel_account,
                message.reply_context_ref,
            )
        return ChannelDeliveryResult(
            False,
            retryable=retryable,
            provider_code=f"http_{status}",
        )
