from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zhixu.adapters.channels import InboundReceiptStore
from zhixu.adapters.channels.qq import QQContactStore
from zhixu.adapters.storage.sqlite import (
    AgendaRepository,
    ChannelRouteStore,
    Database,
    GrantRepository,
    NoteRepository,
    ReminderRepository,
    TaskRepository,
    UserRepository,
)
from zhixu.adapters.web.internal_channel import InternalChannelAPI
from zhixu.application import (
    AssistantEngine,
    ReminderScheduler,
    RuleIntentRouter,
    ZhixuServices,
)
from zhixu.channels import ChannelCapabilities
from zhixu.delivery import OutboxStore, QuotaManager, QuotaRule
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
from zhixu.ports import FrozenClock
from zhixu.security import FieldCipher, OpaqueReferenceFactory

NOW = datetime(2026, 7, 30, 12, tzinfo=UTC)
SERVICE_TOKEN = "synthetic-channel-service-token-value"


def test_qq_network_database_is_separate_and_duplicate_events_are_idempotent(
    tmp_path: Path,
) -> None:
    application_database = Database(tmp_path / "application.sqlite3")
    qq_database = Database(tmp_path / "qq.sqlite3")
    assert application_database.migrate() == [1, 2, 3, 4, 5, 6, 7, 8]
    assert qq_database.migrate() == [1, 2, 3, 4, 5, 6, 7, 8]
    references = OpaqueReferenceFactory(b"R" * 32)
    cipher = FieldCipher(b"E" * 32)
    raw_actor = "synthetic-qq-actor"
    account = "qq_synthetic"

    qq_contacts = QQContactStore(qq_database, cipher, references)
    qq_contacts.register_account(
        account,
        label="Synthetic bot",
        config_ref="synthetic-config",
        now=NOW,
    )
    actor_ref = qq_contacts.record(
        channel_account=account,
        kind="private",
        external_identifier=raw_actor,
        now=NOW,
    )
    assert actor_ref == references.create("identity", "qq", account, raw_actor)

    users = UserRepository(application_database)
    grants = GrantRepository(application_database)
    policy = PolicyEngine(grants.has_grant)
    users.create(
        User("user_synthetic", "Synthetic User", UserStatus.ACTIVE, NOW),
        policy.require(
            CommandContext(actor_user_id="user_synthetic", now=NOW),
            Action.CREATE,
            ResourceRef("user", "user_synthetic", "user_synthetic"),
        ),
    )
    users.bind_identity(
        ExternalIdentity(
            "identity_synthetic",
            "user_synthetic",
            "qq",
            account,
            EncryptedIdentifier(
                cipher.encrypt(
                    raw_actor,
                    context=f"external-identity:qq:{account}:{actor_ref}",
                )
            ),
            actor_ref,
            NOW,
        ),
        policy.require(
            CommandContext(actor_user_id="user_synthetic", now=NOW),
            Action.CREATE,
            ResourceRef(
                "external_identity",
                "identity_synthetic",
                "user_synthetic",
            ),
        ),
    )
    clock = FrozenClock(NOW)
    services = ZhixuServices(
        agenda=AgendaRepository(application_database),
        tasks=TaskRepository(application_database),
        notes=NoteRepository(application_database),
        reminders=ReminderRepository(application_database),
        policy=policy,
        clock=clock,
    )
    outbox = OutboxStore(application_database)
    internal = InternalChannelAPI(
        service_token=SERVICE_TOKEN,
        users=users,
        routes=ChannelRouteStore(application_database),
        receipts=InboundReceiptStore(application_database, references),
        assistant=AssistantEngine(
            services=services,
            router=RuleIntentRouter(clock, timezone="UTC"),
        ),
        outbox=outbox,
        quota=QuotaManager(
            application_database,
            (
                QuotaRule("provider", QuotaWindow.SECOND, 20),
                QuotaRule("account", QuotaWindow.MINUTE, 20),
                QuotaRule("conversation", QuotaWindow.MINUTE, 20),
                QuotaRule("user", QuotaWindow.DAY, 20),
            ),
        ),
        references=references,
        capabilities={
            "qq": ChannelCapabilities(
                inbound_text=True,
                outbound_text=True,
                proactive_push=True,
                buttons=True,
                attachments=True,
                groups=True,
            )
        },
    )
    event = {
        "event_id": "event_synthetic",
        "channel": "qq",
        "channel_account": account,
        "actor_ref": actor_ref,
        "conversation_ref": actor_ref,
        "conversation_kind": "private",
        "message_kind": "text",
        "text": "明天9点提醒我提交合成报告",
        "received_at": NOW.isoformat(),
        "mentioned": False,
    }
    headers = {"authorization": f"Bearer {SERVICE_TOKEN}"}

    accepted = internal.dispatch(
        "POST",
        "/internal/channel/event",
        headers=headers,
        body=json.dumps(event).encode(),
    )
    duplicate = internal.dispatch(
        "POST",
        "/internal/channel/event",
        headers=headers,
        body=json.dumps(event).encode(),
    )

    assert accepted.status == 202
    assert duplicate.body == {"accepted": False, "reason_code": "duplicate_event"}
    with application_database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM inbound_event_receipts").fetchone()[0] == 1
    claim = internal.dispatch(
        "POST",
        "/internal/channel/delivery/claim",
        headers=headers,
        body=json.dumps(
            {
                "channel": "qq",
                "channel_account": account,
                "worker_id": "qq:synthetic",
            }
        ).encode(),
    )
    delivery = claim.body["delivery"]
    assert delivery["target_ref"] == actor_ref
    completed = internal.dispatch(
        "POST",
        "/internal/channel/delivery/complete",
        headers=headers,
        body=json.dumps(
            {
                "delivery_id": delivery["id"],
                "lease_token": delivery["lease_token"],
                "ok": True,
                "provider_message_id": "provider_synthetic",
            }
        ).encode(),
    )
    assert completed.body == {"status": "sent"}

    clock.set((NOW + timedelta(days=1)).replace(hour=9))
    assert ReminderScheduler(ReminderRepository(application_database), clock).tick() == 1
    proactive = internal.dispatch(
        "POST",
        "/internal/channel/delivery/claim",
        headers=headers,
        body=json.dumps(
            {
                "channel": "qq",
                "channel_account": account,
                "worker_id": "qq:synthetic",
            }
        ).encode(),
    ).body["delivery"]
    assert proactive["target_ref"] == actor_ref
    assert proactive["text"] == "提交合成报告"
    assert [button["label"] for button in proactive["buttons"]] == ["完成", "稍后"]
    internal.dispatch(
        "POST",
        "/internal/channel/delivery/complete",
        headers=headers,
        body=json.dumps(
            {
                "delivery_id": proactive["id"],
                "lease_token": proactive["lease_token"],
                "ok": True,
            }
        ).encode(),
    )

    snooze_event = {
        **event,
        "event_id": "event_synthetic_snooze",
        "message_kind": "button",
        "text": proactive["buttons"][1]["action"],
        "received_at": clock.now().isoformat(),
        "mentioned": True,
    }
    assert internal.dispatch(
        "POST",
        "/internal/channel/event",
        headers=headers,
        body=json.dumps(snooze_event).encode(),
    ).status == 202
    with application_database.connect() as connection:
        reminder = connection.execute("SELECT * FROM reminders").fetchone()
    assert reminder["status"] == "pending"

    clock.set(clock.now() + timedelta(minutes=15))
    assert ReminderScheduler(ReminderRepository(application_database), clock).tick() == 1
    repeated = internal.dispatch(
        "POST",
        "/internal/channel/delivery/claim",
        headers=headers,
        body=json.dumps(
            {
                "channel": "qq",
                "channel_account": account,
                "worker_id": "qq:synthetic",
            }
        ).encode(),
    ).body["delivery"]
    assert repeated["text"] == "提交合成报告"
    internal.dispatch(
        "POST",
        "/internal/channel/delivery/complete",
        headers=headers,
        body=json.dumps(
            {
                "delivery_id": repeated["id"],
                "lease_token": repeated["lease_token"],
                "ok": True,
            }
        ).encode(),
    )
    acknowledge_event = {
        **event,
        "event_id": "event_synthetic_acknowledge",
        "message_kind": "button",
        "text": repeated["buttons"][0]["action"],
        "received_at": clock.now().isoformat(),
        "mentioned": True,
    }
    assert internal.dispatch(
        "POST",
        "/internal/channel/event",
        headers=headers,
        body=json.dumps(acknowledge_event).encode(),
    ).status == 202
    with application_database.connect() as connection:
        assert connection.execute("SELECT status FROM reminders").fetchone()[0] == "cancelled"

    for database_path in (tmp_path / "application.sqlite3", tmp_path / "qq.sqlite3"):
        related = [
            path
            for path in tmp_path.iterdir()
            if path.name.startswith(database_path.name)
        ]
        assert raw_actor.encode() not in b"".join(path.read_bytes() for path in related)


