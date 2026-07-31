from __future__ import annotations

import json
import urllib.request
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from zhixu.adapters.channels import ChannelRegistry, OutboundTargetStore
from zhixu.adapters.channels.email import EmailCredentials, EmailOutboundAdapter
from zhixu.adapters.channels.http import UrllibJsonTransport
from zhixu.adapters.channels.webhook import (
    WebhookCredentials,
    WebhookEgressPolicy,
    WebhookOutboundAdapter,
)
from zhixu.adapters.channels.wecom import WeComCredentials, WeComOutboundAdapter
from zhixu.adapters.storage.sqlite import Database
from zhixu.channels import (
    ChannelCapabilities,
    MessageButton,
    MessageKind,
    OutboundMessage,
)
from zhixu.delivery import render_for_capabilities
from zhixu.domain import DataClassification
from zhixu.domain.errors import PermissionDenied, ValidationError
from zhixu.security import FieldCipher, OpaqueReferenceFactory

NOW = datetime(2026, 7, 30, 12, tzinfo=UTC)


@pytest.fixture
def target_store(tmp_path: Path) -> tuple[OutboundTargetStore, Path]:
    path = tmp_path / "zhixu.sqlite3"
    database = Database(path)
    assert database.migrate() == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    return (
        OutboundTargetStore(
            database,
            FieldCipher(b"E" * 32),
            OpaqueReferenceFactory(b"R" * 32),
        ),
        tmp_path,
    )


class FakeWeComTransport:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10,
    ) -> tuple[int, dict[str, Any]]:
        del headers, timeout
        self.requests.append((url, method, payload))
        if "gettoken" in url:
            return 200, {"errcode": 0, "access_token": "synthetic_access", "expires_in": 7200}
        return 200, {"errcode": 0, "msgid": "synthetic_message"}


class FakeSMTPTransport:
    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    def send(
        self,
        message: EmailMessage,
        *,
        credentials: EmailCredentials,
        timeout: float,
    ) -> tuple[bool, str]:
        del credentials, timeout
        self.messages.append(message)
        return True, "ok"


class FixedResolver:
    def __init__(self, *addresses: str) -> None:
        self.addresses = addresses

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        del host, port
        return self.addresses


