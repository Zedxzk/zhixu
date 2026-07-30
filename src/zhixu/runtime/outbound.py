"""Outbound-only channel worker with an isolated encrypted target database."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import signal
import threading
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

from zhixu.adapters.channels import OutboundTargetResolver
from zhixu.adapters.channels.email import EmailCredentials, EmailOutboundAdapter
from zhixu.adapters.channels.webhook import (
    WebhookCredentials,
    WebhookEgressPolicy,
    WebhookOutboundAdapter,
)
from zhixu.adapters.channels.wecom import WeComCredentials, WeComOutboundAdapter
from zhixu.adapters.storage.sqlite import Database
from zhixu.channels import (
    ChannelDeliveryResult,
    MessageButton,
    MessageKind,
    OutboundMessage,
)
from zhixu.domain import DataClassification
from zhixu.ports import ChannelAdapter
from zhixu.security import FieldCipher

from .common import configure_logging, read_key_file, read_text_credential

logger = logging.getLogger(__name__)
MAX_CONFIG_BYTES = 64 * 1024


class OutboundBrokerClient:
    def __init__(self, base_url: str, service_token: str) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.path not in {"", "/"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("outbound broker must be a loopback HTTP origin")
        if len(service_token) < 32:
            raise ValueError("outbound broker service token is too short")
        self.base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _RejectRedirects(),
        )

    def claim(
        self,
        *,
        channel: str,
        channel_account: str,
    ) -> dict[str, object] | None:
        value = self._post(
            "/internal/channel/delivery/claim",
            {
                "channel": channel,
                "channel_account": channel_account,
                "worker_id": f"outbound:{channel}:{channel_account}",
            },
        )
        delivery = value.get("delivery")
        return delivery if isinstance(delivery, dict) else None

    def complete(
        self,
        delivery: dict[str, object],
        result: ChannelDeliveryResult,
    ) -> None:
        self._post(
            "/internal/channel/delivery/complete",
            {
                "delivery_id": str(delivery["id"]),
                "lease_token": str(delivery["lease_token"]),
                "ok": result.ok,
                "retryable": result.retryable,
                "provider_code": result.provider_code,
                "provider_message_id": result.provider_message_id,
            },
        )

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._service_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=10) as response:
                raw = response.read(1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            exc.read(4096)
            raise RuntimeError("outbound broker rejected a request") from exc
        if len(raw) > 1024 * 1024:
            raise RuntimeError("outbound broker response is too large")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise RuntimeError("outbound broker returned an invalid response")
        return value


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        _request: urllib.request.Request,
        _file_pointer: object,
        _code: int,
        _message: str,
        _headers: object,
        _new_url: str,
    ) -> None:
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zhixu-outbound")
    parser.add_argument("--database", required=True)
    parser.add_argument("--config-file", required=True)
    parser.add_argument("--field-key-file", required=True)
    parser.add_argument("--channel-service-token-file", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8840")
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--log-level", default="INFO")
    return parser


def _adapter(
    config_path: str | Path,
    targets: OutboundTargetResolver,
) -> ChannelAdapter:
    raw = Path(config_path).read_bytes()
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("outbound channel credential is too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("outbound channel credential must be an object")
    channel = value.get("channel")
    if channel == "wecom":
        _exact(
            value,
            {
                "channel",
                "channel_account",
                "corp_id",
                "agent_id",
                "secret",
            },
        )
        return WeComOutboundAdapter(
            WeComCredentials(
                _text(value, "channel_account", 160),
                _text(value, "corp_id", 512),
                _integer(value, "agent_id", 1, 2_147_483_647),
                _text(value, "secret", 4096),
            ),
            targets,
        )
    if channel == "email":
        _exact(
            value,
            {
                "channel",
                "channel_account",
                "host",
                "port",
                "sender",
                "username",
                "password",
                "implicit_tls",
            },
        )
        implicit_tls = value.get("implicit_tls")
        if not isinstance(implicit_tls, bool):
            raise ValueError("email implicit_tls must be a boolean")
        return EmailOutboundAdapter(
            EmailCredentials(
                _text(value, "channel_account", 160),
                _text(value, "host", 253),
                _integer(value, "port", 1, 65535),
                _text(value, "sender", 512),
                _text(value, "username", 512, allow_empty=True),
                _text(value, "password", 4096, allow_empty=True),
                implicit_tls,
            ),
            targets,
        )
    if channel == "webhook":
        _exact(
            value,
            {
                "channel",
                "channel_account",
                "signing_key_base64",
                "allowed_hosts",
                "allowed_ip_networks",
            },
        )
        hosts = _strings(value, "allowed_hosts", maximum=253)
        networks = _strings(value, "allowed_ip_networks", maximum=80)
        try:
            signing_key = base64.urlsafe_b64decode(
                _text(value, "signing_key_base64", 8192)
            )
        except Exception as exc:
            raise ValueError("webhook signing key encoding is invalid") from exc
        return WebhookOutboundAdapter(
            WebhookCredentials(
                _text(value, "channel_account", 160),
                signing_key,
            ),
            targets,
            WebhookEgressPolicy(frozenset(hosts), tuple(networks)),
        )
    raise ValueError("outbound channel is unsupported")


def _exact(value: dict[str, object], fields: set[str]) -> None:
    if set(value) != fields:
        raise ValueError("outbound channel credential fields are invalid")


def _text(
    value: dict[str, object],
    key: str,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> str:
    selected = value.get(key)
    if (
        not isinstance(selected, str)
        or (not allow_empty and not selected)
        or len(selected) > maximum
        or "\0" in selected
    ):
        raise ValueError("outbound channel credential value is invalid")
    return selected


def _integer(
    value: dict[str, object],
    key: str,
    minimum: int,
    maximum: int,
) -> int:
    selected = value.get(key)
    if (
        not isinstance(selected, int)
        or isinstance(selected, bool)
        or not minimum <= selected <= maximum
    ):
        raise ValueError("outbound channel credential integer is invalid")
    return selected


def _strings(
    value: dict[str, object],
    key: str,
    *,
    maximum: int,
) -> tuple[str, ...]:
    selected = value.get(key)
    if (
        not isinstance(selected, list)
        or len(selected) > 100
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > maximum
            or "\0" in item
            for item in selected
        )
    ):
        raise ValueError("outbound channel credential list is invalid")
    return tuple(selected)


def _message(value: dict[str, object]) -> OutboundMessage:
    buttons_value = value.get("buttons")
    buttons = (
        tuple(
            MessageButton(str(item["label"]), str(item["action"]))
            for item in buttons_value
            if isinstance(item, dict)
        )
        if isinstance(buttons_value, list)
        else ()
    )
    return OutboundMessage(
        channel=str(value["channel"]),
        channel_account=str(value["channel_account"]),
        target_ref=str(value["target_ref"]),
        kind=MessageKind(str(value["kind"])),
        text=str(value.get("text") or ""),
        buttons=buttons,
        attachment_url=(
            str(value["attachment_url"]) if value.get("attachment_url") else None
        ),
        classification=DataClassification(int(value["classification"])),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    if not 0.05 <= args.interval <= 60:
        raise SystemExit("outbound worker interval is invalid")
    database = Database(Path(args.database))
    database.migrate()
    targets = OutboundTargetResolver(
        database,
        FieldCipher(read_key_file(args.field_key_file, exact_bytes=32)),
    )
    adapter = _adapter(args.config_file, targets)
    broker = OutboundBrokerClient(
        args.api_url,
        read_text_credential(args.channel_service_token_file),
    )
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    while not stop.is_set():
        try:
            delivery = broker.claim(
                channel=adapter.channel,
                channel_account=adapter.channel_account,
            )
            if delivery is None:
                stop.wait(args.interval)
                continue
            try:
                result = adapter.send(_message(delivery))
            except Exception:
                result = ChannelDeliveryResult(False, True, "adapter_exception")
            broker.complete(delivery, result)
        except Exception as exc:
            logger.warning("outbound broker_failed error=%s", type(exc).__name__)
            stop.wait(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
