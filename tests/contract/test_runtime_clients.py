from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest

from zhixu.adapters.channels import OutboundTargetStore
from zhixu.adapters.channels.email import EmailOutboundAdapter
from zhixu.adapters.storage.sqlite import Database
from zhixu.runtime.channel_qq import InternalChannelClient
from zhixu.runtime.outbound import OutboundBrokerClient, _adapter
from zhixu.security import FieldCipher, OpaqueReferenceFactory

SERVICE_TOKEN = "synthetic-service-token-that-is-long-enough"


@pytest.mark.parametrize(
    "factory",
    [InternalChannelClient, OutboundBrokerClient],
)
@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:8840",
        "http://provider.example.invalid",
        "http://user@127.0.0.1:8840",
        "http://127.0.0.1:8840?redirect=1",
        "http://127.0.0.1:8840/#fragment",
    ],
)
def test_internal_broker_clients_only_accept_plain_loopback_origins(
    factory: Callable[[str, str], object],
    origin: str,
) -> None:
    with pytest.raises(ValueError):
        factory(origin, SERVICE_TOKEN)


@pytest.mark.parametrize(
    "factory",
    [InternalChannelClient, OutboundBrokerClient],
)
def test_internal_broker_clients_disable_proxies_and_redirects(
    factory: Callable[[str, str], object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []

    class Opener:
        pass

    def build_opener(*handlers: object) -> Opener:
        captured.extend(handlers)
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    factory("http://127.0.0.1:8840", SERVICE_TOKEN)

    proxy = next(
        item for item in captured if isinstance(item, urllib.request.ProxyHandler)
    )
    assert proxy.proxies == {}
    assert any(type(item).__name__ == "_RejectRedirects" for item in captured)


def test_outbound_worker_uses_a_strict_credential_schema(tmp_path: Path) -> None:
    database = Database(tmp_path / "outbound.sqlite3")
    database.migrate()
    targets = OutboundTargetStore(
        database,
        FieldCipher(b"E" * 32),
        OpaqueReferenceFactory(b"R" * 32),
    )
    config = {
        "channel": "email",
        "channel_account": "email_synthetic",
        "host": "smtp.example.invalid",
        "port": 465,
        "sender": "sender@example.invalid",
        "username": "synthetic-user",
        "password": "synthetic-password",
        "implicit_tls": True,
    }
    path = tmp_path / "outbound.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    adapter = _adapter(path, targets)

    assert isinstance(adapter, EmailOutboundAdapter)
    assert adapter.channel_account == "email_synthetic"
    assert "synthetic-password" not in repr(adapter.credentials)

    config["unexpected"] = "rejected"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError):
        _adapter(path, targets)