class FakeWebhookTransport:
    def __init__(self, status: int = 204) -> None:
        self.status = status
        self.calls: list[tuple[object, bytes, dict[str, str]]] = []

    def post(
        self,
        target: object,
        *,
        body: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> int:
        del timeout
        self.calls.append((target, body, headers))
        return self.status


def test_wecom_email_and_webhook_are_explicitly_outbound_only(
    target_store: tuple[OutboundTargetStore, Path],
) -> None:
    targets, _tmp_path = target_store
    wecom = WeComOutboundAdapter(
        WeComCredentials("wecom_synthetic", "corp_synthetic", 1001, "invalid-secret"),
        targets,
        transport=FakeWeComTransport(),
    )
    email = EmailOutboundAdapter(
        EmailCredentials(
            "email_synthetic",
            "smtp.example.invalid",
            465,
            "sender@example.invalid",
        ),
        targets,
        transport=FakeSMTPTransport(),
    )
    webhook = WebhookOutboundAdapter(
        WebhookCredentials("webhook_synthetic", b"S" * 32),
        targets,
        WebhookEgressPolicy(
            frozenset({"hooks.example.invalid"}),
            ("93.184.216.34/32",),
            resolver=FixedResolver("93.184.216.34"),
        ),
        transport=FakeWebhookTransport(),
    )
    registry = ChannelRegistry((wecom, email, webhook))

    assert registry.conversational() == []
    assert {item.mode for item in registry.describe()} == {"outbound-only"}
    assert all(not item.capabilities["inbound_text"] for item in registry.describe())


def test_fixed_provider_transport_disables_proxies_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b'{"ok":true}'

    class Opener:
        def open(self, _request: object, *, timeout: float) -> Response:
            assert timeout == 10
            return Response()

    def build_opener(*handlers: object) -> Opener:
        captured.extend(handlers)
        return Opener()

    monkeypatch.setattr("urllib.request.build_opener", build_opener)
    status, value = UrllibJsonTransport().request(
        "https://provider.example.invalid/fixed",
    )

    assert status == 200 and value == {"ok": True}
    proxy = next(
        item for item in captured if isinstance(item, urllib.request.ProxyHandler)
    )
    assert proxy.proxies == {}
    assert any(type(item).__name__ == "_RejectRedirects" for item in captured)


def test_wecom_delivery_uses_encrypted_target_and_fixed_provider(
    target_store: tuple[OutboundTargetStore, Path],
) -> None:
    targets, tmp_path = target_store
    external_user = "synthetic-wecom-user"
    opaque = targets.register(
        channel="wecom",
        channel_account="wecom_synthetic",
        kind="user",
        target=external_user,
        now=NOW,
    )
    transport = FakeWeComTransport()
    adapter = WeComOutboundAdapter(
        WeComCredentials("wecom_synthetic", "corp_synthetic", 1001, "invalid-secret"),
        targets,
        transport=transport,
    )
    result = adapter.send(
        OutboundMessage(
            "wecom",
            "wecom_synthetic",
            opaque,
            MessageKind.TEXT,
            "Synthetic notification",
        )
    )

    assert result.ok
    assert transport.requests[-1][2]["touser"] == external_user
    assert all("qyapi.weixin.qq.com" in url for url, _method, _payload in transport.requests)
    database_bytes = b"".join(
        path.read_bytes()
        for path in tmp_path.iterdir()
        if path.name.startswith("zhixu.sqlite3")
    )
    assert external_user.encode() not in database_bytes


def test_email_delivery_rejects_header_injection_and_confidential_output(
    target_store: tuple[OutboundTargetStore, Path],
) -> None:
    targets, _tmp_path = target_store
    opaque = targets.register(
        channel="email",
        channel_account="email_synthetic",
        kind="recipient",
        target="recipient@example.invalid",
        now=NOW,
    )
    transport = FakeSMTPTransport()
    adapter = EmailOutboundAdapter(
        EmailCredentials(
            "email_synthetic",
            "smtp.example.invalid",
            465,
            "sender@example.invalid",
        ),
        targets,
        transport=transport,
    )
    sent = adapter.send(
        OutboundMessage(
            "email",
            "email_synthetic",
            opaque,
            MessageKind.TEXT,
            "Synthetic notification",
        )
    )
    blocked = adapter.send(
        OutboundMessage(
            "email",
            "email_synthetic",
            opaque,
            MessageKind.TEXT,
            "Confidential synthetic notification",
            classification=DataClassification.CONFIDENTIAL,
        )
    )

    assert sent.ok
    assert transport.messages[0]["To"] == "recipient@example.invalid"
    assert not blocked.ok
    assert blocked.provider_code == "classification_blocked"
    assert len(transport.messages) == 1
    with pytest.raises(ValidationError):
        EmailCredentials(
            "email_synthetic",
            "smtp.example.invalid",
            465,
            "sender@example.invalid\nBcc: attacker@example.invalid",
        )


def test_webhook_policy_blocks_internal_dns_disallowed_hosts_and_redirects(
    target_store: tuple[OutboundTargetStore, Path],
) -> None:
    targets, _tmp_path = target_store
    private_policy = WebhookEgressPolicy(
        frozenset({"hooks.example.invalid"}),
        resolver=FixedResolver("127.0.0.1"),
    )
    with pytest.raises(PermissionDenied):
        private_policy.validate("https://hooks.example.invalid/callback")
    with pytest.raises(PermissionDenied):
        private_policy.validate("https://other.example.invalid/callback")

    opaque = targets.register(
        channel="webhook",
        channel_account="webhook_synthetic",
        kind="endpoint",
        target="https://hooks.example.invalid/callback",
        now=NOW,
    )
    transport = FakeWebhookTransport(status=302)
    adapter = WebhookOutboundAdapter(
        WebhookCredentials("webhook_synthetic", b"S" * 32),
        targets,
        WebhookEgressPolicy(
            frozenset({"hooks.example.invalid"}),
            ("93.184.216.34/32",),
            resolver=FixedResolver("93.184.216.34"),
        ),
        transport=transport,
    )
    result = adapter.send(
        OutboundMessage(
            "webhook",
            "webhook_synthetic",
            opaque,
            MessageKind.TEXT,
            "Synthetic webhook notification",
        )
    )

    assert not result.ok
    assert result.provider_code == "redirect_blocked"
    payload = json.loads(transport.calls[0][1])
    assert payload["event"] == "zhixu.notification"
    assert transport.calls[0][2]["X-Zhixu-Signature"].startswith("v1=")


@pytest.mark.parametrize(
    "capabilities",
    [
        ChannelCapabilities(outbound_text=True),
        ChannelCapabilities(outbound_text=True, proactive_push=True),
    ],
)
def test_capability_contract_degrades_buttons_and_attachments(
    capabilities: ChannelCapabilities,
) -> None:
    rendered = render_for_capabilities(
        OutboundMessage(
            "synthetic",
            "account_synthetic",
            "target_synthetic",
            MessageKind.ATTACHMENT,
            "Synthetic message",
            buttons=(MessageButton("Acknowledge", "/ack"),),
            attachment_url="https://assets.example.invalid/synthetic.png",
        ),
        capabilities,
    )

    assert rendered.kind is MessageKind.TEXT
    assert rendered.buttons == ()
    assert rendered.attachment_url is None
    assert "/ack" in rendered.text
    assert "https://assets.example.invalid/synthetic.png" in rendered.text
