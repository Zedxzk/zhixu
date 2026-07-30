"""Enterprise WeChat application-message delivery over fixed official endpoints."""

from __future__ import annotations

import threading
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from zhixu.adapters.channels.http import JsonTransport, UrllibJsonTransport
from zhixu.adapters.channels.targets import OutboundTargetResolver
from zhixu.channels import ChannelCapabilities, ChannelDeliveryResult, OutboundMessage
from zhixu.domain import DataClassification
from zhixu.domain.errors import ValidationError

TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"
TOKEN_RETRY_CODES = {40014, 42001}


@dataclass(frozen=True, slots=True)
class WeComCredentials:
    channel_account: str
    corp_id: str = field(repr=False)
    agent_id: int
    secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.channel_account.strip() or not self.corp_id.strip() or not self.secret:
            raise ValidationError("WeCom credentials are incomplete")
        if self.agent_id <= 0:
            raise ValidationError("WeCom agent id must be positive")


class WeComOutboundAdapter:
    channel = "wecom"
    capabilities = ChannelCapabilities(
        inbound_text=False,
        outbound_text=True,
        proactive_push=True,
    )

    def __init__(
        self,
        credentials: WeComCredentials,
        targets: OutboundTargetResolver,
        *,
        transport: JsonTransport | None = None,
    ) -> None:
        self.credentials = credentials
        self.targets = targets
        self.transport = transport or UrllibJsonTransport()
        self._token = ""
        self._token_expires_at = datetime.min.replace(tzinfo=UTC)
        self._lock = threading.Lock()

    @property
    def channel_account(self) -> str:
        return self.credentials.channel_account

    def _access_token(self, *, now: datetime) -> str:
        with self._lock:
            if self._token and now < self._token_expires_at:
                return self._token
            url = TOKEN_URL + "?" + urllib.parse.urlencode(
                {
                    "corpid": self.credentials.corp_id,
                    "corpsecret": self.credentials.secret,
                }
            )
            status, value = self.transport.request(url)
            token = str(value.get("access_token") or "")
            if status != 200 or int(value.get("errcode") or 0) != 0 or not token:
                raise RuntimeError("WeCom token request failed")
            expires = max(120, int(value.get("expires_in") or 7200))
            self._token = token
            self._token_expires_at = now + timedelta(seconds=expires - 60)
            return token

    def send(self, message: OutboundMessage) -> ChannelDeliveryResult:
        if (
            message.channel != self.channel
            or message.channel_account != self.channel_account
        ):
            return ChannelDeliveryResult(False, False, "account_mismatch")
        if message.classification >= DataClassification.CONFIDENTIAL:
            return ChannelDeliveryResult(False, False, "classification_blocked")
        target = self.targets.resolve(
            channel=self.channel,
            channel_account=self.channel_account,
            opaque_ref=message.target_ref,
        )
        if target.kind not in {"user", "party", "tag"}:
            return ChannelDeliveryResult(False, False, "target_kind_unsupported")
        target_key = {"user": "touser", "party": "toparty", "tag": "totag"}[target.kind]
        now = datetime.now(UTC)
        return self._send(message, target_key, target.value, now=now, allow_refresh=True)

    def _send(
        self,
        message: OutboundMessage,
        target_key: str,
        target_value: str,
        *,
        now: datetime,
        allow_refresh: bool,
    ) -> ChannelDeliveryResult:
        token = self._access_token(now=now)
        url = SEND_URL + "?" + urllib.parse.urlencode({"access_token": token})
        status, value = self.transport.request(
            url,
            method="POST",
            payload={
                target_key: target_value,
                "msgtype": "text",
                "agentid": self.credentials.agent_id,
                "text": {"content": message.text},
                "safe": 0,
            },
        )
        error_code = int(value.get("errcode") or 0)
        if 200 <= status < 300 and error_code == 0:
            return ChannelDeliveryResult(
                True,
                provider_code="ok",
                provider_message_id=str(value.get("msgid") or ""),
            )
        if allow_refresh and error_code in TOKEN_RETRY_CODES:
            with self._lock:
                self._token = ""
                self._token_expires_at = datetime.min.replace(tzinfo=UTC)
            return self._send(
                message,
                target_key,
                target_value,
                now=now,
                allow_refresh=False,
            )
        return ChannelDeliveryResult(
            False,
            retryable=status == 429 or status >= 500 or error_code == -1,
            provider_code=f"wecom_{error_code}" if error_code else f"http_{status}",
        )
