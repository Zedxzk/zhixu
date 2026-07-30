"""Dedicated QQ network process with no access to the domain database."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from zhixu.adapters.channels.qq import (
    QQBotCredentials,
    QQContactStore,
    QQGatewayProtocol,
    QQGatewayRunner,
    QQHttpAdapter,
)
from zhixu.adapters.channels.qq.gateway import QQEventMapper, QQGatewaySessionStore
from zhixu.adapters.storage.sqlite import Database
from zhixu.channels import (
    ChannelDeliveryResult,
    InboundEvent,
    MessageButton,
    MessageKind,
    OutboundMessage,
)
from zhixu.domain import DataClassification
from zhixu.security import FieldCipher, OpaqueReferenceFactory

from .common import configure_logging, read_key_file, read_text_credential

logger = logging.getLogger(__name__)


class InternalChannelClient:
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
            raise ValueError("internal channel API must be a loopback HTTP origin")
        if len(service_token) < 32:
            raise ValueError("internal channel service token is too short")
        self.base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _RejectRedirects(),
        )

    def event(self, event: InboundEvent) -> None:
        self._post(
            "/internal/channel/event",
            {
                "event_id": event.event_id,
                "channel": event.channel,
                "channel_account": event.channel_account,
                "actor_ref": event.external_actor_ref,
                "conversation_ref": event.external_conversation_ref,
                "conversation_kind": event.conversation_kind.value,
                "message_kind": event.message_kind.value,
                "text": event.text,
                "received_at": event.received_at.isoformat(),
                "mentioned": bool(event.metadata.get("mentioned")),
            },
        )

    def claim(self, *, channel_account: str) -> dict[str, object] | None:
        response = self._post(
            "/internal/channel/delivery/claim",
            {
                "channel": "qq",
                "channel_account": channel_account,
                "worker_id": f"qq:{channel_account}",
            },
        )
        delivery = response.get("delivery")
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
            raise RuntimeError("internal channel API rejected a request") from exc
        if len(raw) > 1024 * 1024:
            raise RuntimeError("internal channel API response is too large")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise RuntimeError("internal channel API returned an invalid response")
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
    parser = argparse.ArgumentParser(prog="zhixu-qq")
    parser.add_argument("--database", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--app-id-file", required=True)
    parser.add_argument("--client-secret-file", required=True)
    parser.add_argument("--field-key-file", required=True)
    parser.add_argument("--reference-key-file", required=True)
    parser.add_argument("--channel-service-token-file", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8840")
    parser.add_argument("--delivery-interval", type=float, default=0.25)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    if not 0.05 <= args.delivery_interval <= 10:
        raise SystemExit("QQ delivery interval is invalid")
    database = Database(Path(args.database))
    database.migrate()
    cipher = FieldCipher(read_key_file(args.field_key_file, exact_bytes=32))
    references = OpaqueReferenceFactory(read_key_file(args.reference_key_file))
    contacts = QQContactStore(database, cipher, references)
    contacts.register_account(
        args.account,
        label="QQ official bot",
        config_ref="systemd-credentials",
        now=datetime.now(UTC),
    )
    adapter = QQHttpAdapter(
        QQBotCredentials(
            args.account,
            read_text_credential(args.app_id_file),
            read_text_credential(args.client_secret_file),
        ),
        contacts,
    )
    internal = InternalChannelClient(
        args.api_url,
        read_text_credential(args.channel_service_token_file),
    )

    def on_event(event: InboundEvent) -> None:
        internal.event(event)

    protocol = QQGatewayProtocol(
        channel_account=args.account,
        mapper=QQEventMapper(args.account, contacts),
        session_store=QQGatewaySessionStore(database, cipher),
        on_event=on_event,
    )
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    def deliver() -> None:
        while not stop.is_set():
            try:
                value = internal.claim(channel_account=args.account)
                if value is None:
                    stop.wait(args.delivery_interval)
                    continue
                message = _message(value)
                try:
                    result = adapter.send(message)
                except Exception:
                    result = ChannelDeliveryResult(False, True, "adapter_exception")
                internal.complete(value, result)
            except Exception as exc:
                logger.warning("qq_delivery broker_failed error=%s", type(exc).__name__)
                stop.wait(1)

    delivery_thread = threading.Thread(
        target=deliver,
        name="zhixu-qq-delivery",
        daemon=True,
    )
    delivery_thread.start()
    try:
        QQGatewayRunner(adapter, protocol).run(stop)
    finally:
        stop.set()
        delivery_thread.join(timeout=5)
    return 0


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


if __name__ == "__main__":
    raise SystemExit(main())
