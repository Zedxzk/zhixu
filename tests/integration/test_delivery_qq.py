from __future__ import annotations

import base64
import json
import struct
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from zhixu.adapters.channels.qq import (
    InboundAdmission,
    InboundReceiptStore,
    QQBotCredentials,
    QQContactStore,
    QQGatewayProtocol,
    QQGatewayState,
    QQHttpAdapter,
)
from zhixu.adapters.channels.qq.gateway import (
    FULL_INTENTS,
    QQEventMapper,
    QQGatewaySessionStore,
)
from zhixu.adapters.storage.sqlite import Database, UserRepository
from zhixu.channels import (
    ButtonActionKind,
    CalendarPreview,
    ChannelCapabilities,
    ChannelDeliveryResult,
    ConversationKind,
    DailyAgendaPreview,
    InboundEvent,
    MessageButton,
    MessageKind,
    OutboundMessage,
)
from zhixu.delivery import (
    DeliveryWorker,
    OutboxStore,
    QuotaManager,
    QuotaRule,
    QuotaScope,
    render_for_capabilities,
)
from zhixu.delivery.quota import QuotaWindow
from zhixu.domain import (
    Action,
    CommandContext,
    EncryptedIdentifier,
    ExternalIdentity,
    PolicyEngine,
    ResourceRef,
    User,
    UserStatus,
)
from zhixu.domain.errors import ConflictError
from zhixu.security import FieldCipher, OpaqueReferenceFactory

NOW = datetime(2026, 6, 1, 8, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "zhixu.sqlite3")
    assert value.migrate() == list(range(1, 19))
    return value


@pytest.fixture
def privacy_primitives() -> tuple[FieldCipher, OpaqueReferenceFactory]:
    return FieldCipher(b"K" * 32), OpaqueReferenceFactory(b"R" * 32)


def create_user(database: Database, user_id: str = "user_test") -> UserRepository:
    users = UserRepository(database)
    user = User(user_id, "Synthetic User", UserStatus.ACTIVE, NOW)
    authorization = PolicyEngine().require(
        CommandContext(actor_user_id=user_id, now=NOW),
        Action.CREATE,
        ResourceRef("user", user_id, user_id),
    )
    users.create(user, authorization)
    return users


def register_account(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
    account_id: str = "bot_test_a",
) -> QQContactStore:
    cipher, references = privacy_primitives
    contacts = QQContactStore(database, cipher, references)
    contacts.register_account(
        account_id,
        label="Synthetic bot",
        config_ref="config_opaque_test",
        now=NOW,
    )
    return contacts


def test_outbox_recovers_expired_lease_and_dead_letters(
    database: Database,
) -> None:
    create_user(database)
    message = OutboundMessage(
        channel="qq",
        channel_account="bot_test_a",
        target_ref="qqc_synthetic",
        kind=MessageKind.TEXT,
        text="Synthetic outbound reminder",
    )
    outbox = OutboxStore(database, backoff_seconds=(5,))

    assert outbox.enqueue(
        delivery_id="delivery_test",
        idempotency_key="idempotency_test",
        owner_user_id="user_test",
        message=message,
        now=NOW,
    )
    assert not outbox.enqueue(
        delivery_id="delivery_duplicate",
        idempotency_key="idempotency_test",
        owner_user_id="user_test",
        message=message,
        now=NOW,
    )
    first = outbox.claim(worker_id="worker_a", now=NOW)
    assert first is not None
    recovered_at = NOW + timedelta(seconds=31)
    recovered = outbox.claim(worker_id="worker_b", now=recovered_at)
    assert recovered is not None
    assert recovered.attempts == 2
    assert recovered.lease_token != first.lease_token

    with pytest.raises(ConflictError):
        outbox.complete(first, ChannelDeliveryResult(True), now=recovered_at)

    assert (
        outbox.complete(
            recovered,
            ChannelDeliveryResult(False, True, "network_unavailable"),
            now=recovered_at,
        )
        == "retry_wait"
    )
    assert outbox.claim(worker_id="worker_b", now=recovered_at + timedelta(seconds=4)) is None
    final = outbox.claim(worker_id="worker_b", now=recovered_at + timedelta(seconds=5))
    assert final is not None
    assert (
        outbox.complete(
            final,
            ChannelDeliveryResult(False, False, "provider_rejected"),
            now=recovered_at + timedelta(seconds=5),
        )
        == "dead"
    )
    assert outbox.retry_dead(
        "dead_delivery_test",
        actor_user_id="user_test",
        now=recovered_at + timedelta(seconds=6),
    )
    assert outbox.get_status("delivery_test") == "pending"


def test_delivery_worker_composes_quota_rendering_and_adapter(
    database: Database,
) -> None:
    create_user(database)
    outbox = OutboxStore(database)
    assert outbox.enqueue(
        delivery_id="delivery_worker_test",
        idempotency_key="delivery_worker_idempotency",
        owner_user_id="user_test",
        message=OutboundMessage(
            channel="qq",
            channel_account="bot_test_a",
            target_ref="qqc_worker_test",
            kind=MessageKind.BUTTON,
            text="Synthetic worker message",
            buttons=(MessageButton("Run", "/run"),),
        ),
        now=NOW,
    )

    class TextOnlyAdapter:
        channel = "qq"
        channel_account = "bot_test_a"
        capabilities = ChannelCapabilities(outbound_text=True)

        def __init__(self) -> None:
            self.sent: list[OutboundMessage] = []

        def send(self, message: OutboundMessage) -> ChannelDeliveryResult:
            self.sent.append(message)
            if len(self.sent) == 1:
                return ChannelDeliveryResult(False, True, "network_unavailable")
            return ChannelDeliveryResult(True, provider_message_id="provider_test")

    adapter = TextOnlyAdapter()
    quota = QuotaManager(
        database,
        (
            QuotaRule("provider", QuotaWindow.SECOND, 5),
            QuotaRule("account", QuotaWindow.MINUTE, 5),
            QuotaRule("conversation", QuotaWindow.DAY, 5),
            QuotaRule("user", QuotaWindow.DAY, 5),
        ),
    )
    worker = DeliveryWorker(
        worker_id="worker_test",
        outbox=outbox,
        quota=quota,
        adapters=(adapter,),
    )

    assert worker.tick(now=NOW) == "retry_wait"
    assert worker.tick(now=NOW + timedelta(seconds=5)) == "sent"
    assert outbox.get_status("delivery_worker_test") == "sent"
    assert adapter.sent[0].kind is MessageKind.TEXT
    assert "/run" in adapter.sent[0].text