def test_internal_channel_api_rejects_wrong_service_identity(tmp_path: Path) -> None:
    database = Database(tmp_path / "application.sqlite3")
    database.migrate()
    references = OpaqueReferenceFactory(b"R" * 32)
    policy = PolicyEngine()
    clock = FrozenClock(NOW)
    services = ZhixuServices(
        agenda=AgendaRepository(database),
        tasks=TaskRepository(database),
        notes=NoteRepository(database),
        reminders=ReminderRepository(database),
        policy=policy,
        clock=clock,
    )
    internal = InternalChannelAPI(
        service_token=SERVICE_TOKEN,
        users=UserRepository(database),
        routes=ChannelRouteStore(database),
        receipts=InboundReceiptStore(database, references),
        assistant=AssistantEngine(services=services, router=RuleIntentRouter(clock)),
        outbox=OutboxStore(database),
        quota=QuotaManager(
            database,
            (QuotaRule("provider", QuotaWindow.SECOND, 1),),
        ),
        references=references,
        capabilities={"qq": ChannelCapabilities(outbound_text=True)},
    )

    response = internal.dispatch(
        "POST",
        "/internal/channel/delivery/claim",
        headers={"authorization": "Bearer wrong-synthetic-token-value"},
        body=b"{}",
    )
    assert response.status == 403