def test_quota_reservation_is_atomic_across_scopes(database: Database) -> None:
    manager = QuotaManager(
        database,
        (
            QuotaRule("provider", QuotaWindow.SECOND, 2),
            QuotaRule("account", QuotaWindow.MINUTE, 1),
            QuotaRule("conversation", QuotaWindow.DAY, 5),
            QuotaRule("user", QuotaWindow.DAY, 5),
        ),
    )

    def scopes(account: str) -> tuple[QuotaScope, ...]:
        return (
            QuotaScope("provider", "qq"),
            QuotaScope("account", account),
            QuotaScope("conversation", f"conversation_{account}"),
            QuotaScope("user", f"user_{account}"),
        )

    assert manager.reserve(scopes("a"), now=NOW).allowed
    blocked = manager.reserve(scopes("a"), now=NOW)
    assert not blocked.allowed
    assert "account:minute" in blocked.reason_code
    assert manager.reserve(scopes("b"), now=NOW).allowed
    assert not manager.reserve(scopes("c"), now=NOW).allowed


def test_same_identifier_is_isolated_per_bot_and_encrypted_at_rest(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(database, privacy_primitives, "bot_test_a")
    contacts.register_account(
        "bot_test_b",
        label="Second synthetic bot",
        config_ref="config_opaque_second",
        now=NOW,
    )
    raw_identifier = "openid-private-canary"
    first = contacts.record(
        channel_account="bot_test_a",
        kind="private",
        external_identifier=raw_identifier,
        now=NOW,
    )
    second = contacts.record(
        channel_account="bot_test_b",
        kind="private",
        external_identifier=raw_identifier,
        now=NOW,
    )

    assert first != second
    assert contacts.resolve("bot_test_a", first).identifier == raw_identifier
    assert contacts.resolve("bot_test_b", second).identifier == raw_identifier
    database_bytes = database.path.read_bytes()
    wal = database.path.with_name(database.path.name + "-wal")
    if wal.exists():
        database_bytes += wal.read_bytes()
    assert raw_identifier.encode() not in database_bytes


def test_inbound_admission_requires_binding_and_explicit_group_trigger(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    users = create_user(database)
    contacts = register_account(database, privacy_primitives)
    actor_ref = contacts.record(
        channel_account="bot_test_a",
        kind="actor",
        external_identifier="member-openid-canary",
        now=NOW,
    )
    conversation_ref = contacts.record(
        channel_account="bot_test_a",
        kind="group",
        external_identifier="group-openid-canary",
        now=NOW,
    )
    policy = PolicyEngine()
    identity = ExternalIdentity(
        id="identity_test",
        user_id="user_test",
        channel="qq",
        channel_account="bot_test_a",
        encrypted_subject=EncryptedIdentifier("enc:synthetic"),
        opaque_ref=actor_ref,
        created_at=NOW,
    )
    users.bind_identity(
        identity,
        policy.require(
            CommandContext(actor_user_id="user_test", now=NOW),
            Action.CREATE,
            ResourceRef("external_identity", identity.id, "user_test"),
        ),
    )
    admission = InboundAdmission(users, contacts)

    unknown_private = InboundEvent(
        event_id="event_private_unbound",
        channel="qq",
        channel_account="bot_test_a",
        external_actor_ref="qqc_unknown_actor",
        external_conversation_ref="qqc_unknown_actor",
        conversation_kind=ConversationKind.PRIVATE,
        message_kind=MessageKind.TEXT,
        received_at=NOW,
        text="/today",
    )
    assert admission.decide(unknown_private).reason_code == "identity_unbound"

    ordinary_group = InboundEvent(
        event_id="event_group_1",
        channel="qq",
        channel_account="bot_test_a",
        external_actor_ref=actor_ref,
        external_conversation_ref=conversation_ref,
        conversation_kind=ConversationKind.GROUP,
        message_kind=MessageKind.TEXT,
        received_at=NOW,
        text="ordinary chat",
    )
    assert admission.decide(ordinary_group).reason_code == "group_trigger_required"

    slash_group = InboundEvent(
        event_id="event_group_2",
        channel="qq",
        channel_account="bot_test_a",
        external_actor_ref=actor_ref,
        external_conversation_ref=conversation_ref,
        conversation_kind=ConversationKind.GROUP,
        message_kind=MessageKind.TEXT,
        received_at=NOW,
        text="/today",
    )
    assert admission.decide(slash_group).reason_code == "conversation_disabled"
    assert contacts.set_commands_enabled(
        "bot_test_a",
        conversation_ref,
        enabled=True,
    )
    assert admission.decide(slash_group).accepted


def test_inbound_body_is_not_persisted_or_exposed_by_repr(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    _cipher, references = privacy_primitives
    canary = "inbound-body-canary-do-not-persist"
    event = InboundEvent(
        event_id="event_private_test",
        channel="qq",
        channel_account="bot_test_a",
        external_actor_ref="qqc_actor_test",
        external_conversation_ref="qqc_private_test",
        conversation_kind=ConversationKind.PRIVATE,
        message_kind=MessageKind.TEXT,
        received_at=NOW,
        text=canary,
    )
    receipts = InboundReceiptStore(database, references)
    assert receipts.record(
        event,
        type("Decision", (), {"reason_code": "identity_unbound"})(),
    )
    assert canary not in repr(event)

    database_bytes = database.path.read_bytes()
    wal = database.path.with_name(database.path.name + "-wal")
    if wal.exists():
        database_bytes += wal.read_bytes()
    assert canary.encode() not in database_bytes


class FakeTransport:
    def __init__(self) -> None:
        self.requests: list[
            tuple[str, str, dict[str, Any] | None, dict[str, str] | None]
        ] = []

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10,
    ) -> tuple[int, dict[str, Any]]:
        del timeout
        self.requests.append((url, method, payload, headers))
        if url.endswith("getAppAccessToken"):
            return 200, {"access_token": "synthetic-token", "expires_in": 7200}
        if url.endswith("/files"):
            return 200, {"file_info": "synthetic-file"}
        return 200, {"id": "provider-message-test"}


def test_qq_access_token_is_cached_and_refreshed_before_expiry(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(database, privacy_primitives)
    transport = FakeTransport()
    adapter = QQHttpAdapter(
        QQBotCredentials("bot_test_a", "synthetic-app", "synthetic-secret"),
        contacts,
        transport=transport,
        clock=lambda: NOW,
    )

    assert adapter.access_token(now=NOW) == "synthetic-token"
    assert adapter.access_token(now=NOW + timedelta(hours=1)) == "synthetic-token"
    assert adapter.access_token(now=NOW + timedelta(hours=2)) == "synthetic-token"

    token_requests = [
        request for request in transport.requests if request[0].endswith("getAppAccessToken")
    ]
    assert len(token_requests) == 2


@pytest.mark.parametrize(
    ("status", "retryable"),
    ((400, False), (429, True), (503, True)),
)
def test_qq_http_classifies_permanent_and_retryable_provider_failures(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
    status: int,
    retryable: bool,
) -> None:
    contacts = register_account(database, privacy_primitives)
    target_ref = contacts.record(
        channel_account="bot_test_a",
        kind="private",
        external_identifier="private-openid-provider-failure-test",
        now=NOW,
    )

    class StatusTransport(FakeTransport):
        def request(
            self,
            url: str,
            *,
            method: str = "GET",
            payload: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
            timeout: float = 10,
        ) -> tuple[int, dict[str, Any]]:
            if url.endswith("getAppAccessToken"):
                return super().request(
                    url,
                    method=method,
                    payload=payload,
                    headers=headers,
                    timeout=timeout,
                )
            self.requests.append((url, method, payload, headers))
            return status, {}

    adapter = QQHttpAdapter(
        QQBotCredentials("bot_test_a", "synthetic-app", "synthetic-secret"),
        contacts,
        transport=StatusTransport(),
    )
    result = adapter.send(
        OutboundMessage(
            channel="qq",
            channel_account="bot_test_a",
            target_ref=target_ref,
            kind=MessageKind.TEXT,
            text="Synthetic provider failure",
        )
    )

    assert not result.ok
    assert result.retryable is retryable
    assert result.provider_code == f"http_{status}"


def test_qq_http_falls_back_to_plain_text_when_rich_success_has_no_message_id(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(database, privacy_primitives)
    target_ref = contacts.record(
        channel_account="bot_test_a",
        kind="group",
        external_identifier="group-openid-rich-fallback-test",
        now=NOW,
    )
    reply_ref = contacts.record_reply_context(
        channel_account="bot_test_a",
        target_ref=target_ref,
        external_context="synthetic-source-message",
        context_kind="msg_id",
        now=NOW,
    )

    class MissingIdTransport(FakeTransport):
        message_calls = 0

        def request(
            self,
            url: str,
            *,
            method: str = "GET",
            payload: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
            timeout: float = 10,
        ) -> tuple[int, dict[str, Any]]:
            if url.endswith("getAppAccessToken"):
                return super().request(
                    url,
                    method=method,
                    payload=payload,
                    headers=headers,
                    timeout=timeout,
                )
            self.requests.append((url, method, payload, headers))
            self.message_calls += 1
            if self.message_calls == 1:
                return 200, {"code": 1, "message": "synthetic rich rejection"}
            return 200, {"id": "provider-fallback-message-test"}

    transport = MissingIdTransport()
    adapter = QQHttpAdapter(
        QQBotCredentials("bot_test_a", "synthetic-app", "synthetic-secret"),
        contacts,
        transport=transport,
        clock=lambda: NOW,
    )
    result = adapter.send(
        OutboundMessage(
            channel="qq",
            channel_account="bot_test_a",
            target_ref=target_ref,
            kind=MessageKind.BUTTON,
            text="# Synthetic preview",
            buttons=(MessageButton("Accept", "/synthetic-accept"),),
            reply_context_ref=reply_ref,
        )
    )

    assert result.ok
    assert result.provider_message_id == "provider-fallback-message-test"
    rich_payload = transport.requests[-2][2]
    fallback_payload = transport.requests[-1][2]
    assert rich_payload is not None
    assert rich_payload["msg_type"] == 2
    assert rich_payload["msg_id"] == "synthetic-source-message"
    assert rich_payload["msg_seq"] == 1
    assert fallback_payload is not None
    assert fallback_payload["msg_type"] == 0
    assert fallback_payload["msg_id"] == "synthetic-source-message"
    assert fallback_payload["msg_seq"] == 2


def test_qq_http_does_not_report_text_without_a_provider_message_id_as_sent(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(database, privacy_primitives)
    target_ref = contacts.record(
        channel_account="bot_test_a",
        kind="private",
        external_identifier="private-openid-missing-provider-id-test",
        now=NOW,
    )

    class MissingIdTransport(FakeTransport):
        def request(
            self,
            url: str,
            *,
            method: str = "GET",
            payload: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
            timeout: float = 10,
        ) -> tuple[int, dict[str, Any]]:
            if url.endswith("getAppAccessToken"):
                return super().request(
                    url,
                    method=method,
                    payload=payload,
                    headers=headers,
                    timeout=timeout,
                )
            self.requests.append((url, method, payload, headers))
            return 200, {"code": 1, "message": "synthetic rejection"}

    result = QQHttpAdapter(
        QQBotCredentials("bot_test_a", "synthetic-app", "synthetic-secret"),
        contacts,
        transport=MissingIdTransport(),
    ).send(
        OutboundMessage(
            channel="qq",
            channel_account="bot_test_a",
            target_ref=target_ref,
            kind=MessageKind.TEXT,
            text="Synthetic provider response validation",
        )
    )

    assert not result.ok
    assert not result.retryable
    assert result.provider_code == "invalid_provider_response"


def test_qq_http_supports_image_upload(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(database, privacy_primitives)
    target_ref = contacts.record(
        channel_account="bot_test_a",
        kind="private",
        external_identifier="private-openid-http-test",
        now=NOW,
    )
    transport = FakeTransport()
    adapter = QQHttpAdapter(
        QQBotCredentials("bot_test_a", "synthetic-app", "synthetic-secret"),
        contacts,
        transport=transport,
    )
    result = adapter.send(
        OutboundMessage(
            channel="qq",
            channel_account="bot_test_a",
            target_ref=target_ref,
            kind=MessageKind.ATTACHMENT,
            text="Synthetic rich message",
            attachment_url="https://example.invalid/image.png",
        )
    )

    assert result.ok
    assert result.provider_message_id == "provider-message-test"
    assert any(request[0].endswith("/files") for request in transport.requests)
    final_payload = transport.requests[-1][2]
    assert final_payload is not None
    assert final_payload["media"]["file_info"] == "synthetic-file"


def test_qq_http_renders_calendar_as_private_base64_image_with_buttons(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(database, privacy_primitives)
    target_ref = contacts.record(
        channel_account="bot_test_a",
        kind="private",
        external_identifier="private-openid-calendar-image-test",
        now=NOW,
    )
    transport = FakeTransport()
    adapter = QQHttpAdapter(
        QQBotCredentials("bot_test_a", "synthetic-app", "synthetic-secret"),
        contacts,
        transport=transport,
    )

    result = adapter.send(
        OutboundMessage(
            channel="qq",
            channel_account="bot_test_a",
            target_ref=target_ref,
            kind=MessageKind.BUTTON,
            text="# 2026 年 6 月\n\n- `06-01 09:00` Synthetic",
            buttons=(MessageButton("下个月", "/日历 2026-07"),),
            calendar_preview=CalendarPreview(
                2026,
                6,
                busy_day_counts=((1, 2), (18, 1)),
                today_day=1,
            ),
        )
    )

    assert result.ok
    upload_payload = next(
        request[2] for request in transport.requests if request[0].endswith("/files")
    )
    assert upload_payload is not None
    assert "url" not in upload_payload
    png = base64.b64decode(upload_payload["file_data"], validate=True)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", png[16:24]) == (1120, 820)
    final_payload = transport.requests[-1][2]
    assert final_payload is not None
    assert final_payload["msg_type"] == 7
    assert final_payload["media"]["file_info"] == "synthetic-file"
    assert final_payload["keyboard"]["content"]["rows"][0]["buttons"][0][
        "action"
    ]["data"] == "/日历 2026-07"


def test_qq_calendar_falls_back_to_text_when_media_message_is_rejected(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(database, privacy_primitives)
    target_ref = contacts.record(
        channel_account="bot_test_a",
        kind="private",
        external_identifier="private-openid-calendar-fallback-test",
        now=NOW,
    )

    class RejectMediaTransport(FakeTransport):
        def request(
            self,
            url: str,
            *,
            method: str = "GET",
            payload: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
            timeout: float = 10,
        ) -> tuple[int, dict[str, Any]]:
            if payload is not None and payload.get("msg_type") == 7:
                self.requests.append((url, method, payload, headers))
                return 400, {}
            return super().request(
                url,
                method=method,
                payload=payload,
                headers=headers,
                timeout=timeout,
            )

    transport = RejectMediaTransport()
    adapter = QQHttpAdapter(
        QQBotCredentials("bot_test_a", "synthetic-app", "synthetic-secret"),
        contacts,
        transport=transport,
    )
    result = adapter.send(
        OutboundMessage(
            "qq",
            "bot_test_a",
            target_ref,
            MessageKind.BUTTON,
            "# Synthetic calendar",
            buttons=(MessageButton("今天", "/今天"),),
            calendar_preview=CalendarPreview(2026, 6, ((1, 1),), 1),
        )
    )

    assert result.ok
    assert any(
        request[2] is not None and request[2].get("msg_type") == 7
        for request in transport.requests
    )
    fallback = transport.requests[-1][2]
    assert fallback is not None
    assert fallback["msg_type"] == 0
    assert "/今天" in fallback["content"]


def test_calendar_preview_survives_outbox_without_storing_png(
    database: Database,
) -> None:
    create_user(database)
    preview = CalendarPreview(2026, 6, ((1, 2),), 1)
    outbox = OutboxStore(database)
    assert outbox.enqueue(
        delivery_id="delivery_calendar_test",
        idempotency_key="idempotency_calendar_test",
        owner_user_id="user_test",
        message=OutboundMessage(
            "qq",
            "bot_test_a",
            "qqc_calendar_test",
            MessageKind.BUTTON,
            "Synthetic calendar",
            calendar_preview=preview,
        ),
        now=NOW,
    )

    claimed = outbox.claim(worker_id="worker_calendar", now=NOW)
    assert claimed is not None
    assert claimed.message.calendar_preview == preview
    with database.connect() as connection:
        payload = str(
            connection.execute(
                "SELECT payload_json FROM outbox_deliveries WHERE id = ?",
                ("delivery_calendar_test",),
            ).fetchone()["payload_json"]
        )
    assert "file_data" not in payload
    assert "PNG" not in payload


def test_daily_agenda_preview_survives_outbox_and_uploads_png(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    create_user(database)
    contacts = register_account(database, privacy_primitives)
    target_ref = contacts.record(
        channel_account="bot_test_a",
        kind="private",
        external_identifier="private-openid-daily-agenda-test",
        now=NOW,
    )
    preview = DailyAgendaPreview(
        2026,
        8,
        28,
        entries=((540, 600, "agenda"), (720, 735, "reminder")),
        anniversary_day_numbers=(365,),
    )
    outbox = OutboxStore(database)
    assert outbox.enqueue(
        delivery_id="delivery_daily_agenda_test",
        idempotency_key="idempotency_daily_agenda_test",
        owner_user_id="user_test",
        message=OutboundMessage(
            "qq",
            "bot_test_a",
            target_ref,
            MessageKind.BUTTON,
            "# Synthetic daily briefing",
            buttons=(MessageButton("今天", "/今天"),),
            daily_agenda_preview=preview,
        ),
        now=NOW,
    )
    claimed = outbox.claim(worker_id="worker_daily", now=NOW)
    assert claimed is not None
    assert claimed.message.daily_agenda_preview == preview

    transport = FakeTransport()
    adapter = QQHttpAdapter(
        QQBotCredentials("bot_test_a", "synthetic-app", "synthetic-secret"),
        contacts,
        transport=transport,
    )
    assert adapter.send(claimed.message).ok
    upload_payload = next(
        request[2] for request in transport.requests if request[0].endswith("/files")
    )
    assert upload_payload is not None
    png = base64.b64decode(upload_payload["file_data"], validate=True)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    with database.connect() as connection:
        payload = str(
            connection.execute(
                "SELECT payload_json FROM outbox_deliveries WHERE id=?",
                ("delivery_daily_agenda_test",),
            ).fetchone()["payload_json"]
        )
    assert "file_data" not in payload


def test_qq_group_reply_uses_encrypted_message_context(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(database, privacy_primitives)
    target_ref = contacts.record(
        channel_account="bot_test_a",
        kind="group",
        external_identifier="group-openid-reply-context-canary",
        now=NOW,
    )
    reply_ref = contacts.record_reply_context(
        channel_account="bot_test_a",
        target_ref=target_ref,
        external_context="message-id-reply-context-canary",
        context_kind="msg_id",
        now=NOW,
    )
    transport = FakeTransport()
    adapter = QQHttpAdapter(
        QQBotCredentials("bot_test_a", "synthetic-app", "synthetic-secret"),
        contacts,
        transport=transport,
        clock=lambda: NOW,
    )

    result = adapter.send(
        OutboundMessage(
            channel="qq",
            channel_account="bot_test_a",
            target_ref=target_ref,
            kind=MessageKind.TEXT,
            text="Synthetic passive group reply",
            reply_context_ref=reply_ref,
        )
    )

    assert result.ok
    payload = transport.requests[-1][2]
    assert payload is not None
    assert payload["msg_id"] == "message-id-reply-context-canary"
    assert payload["msg_seq"] == 1
    assert (
        contacts.resolve_reply_context(
            "bot_test_a",
            reply_ref,
            target_ref=target_ref,
            now=NOW,
        )
        is None
    )
    database_bytes = database.path.read_bytes()
    wal = database.path.with_name(database.path.name + "-wal")
    if wal.exists():
        database_bytes += wal.read_bytes()
    assert b"message-id-reply-context-canary" not in database_bytes


def test_qq_http_sends_official_callback_keyboard_and_acknowledges_interaction(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(database, privacy_primitives)
    target_ref = contacts.record(
        channel_account="bot_test_a",
        kind="private",
        external_identifier="private-openid-button-test",
        now=NOW,
    )
    transport = FakeTransport()
    adapter = QQHttpAdapter(
        QQBotCredentials("bot_test_a", "synthetic-app", "synthetic-secret"),
        contacts,
        transport=transport,
    )

    result = adapter.send(
        OutboundMessage(
            channel="qq",
            channel_account="bot_test_a",
            target_ref=target_ref,
            kind=MessageKind.BUTTON,
            text="Synthetic reminder",
            buttons=(
                MessageButton("5 minutes", "/提醒稍后 reminder_synthetic 5分钟"),
                MessageButton("15 minutes", "/提醒稍后 reminder_synthetic 15分钟"),
                MessageButton("30 minutes", "/提醒稍后 reminder_synthetic 30分钟"),
                MessageButton("60 minutes", "/提醒稍后 reminder_synthetic 60分钟"),
                MessageButton("Complete", "/提醒完成 reminder_synthetic"),
                MessageButton("Cancel", "/取消提醒 reminder_synthetic"),
            ),
        )
    )
    adapter.acknowledge_interaction("synthetic-interaction")

    assert result.ok
    message_request = transport.requests[-2]
    assert message_request[1] == "POST"
    payload = message_request[2]
    assert payload is not None
    assert payload["msg_type"] == 2
    assert "content" not in payload
    assert payload["markdown"] == {"content": "Synthetic reminder"}
    rows = payload["keyboard"]["content"]["rows"]
    assert [len(row["buttons"]) for row in rows] == [4, 2]
    buttons = [button for row in rows for button in row["buttons"]]
    assert [button["id"] for button in buttons] == [
        "zhixu-1",
        "zhixu-2",
        "zhixu-3",
        "zhixu-4",
        "zhixu-5",
        "zhixu-6",
    ]
    assert buttons[0]["render_data"] == {
        "label": "5 minutes",
        "visited_label": "5 minutes",
        "style": 0,
    }
    assert buttons[0]["action"] == {
        "type": 1,
        "data": "/提醒稍后 reminder_synthetic 5分钟",
        "permission": {"type": 2},
        "unsupport_tips": "请发送对应文字命令",
    }
    assert buttons[4]["render_data"]["style"] == 1
    acknowledgement = transport.requests[-1]
    assert acknowledgement[0].endswith("/interactions/synthetic-interaction")
    assert acknowledgement[1] == "PUT"
    assert acknowledgement[2] == {"code": 0}


def test_qq_http_renders_generic_open_url_button(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(database, privacy_primitives)
    target_ref = contacts.record(
        channel_account="bot_test_a",
        kind="private",
        external_identifier="private-openid-link-button-test",
        now=NOW,
    )
    transport = FakeTransport()
    adapter = QQHttpAdapter(
        QQBotCredentials("bot_test_a", "synthetic-app", "synthetic-secret"),
        contacts,
        transport=transport,
    )
    link = "https://example.invalid/synthetic-meeting"

    assert adapter.send(
        OutboundMessage(
            channel="qq",
            channel_account="bot_test_a",
            target_ref=target_ref,
            kind=MessageKind.BUTTON,
            text="Synthetic linked reminder",
            buttons=(
                MessageButton("Join", link, ButtonActionKind.OPEN_URL),
            ),
        )
    ).ok

    payload = transport.requests[-1][2]
    assert payload is not None
    action = payload["keyboard"]["content"]["rows"][0]["buttons"][0]["action"]
    assert action == {
        "type": 0,
        "data": link,
        "permission": {"type": 2},
        "unsupport_tips": "请复制消息中的链接",
    }


def test_qq_http_sends_markdown_without_a_keyboard(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(database, privacy_primitives)
    target_ref = contacts.record(
        channel_account="bot_test_a",
        kind="private",
        external_identifier="private-openid-markdown-only-test",
        now=NOW,
    )
    transport = FakeTransport()
    adapter = QQHttpAdapter(
        QQBotCredentials("bot_test_a", "synthetic-app", "synthetic-secret"),
        contacts,
        transport=transport,
    )

    result = adapter.send(
        OutboundMessage(
            channel="qq",
            channel_account="bot_test_a",
            target_ref=target_ref,
            kind=MessageKind.MARKDOWN,
            text="# Synthetic heading\n\n- item",
        )
    )

    assert result.ok
    payload = transport.requests[-1][2]
    assert payload == {
        "msg_type": 2,
        "markdown": {"content": "# Synthetic heading\n\n- item"},
    }


def test_qq_http_falls_back_to_plain_text_when_markdown_is_rejected(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(database, privacy_primitives)
    target_ref = contacts.record(
        channel_account="bot_test_a",
        kind="private",
        external_identifier="private-openid-markdown-fallback-test",
        now=NOW,
    )

    class MarkdownRejectedTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.message_requests = 0

        def request(
            self,
            url: str,
            *,
            method: str = "GET",
            payload: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
            timeout: float = 10,
        ) -> tuple[int, dict[str, Any]]:
            if url.endswith("getAppAccessToken"):
                return super().request(
                    url,
                    method=method,
                    payload=payload,
                    headers=headers,
                    timeout=timeout,
                )
            self.requests.append((url, method, payload, headers))
            self.message_requests += 1
            if self.message_requests == 1:
                return 400, {}
            return 200, {"id": "provider-fallback-message-test"}

    transport = MarkdownRejectedTransport()
    adapter = QQHttpAdapter(
        QQBotCredentials("bot_test_a", "synthetic-app", "synthetic-secret"),
        contacts,
        transport=transport,
    )
    result = adapter.send(
        OutboundMessage(
            channel="qq",
            channel_account="bot_test_a",
            target_ref=target_ref,
            kind=MessageKind.BUTTON,
            text="# ⏰ Reminder\n\n**Item:** Synthetic",
            buttons=(
                MessageButton("Complete", "/提醒完成 reminder_synthetic"),
                MessageButton("Cancel", "/取消提醒 reminder_synthetic"),
            ),
        )
    )

    assert result.ok
    assert result.provider_message_id == "provider-fallback-message-test"
    rich_payload = transport.requests[-2][2]
    plain_payload = transport.requests[-1][2]
    assert rich_payload is not None and rich_payload["msg_type"] == 2
    assert plain_payload is not None and plain_payload["msg_type"] == 0
    assert "markdown" not in plain_payload
    assert plain_payload["content"].startswith("⏰ Reminder\n\nItem: Synthetic")
    assert "/提醒完成 reminder_synthetic" in plain_payload["content"]
    assert "/取消提醒 reminder_synthetic" in plain_payload["content"]


def test_gateway_persists_resume_state_encrypted_and_emits_ephemeral_event(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    cipher, _references = privacy_primitives
    contacts = register_account(database, privacy_primitives)
    store = QQGatewaySessionStore(database, cipher)
    mapper = QQEventMapper("bot_test_a", contacts)
    received: list[InboundEvent] = []
    protocol = QQGatewayProtocol(
        channel_account="bot_test_a",
        mapper=mapper,
        session_store=store,
        on_event=received.append,
    )
    assert protocol.handle(
        {
            "op": 0,
            "t": "READY",
            "s": 7,
            "d": {
                "session_id": "session-canary",
                "resume_gateway_url": "wss://example.invalid/resume",
            },
        },
        received_at=NOW,
    ) == "ready"
    assert protocol.handshake_payload("synthetic-token")["op"] == 6
    assert protocol.handle(
        {
            "op": 0,
            "t": "GROUP_AT_MESSAGE_CREATE",
            "s": 8,
            "d": {
                "id": "event_gateway_test",
                "content": "gateway-body-canary",
                "group_openid": "group-openid-gateway-canary",
                "author": {"member_openid": "member-openid-gateway-canary"},
            },
        },
        received_at=NOW,
    ) == "event"
    assert len(received) == 1
    assert received[0].text == "gateway-body-canary"
    reply_ref = str(received[0].metadata["reply_context_ref"])
    reply_context = contacts.resolve_reply_context(
        "bot_test_a",
        reply_ref,
        target_ref=received[0].external_conversation_ref,
        now=NOW,
    )
    assert reply_context is not None
    assert reply_context.field == "msg_id"
    assert reply_context.identifier == "event_gateway_test"
    restored = store.load("bot_test_a")
    assert restored == QQGatewayState(
        session_id="session-canary",
        sequence=8,
        resume_url="wss://example.invalid/resume",
    )

    database_bytes = database.path.read_bytes()
    wal = database.path.with_name(database.path.name + "-wal")
    if wal.exists():
        database_bytes += wal.read_bytes()
    for canary in (
        b"session-canary",
        b"group-openid-gateway-canary",
        b"member-openid-gateway-canary",
        b"gateway-body-canary",
        b"event_gateway_test",
    ):
        assert canary not in database_bytes


def test_gateway_identify_matches_official_payload_and_ready_allows_no_resume_url(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    cipher, _references = privacy_primitives
    contacts = register_account(database, privacy_primitives)
    store = QQGatewaySessionStore(database, cipher)
    protocol = QQGatewayProtocol(
        channel_account="bot_test_a",
        mapper=QQEventMapper("bot_test_a", contacts),
        session_store=store,
        on_event=lambda _event: None,
    )

    assert FULL_INTENTS == (
        (1 << 0)
        | (1 << 1)
        | (1 << 30)
        | (1 << 12)
        | (1 << 25)
        | (1 << 26)
    )
    assert protocol.identify_payload("synthetic-token") == {
        "op": 2,
        "d": {
            "token": "QQBot synthetic-token",
            "intents": FULL_INTENTS,
            "shard": [0, 1],
        },
    }
    assert (
        protocol.handle(
            {
                "op": 0,
                "t": "READY",
                "s": 1,
                "d": {"session_id": "synthetic-session"},
            },
            received_at=NOW,
        )
        == "ready"
    )
    restored = store.load("bot_test_a")
    assert restored.session_id == "synthetic-session"
    assert restored.resume_url == ""
    assert protocol.handshake_payload("synthetic-token") == {
        "op": 6,
        "d": {
            "token": "QQBot synthetic-token",
            "session_id": "synthetic-session",
            "seq": 1,
        },
    }


def test_gateway_heartbeat_reconnect_and_invalid_session_state_machine(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    cipher, _references = privacy_primitives
    contacts = register_account(database, privacy_primitives)
    store = QQGatewaySessionStore(database, cipher)
    protocol = QQGatewayProtocol(
        channel_account="bot_test_a",
        mapper=QQEventMapper("bot_test_a", contacts),
        session_store=store,
        on_event=lambda _event: None,
    )
    assert protocol.handle(
        {
            "op": 0,
            "t": "READY",
            "s": 7,
            "d": {
                "session_id": "synthetic-session",
                "resume_gateway_url": "wss://example.invalid/resume",
            },
        },
        received_at=NOW,
    ) == "ready"
    assert protocol.heartbeat_payload() == {"op": 1, "d": 7}
    assert not protocol.state.heartbeat_acknowledged
    assert protocol.handle({"op": 11}, received_at=NOW) == "heartbeat_ack"
    assert protocol.state.heartbeat_acknowledged
    assert protocol.handle({"op": 7}, received_at=NOW) == "reconnect"

    assert protocol.handle({"op": 9, "d": True}, received_at=NOW) == "reconnect"
    assert protocol.state.session_id == "synthetic-session"
    assert protocol.handle({"op": 9, "d": False}, received_at=NOW) == "reconnect"
    assert protocol.state.session_id == ""
    assert protocol.state.sequence is None
    assert store.load("bot_test_a").session_id == ""


def test_gateway_does_not_advance_resume_sequence_when_forwarding_fails(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    cipher, _references = privacy_primitives
    contacts = register_account(database, privacy_primitives)
    store = QQGatewaySessionStore(database, cipher)
    protocol = QQGatewayProtocol(
        channel_account="bot_test_a",
        mapper=QQEventMapper("bot_test_a", contacts),
        session_store=store,
        on_event=lambda _event: (_ for _ in ()).throw(
            RuntimeError("synthetic broker outage")
        ),
    )
    assert protocol.handle(
        {
            "op": 0,
            "t": "READY",
            "s": 7,
            "d": {
                "session_id": "synthetic-session",
                "resume_gateway_url": "wss://example.invalid/resume",
            },
        },
        received_at=NOW,
    ) == "ready"

    with pytest.raises(RuntimeError, match="synthetic broker outage"):
        protocol.handle(
            {
                "op": 0,
                "t": "C2C_MESSAGE_CREATE",
                "s": 8,
                "d": {
                    "id": "synthetic-event",
                    "content": "synthetic transient body",
                    "author": {"user_openid": "synthetic-actor"},
                },
            },
            received_at=NOW,
        )

    assert protocol.state.sequence == 7
    assert store.load("bot_test_a").sequence == 7


def test_gateway_maps_button_interaction_to_deterministic_command(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(database, privacy_primitives)
    event = QQEventMapper("bot_test_a", contacts).map(
        "INTERACTION_CREATE",
        {
            "id": "synthetic-interaction",
            "type": 11,
            "user_openid": "synthetic-private-actor",
            "data": {
                "resolved": {
                    "button_data": "/提醒稍后 reminder_synthetic 15分钟",
                }
            },
        },
        received_at=NOW,
    )

    assert event is not None
    assert event.message_kind is MessageKind.BUTTON
    assert event.conversation_kind is ConversationKind.PRIVATE
    assert event.text == "/提醒稍后 reminder_synthetic 15分钟"
    assert event.metadata["mentioned"] is True


def test_gateway_strips_only_the_bot_mention_from_group_natural_language(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(database, privacy_primitives)
    event = QQEventMapper("bot_test_a", contacts).map(
        "GROUP_AT_MESSAGE_CREATE",
        {
            "id": "synthetic-natural-event",
            "group_openid": "synthetic-group",
            "content": (
                "<@!bot_test_a> every month create a synthetic recurring event"
            ),
            "author": {"member_openid": "synthetic-member"},
        },
        received_at=NOW,
    )

    assert event is not None
    assert event.metadata["mentioned"] is True
    assert event.text == "every month create a synthetic recurring event"


def test_gateway_strips_a_display_name_mention_before_a_group_command(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(database, privacy_primitives)
    event = QQEventMapper("bot_test_a", contacts).map(
        "GROUP_AT_MESSAGE_CREATE",
        {
            "id": "synthetic-display-mention-event",
            "group_openid": "synthetic-group",
            "content": "@SyntheticBot /日历",
            "author": {"member_openid": "synthetic-member"},
        },
        received_at=NOW,
    )

    assert event is not None
    assert event.metadata["mentioned"] is True
    assert event.text == "/日历"


def test_gateway_accepts_a_display_mentioned_command_from_plain_group_delivery(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(database, privacy_primitives)
    event = QQEventMapper("bot_test_a", contacts).map(
        "GROUP_MESSAGE_CREATE",
        {
            "id": "synthetic-plain-display-command-event",
            "group_openid": "synthetic-group",
            "content": "@SyntheticBot /日历",
            "author": {"member_openid": "synthetic-member"},
        },
        received_at=NOW,
    )

    assert event is not None
    assert event.metadata["mentioned"] is True
    assert event.text == "/日历"


def test_gateway_preserves_a_real_bot_mention_from_plain_group_delivery(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(
        database,
        privacy_primitives,
        account_id="logical_account_test",
    )
    event = QQEventMapper(
        "logical_account_test",
        contacts,
        bot_identifier="actual_app_id_test",
    ).map(
        "GROUP_MESSAGE_CREATE",
        {
            "id": "synthetic-plain-real-mention-event",
            "group_openid": "synthetic-group",
            "content": "<@!actual_app_id_test> create a synthetic recurring event",
            "author": {"member_openid": "synthetic-member"},
        },
        received_at=NOW,
    )

    assert event is not None
    assert event.metadata["mentioned"] is True
    assert event.text == "create a synthetic recurring event"


def test_gateway_accepts_a_group_mention_marked_by_the_mentions_entry(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    # Shape taken from a real GROUP_MESSAGE_CREATE: QQ keys the content marker
    # by the bot's per-group openid, not by its application id.
    contacts = register_account(
        database,
        privacy_primitives,
        account_id="logical_account_test",
    )
    event = QQEventMapper(
        "logical_account_test",
        contacts,
        bot_identifier="actual_app_id_test",
    ).map(
        "GROUP_MESSAGE_CREATE",
        {
            "id": "synthetic-mentions-entry-event",
            "group_openid": "synthetic-group",
            "content": "<@synthetic-bot-openid> create a synthetic recurring event",
            "author": {"member_openid": "synthetic-member"},
            "mentions": [
                {
                    "bot": True,
                    "id": "synthetic-bot-openid",
                    "is_you": True,
                    "member_openid": "synthetic-bot-openid",
                    "username": "SyntheticBotName",
                }
            ],
        },
        received_at=NOW,
    )

    assert event is not None
    assert event.metadata["mentioned"] is True
    assert event.text == "create a synthetic recurring event"


def test_gateway_ignores_a_mentions_entry_addressing_another_member(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(
        database,
        privacy_primitives,
        account_id="logical_account_test",
    )
    event = QQEventMapper(
        "logical_account_test",
        contacts,
        bot_identifier="actual_app_id_test",
    ).map(
        "GROUP_MESSAGE_CREATE",
        {
            "id": "synthetic-other-mentions-entry-event",
            "group_openid": "synthetic-group",
            "content": "<@synthetic-other-openid> create a synthetic recurring event",
            "author": {"member_openid": "synthetic-member"},
            "mentions": [
                {
                    "bot": False,
                    "id": "synthetic-other-openid",
                    "is_you": False,
                    "member_openid": "synthetic-other-openid",
                    "username": "SyntheticMember",
                }
            ],
        },
        received_at=NOW,
    )

    assert event is not None
    assert event.metadata["mentioned"] is False
    assert event.text == (
        "<@synthetic-other-openid> create a synthetic recurring event"
    )


def test_gateway_accepts_a_display_named_natural_language_group_mention(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    # QQ delivers a group mention of the bot as a plain GROUP_MESSAGE_CREATE
    # whose content carries the display name as ordinary text and no <@app-id>.
    contacts = register_account(
        database,
        privacy_primitives,
        account_id="logical_account_test",
    )
    event = QQEventMapper(
        "logical_account_test",
        contacts,
        bot_identifier="actual_app_id_test",
        display_names=("SyntheticBotName",),
    ).map(
        "GROUP_MESSAGE_CREATE",
        {
            "id": "synthetic-display-natural-event",
            "group_openid": "synthetic-group",
            "content": "@SyntheticBotName create a synthetic recurring event",
            "author": {"member_openid": "synthetic-member"},
        },
        received_at=NOW,
    )

    assert event is not None
    assert event.metadata["mentioned"] is True
    assert event.text == "create a synthetic recurring event"


def test_gateway_ignores_a_group_mention_of_another_member(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(
        database,
        privacy_primitives,
        account_id="logical_account_test",
    )
    event = QQEventMapper(
        "logical_account_test",
        contacts,
        bot_identifier="actual_app_id_test",
        display_names=("SyntheticBotName",),
    ).map(
        "GROUP_MESSAGE_CREATE",
        {
            "id": "synthetic-other-member-mention-event",
            "group_openid": "synthetic-group",
            "content": "@SyntheticMember create a synthetic recurring event",
            "author": {"member_openid": "synthetic-member"},
        },
        received_at=NOW,
    )

    assert event is not None
    assert event.metadata["mentioned"] is False
    assert event.text == "@SyntheticMember create a synthetic recurring event"


def test_gateway_does_not_treat_a_display_name_prefix_as_a_mention(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(
        database,
        privacy_primitives,
        account_id="logical_account_test",
    )
    event = QQEventMapper(
        "logical_account_test",
        contacts,
        bot_identifier="actual_app_id_test",
        display_names=("SyntheticBot",),
    ).map(
        "GROUP_MESSAGE_CREATE",
        {
            "id": "synthetic-display-prefix-event",
            "group_openid": "synthetic-group",
            "content": "@SyntheticBotOther create a synthetic recurring event",
            "author": {"member_openid": "synthetic-member"},
        },
        received_at=NOW,
    )

    assert event is not None
    assert event.metadata["mentioned"] is False


def test_gateway_maps_full_group_message_without_marking_it_mentioned(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    contacts = register_account(database, privacy_primitives)
    event = QQEventMapper("bot_test_a", contacts).map(
        "GROUP_MESSAGE_CREATE",
        {
            "id": "synthetic-continuation-event",
            "group_openid": "synthetic-group",
            "content": "change the synthetic notification time",
            "author": {"member_openid": "synthetic-member"},
        },
        received_at=NOW,
    )

    assert event is not None
    assert event.conversation_kind is ConversationKind.GROUP
    assert event.metadata["mentioned"] is False
    assert event.text == "change the synthetic notification time"


def test_gateway_acknowledges_button_before_forwarding_command(
    database: Database,
    privacy_primitives: tuple[FieldCipher, OpaqueReferenceFactory],
) -> None:
    from zhixu.adapters.channels.qq.gateway import QQGatewayRunner

    contacts = register_account(database, privacy_primitives)
    transport = FakeTransport()
    adapter = QQHttpAdapter(
        QQBotCredentials("bot_test_a", "synthetic-app", "synthetic-secret"),
        contacts,
        transport=transport,
    )

    class ProtocolSpy:
        state = QQGatewayState(resume_url="wss://example.invalid/resume")

        def handle(
            self,
            payload: dict[str, Any],
            *,
            received_at: datetime,
        ) -> str:
            del payload, received_at
            assert any(
                request[0].endswith("/interactions/synthetic-interaction")
                for request in transport.requests
            )
            return "reconnect"

    class SyntheticWebSocket:
        def __enter__(self) -> SyntheticWebSocket:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def send(self, _data: str) -> None:
            return None

        def recv(self, timeout: float | None = None) -> str:
            del timeout
            return json.dumps(
                {
                    "op": 0,
                    "t": "INTERACTION_CREATE",
                    "s": 8,
                    "d": {
                        "id": "synthetic-interaction",
                        "type": 11,
                        "user_openid": "synthetic-private-actor",
                        "data": {
                            "resolved": {
                                "button_data": (
                                    "/提醒完成 reminder_synthetic"
                                ),
                            },
                        },
                    },
                }
            )

    runner = QQGatewayRunner(
        adapter,
        ProtocolSpy(),  # type: ignore[arg-type]
        connector=lambda *_args, **_kwargs: SyntheticWebSocket(),
    )
    runner.connect_once(threading.Event())

    acknowledgement = transport.requests[-1]
    assert acknowledgement[0].endswith("/interactions/synthetic-interaction")
    assert acknowledgement[1] == "PUT"
    assert acknowledgement[2] == {"code": 0}


def test_gateway_error_log_does_not_include_exception_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from zhixu.adapters.channels.qq.gateway import QQGatewayRunner

    class FailingAdapter:
        def access_token(self) -> str:
            raise RuntimeError("sensitive-log-canary")

    class StopAfterRetry:
        stopped = False

        def is_set(self) -> bool:
            return self.stopped

        def wait(self, _delay: float) -> None:
            self.stopped = True

    runner = QQGatewayRunner(
        FailingAdapter(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    with caplog.at_level("WARNING"):
        runner.run(StopAfterRetry())  # type: ignore[arg-type]

    assert "RuntimeError" in caplog.text
    assert "sensitive-log-canary" not in caplog.text


def test_renderer_degrades_unsupported_rich_content() -> None:
    rendered = render_for_capabilities(
        OutboundMessage(
            channel="qq",
            channel_account="bot_test_a",
            target_ref="qqc_test",
            kind=MessageKind.ATTACHMENT,
            text="Synthetic",
            buttons=(MessageButton("Open", "/open"),),
            attachment_url="https://example.invalid/file.png",
        ),
        ChannelCapabilities(outbound_text=True),
    )

    assert rendered.kind is MessageKind.TEXT
    assert rendered.buttons == ()
    assert rendered.attachment_url is None
    assert "/open" in rendered.text
    assert "https://example.invalid/file.png" in rendered.text
