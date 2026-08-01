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
    GroupMode,
    NoteRepository,
    PendingPlanStore,
    ReminderRepository,
    TaskRepository,
    UserRepository,
)
from zhixu.adapters.web.internal_channel import InternalChannelAPI
from zhixu.application import (
    AssistantEngine,
    AssistantReply,
    IntentAction,
    ParsedIntent,
    ReminderScheduler,
    RuleIntentRouter,
    ZhixuServices,
)
from zhixu.application.commands import CreateNote
from zhixu.channels import (
    ChannelCapabilities,
    ConversationKind,
    InboundEvent,
    MessageKind,
)
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


class ReminderClassifierStub:
    def classify(self, *_args: object, **_kwargs: object) -> ParsedIntent:
        return ParsedIntent(
            IntentAction.CREATE_REMINDER,
            {
                "title": "提交合成报告",
                "fire_at": (NOW + timedelta(days=1)).replace(hour=9),
            },
            source="llm",
            requires_confirmation=True,
        )


def test_button_result_uses_proactive_conversation_feedback() -> None:
    event = InboundEvent(
        event_id="synthetic-button-event",
        channel="qq",
        channel_account="qq_synthetic",
        external_actor_ref="actor_ref",
        external_conversation_ref="conversation_ref",
        conversation_kind=ConversationKind.PRIVATE,
        message_kind=MessageKind.BUTTON,
        received_at=NOW,
        text="/提醒稍后 reminder_synthetic 60分钟",
        metadata={"reply_context_ref": "expiring-interaction-context"},
    )

    message = InternalChannelAPI._reply_message(
        event,
        AssistantReply("已稍后提醒：synthetic", "updated", "deterministic"),
    )

    assert message.target_ref == "conversation_ref"
    assert message.reply_context_ref == ""
    assert message.text.startswith("已稍后提醒")


def test_qq_network_database_is_separate_and_duplicate_events_are_idempotent(
    tmp_path: Path,
) -> None:
    application_database = Database(tmp_path / "application.sqlite3")
    qq_database = Database(tmp_path / "qq.sqlite3")
    assert application_database.migrate() == list(range(1, 19))
    assert qq_database.migrate() == list(range(1, 19))
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
                classifier=ReminderClassifierStub(),  # type: ignore[arg-type]
                pending_plans=PendingPlanStore(application_database),
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
        "reply_context_ref": "qqr_synthetic_reply_context",
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
        assert connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0
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
    assert delivery["reply_context_ref"] == "qqr_synthetic_reply_context"
    assert delivery["text"].startswith("# 请确认计划")
    confirm_action = delivery["buttons"][0]["action"]
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

    confirmation_event = {
        **event,
        "event_id": "event_synthetic_confirmation",
        "message_kind": "button",
        "text": confirm_action,
        "reply_context_ref": "qqr_expiring_button_context",
    }
    assert internal.dispatch(
        "POST",
        "/internal/channel/event",
        headers=headers,
        body=json.dumps(confirmation_event).encode(),
    ).status == 202
    confirmation_delivery = internal.dispatch(
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
    assert confirmation_delivery["text"].startswith("私人提醒已设置")
    assert confirmation_delivery["reply_context_ref"] == ""
    internal.dispatch(
        "POST",
        "/internal/channel/delivery/complete",
        headers=headers,
        body=json.dumps(
            {
                "delivery_id": confirmation_delivery["id"],
                "lease_token": confirmation_delivery["lease_token"],
                "ok": True,
            }
        ).encode(),
    )
    with application_database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 1

    help_event = {
        **event,
        "event_id": "event_synthetic_help",
        "text": "/帮助",
    }
    assert internal.dispatch(
        "POST",
        "/internal/channel/event",
        headers=headers,
        body=json.dumps(help_event).encode(),
    ).status == 202
    help_delivery = internal.dispatch(
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
    assert help_delivery["kind"] == "button"
    assert help_delivery["text"].startswith("# 知序 · 帮助")
    assert [button["action"] for button in help_delivery["buttons"]] == [
        "/今天",
        "/日历",
        "/待办",
        "/提醒",
    ]
    internal.dispatch(
        "POST",
        "/internal/channel/delivery/complete",
        headers=headers,
        body=json.dumps(
            {
                "delivery_id": help_delivery["id"],
                "lease_token": help_delivery["lease_token"],
                "ok": True,
            }
        ).encode(),
    )

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
    assert proactive["text"].startswith("# ⏰ 日程提醒")
    assert "**事项：** 提交合成报告" in proactive["text"]
    assert "2026-07-31 17:00（北京时间）" in proactive["text"]
    assert [button["label"] for button in proactive["buttons"]] == [
        "5分钟",
        "15分钟",
        "30分钟",
        "60分钟",
        "完成",
        "取消",
    ]
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
    assert repeated["text"].startswith("# ⏰ 日程提醒")
    assert "**事项：** 提交合成报告" in repeated["text"]
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
        "text": repeated["buttons"][4]["action"],
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


def test_group_modes_enforce_public_isolation_and_internal_member_acl(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "application.sqlite3")
    database.migrate()
    references = OpaqueReferenceFactory(b"R" * 32)
    policy = PolicyEngine()
    users = UserRepository(database)
    for user_id in ("user_owner", "user_outsider"):
        users.create(
            User(user_id, f"Synthetic {user_id}", UserStatus.ACTIVE, NOW),
            policy.require(
                CommandContext(actor_user_id=user_id, now=NOW),
                Action.CREATE,
                ResourceRef("user", user_id, user_id),
            ),
        )
        users.bind_identity(
            ExternalIdentity(
                f"identity_{user_id}",
                user_id,
                "qq",
                "qq_synthetic",
                EncryptedIdentifier(f"enc:{user_id}"),
                f"actor_{user_id}",
                NOW,
            ),
            policy.require(
                CommandContext(actor_user_id=user_id, now=NOW),
                Action.CREATE,
                ResourceRef("external_identity", f"identity_{user_id}", user_id),
            ),
        )
    clock = FrozenClock(NOW)
    services = ZhixuServices(
        agenda=AgendaRepository(database),
        tasks=TaskRepository(database),
        notes=NoteRepository(database),
        reminders=ReminderRepository(database),
        policy=policy,
        clock=clock,
    )
    services.create_note(
        CreateNote("Private needle", "private needle content"),
        CommandContext(actor_user_id="user_owner"),
    )
    routes = ChannelRouteStore(database)
    internal = InternalChannelAPI(
        service_token=SERVICE_TOKEN,
        users=users,
        routes=routes,
        receipts=InboundReceiptStore(database, references),
        assistant=AssistantEngine(
            services=services,
            router=RuleIntentRouter(clock),
            classifier=ReminderClassifierStub(),  # type: ignore[arg-type]
            pending_plans=PendingPlanStore(database),
        ),
        outbox=OutboxStore(database),
        quota=QuotaManager(
            database,
            (QuotaRule("provider", QuotaWindow.SECOND, 100),),
        ),
        references=references,
        capabilities={
            "qq": ChannelCapabilities(
                outbound_text=True,
                markdown=True,
                buttons=True,
                groups=True,
            )
        },
    )
    headers = {"authorization": f"Bearer {SERVICE_TOKEN}"}

    def event(event_id: str, actor: str, text: str) -> dict[str, object]:
        return {
            "event_id": event_id,
            "channel": "qq",
            "channel_account": "qq_synthetic",
            "actor_ref": actor,
            "conversation_ref": "group_synthetic",
            "conversation_kind": "group",
            "message_kind": "text",
            "text": text,
            "received_at": NOW.isoformat(),
            "mentioned": False,
        }

    disabled = internal.dispatch(
        "POST",
        "/internal/channel/event",
        headers=headers,
        body=json.dumps(event("event_disabled", "actor_user_owner", "/帮助")).encode(),
    )
    assert disabled.body == {
        "accepted": False,
        "reason_code": "conversation_disabled",
    }
    assert routes.set_commands_enabled(
        channel="qq",
        channel_account="qq_synthetic",
        opaque_ref="group_synthetic",
        enabled=True,
        actor_user_id="user_owner",
        now=NOW,
        group_mode=GroupMode.PUBLIC,
    )
    public_private_command = internal.dispatch(
        "POST",
        "/internal/channel/event",
        headers=headers,
        body=json.dumps(
            event("event_public_private", "actor_user_owner", "/搜索 needle")
        ).encode(),
    )
    assert public_private_command.body == {
        "accepted": False,
        "reason_code": "group_permission_denied",
    }
    public_help = internal.dispatch(
        "POST",
        "/internal/channel/event",
        headers=headers,
        body=json.dumps(event("event_public_help", "actor_user_owner", "/帮助")).encode(),
    )
    assert public_help.status == 202
    public_delivery = internal.dispatch(
        "POST",
        "/internal/channel/delivery/claim",
        headers=headers,
        body=json.dumps(
            {
                "channel": "qq",
                "channel_account": "qq_synthetic",
                "worker_id": "worker_synthetic",
            }
        ).encode(),
    ).body["delivery"]
    assert "公开群不能读取或写入任何个人数据库" in public_delivery["text"]
    internal.dispatch(
        "POST",
        "/internal/channel/delivery/complete",
        headers=headers,
        body=json.dumps(
            {
                "delivery_id": public_delivery["id"],
                "lease_token": public_delivery["lease_token"],
                "ok": True,
            }
        ).encode(),
    )

    assert routes.set_commands_enabled(
        channel="qq",
        channel_account="qq_synthetic",
        opaque_ref="group_synthetic",
        enabled=True,
        actor_user_id="user_owner",
        now=NOW,
        group_mode=GroupMode.INTERNAL,
        member_user_ids=("user_owner",),
    )
    outsider = internal.dispatch(
        "POST",
        "/internal/channel/event",
        headers=headers,
        body=json.dumps(event("event_outsider", "actor_user_outsider", "/帮助")).encode(),
    )
    assert outsider.status == 202
    assert routes.is_member(
        "qq",
        "qq_synthetic",
        "group_synthetic",
        "user_outsider",
    )
    outsider_delivery = internal.dispatch(
        "POST",
        "/internal/channel/delivery/claim",
        headers=headers,
        body=json.dumps(
            {
                "channel": "qq",
                "channel_account": "qq_synthetic",
                "worker_id": "worker_synthetic",
            }
        ).encode(),
    ).body["delivery"]
    internal.dispatch(
        "POST",
        "/internal/channel/delivery/complete",
        headers=headers,
        body=json.dumps(
            {
                "delivery_id": outsider_delivery["id"],
                "lease_token": outsider_delivery["lease_token"],
                "ok": True,
            }
        ).encode(),
    )
    created = internal.dispatch(
        "POST",
        "/internal/channel/event",
        headers=headers,
        body=json.dumps(
            event("event_shared_create", "actor_user_owner", "/记 shared needle")
        ).encode(),
    )
    assert created.status == 202
    route = routes.get("qq", "qq_synthetic", "group_synthetic")
    assert route is not None and route.shared_owner_user_id is not None
    with database.connect() as connection:
        row = connection.execute(
            "SELECT owner_user_id,creator_user_id FROM notes WHERE title='shared needle'"
        ).fetchone()
    assert row["owner_user_id"] == route.shared_owner_user_id
    assert row["creator_user_id"] == "user_owner"
    created_delivery = internal.dispatch(
        "POST",
        "/internal/channel/delivery/claim",
        headers=headers,
        body=json.dumps(
            {
                "channel": "qq",
                "channel_account": "qq_synthetic",
                "worker_id": "worker_synthetic",
            }
        ).encode(),
    ).body["delivery"]
    assert "已保存群共享备忘" in created_delivery["text"]
    internal.dispatch(
        "POST",
        "/internal/channel/delivery/complete",
        headers=headers,
        body=json.dumps(
            {
                "delivery_id": created_delivery["id"],
                "lease_token": created_delivery["lease_token"],
                "ok": True,
            }
        ).encode(),
    )

    group_search = internal.dispatch(
        "POST",
        "/internal/channel/event",
        headers=headers,
        body=json.dumps(
            event("event_shared_search", "actor_user_owner", "/搜索 needle")
        ).encode(),
    )
    assert group_search.status == 202
    search_delivery = internal.dispatch(
        "POST",
        "/internal/channel/delivery/claim",
        headers=headers,
        body=json.dumps(
            {
                "channel": "qq",
                "channel_account": "qq_synthetic",
                "worker_id": "worker_synthetic",
            }
        ).encode(),
    ).body["delivery"]
    assert "shared needle：shared needle" in search_delivery["text"]
    assert "private needle content" not in search_delivery["text"]

    reminder_event = event(
        "event_shared_reminder",
        "actor_user_owner",
        "创建明天提醒Synthetic member处理事项",
    )
    reminder_event["mentioned"] = True
    reminder_response = internal.dispatch(
        "POST",
        "/internal/channel/event",
        headers=headers,
        body=json.dumps(reminder_event).encode(),
    )
    assert reminder_response.status == 202
    plan_delivery = internal.dispatch(
        "POST",
        "/internal/channel/delivery/claim",
        headers=headers,
        body=json.dumps(
            {
                "channel": "qq",
                "channel_account": "qq_synthetic",
                "worker_id": "worker_synthetic",
            }
        ).encode(),
    ).body["delivery"]
    assert plan_delivery["text"].startswith("# 请确认计划")
    continuation_event = event(
        "event_shared_reminder_continuation",
        "actor_user_owner",
        "change the synthetic notification wording",
    )
    continuation = internal.dispatch(
        "POST",
        "/internal/channel/event",
        headers=headers,
        body=json.dumps(continuation_event).encode(),
    )
    assert continuation.status == 202
    outsider_continuation = internal.dispatch(
        "POST",
        "/internal/channel/event",
        headers=headers,
        body=json.dumps(
            event(
                "event_shared_reminder_outsider_continuation",
                "actor_user_outsider",
                "change another user's pending plan",
            )
        ).encode(),
    )
    assert outsider_continuation.body == {
        "accepted": False,
        "reason_code": "group_trigger_required",
    }
    outsider_plan_event = event(
        "event_shared_reminder_outsider_start",
        "actor_user_outsider",
        "创建明天提醒Synthetic second member处理事项",
    )
    outsider_plan_event["mentioned"] = True
    assert internal.dispatch(
        "POST",
        "/internal/channel/event",
        headers=headers,
        body=json.dumps(outsider_plan_event).encode(),
    ).status == 202
    pending_plans = PendingPlanStore(database)
    owner_plan = pending_plans.current(
        actor_user_id="user_owner",
        target_ref="group_synthetic",
        now=NOW,
    )
    outsider_plan = pending_plans.current(
        actor_user_id="user_outsider",
        target_ref="group_synthetic",
        now=NOW,
    )
    assert owner_plan is not None
    assert outsider_plan is not None
    assert owner_plan.id != outsider_plan.id
    revised_plan_delivery = internal.dispatch(
        "POST",
        "/internal/channel/delivery/claim",
        headers=headers,
        body=json.dumps(
            {
                "channel": "qq",
                "channel_account": "qq_synthetic",
                "worker_id": "worker_synthetic",
            }
        ).encode(),
    ).body["delivery"]
    assert revised_plan_delivery["text"].startswith("# 请确认计划")
    outsider_plan_delivery = internal.dispatch(
        "POST",
        "/internal/channel/delivery/claim",
        headers=headers,
        body=json.dumps(
            {
                "channel": "qq",
                "channel_account": "qq_synthetic",
                "worker_id": "worker_synthetic",
            }
        ).encode(),
    ).body["delivery"]
    assert outsider_plan_delivery["text"].startswith("# 请确认计划")
    assert internal.dispatch(
        "POST",
        "/internal/channel/event",
        headers=headers,
        body=json.dumps(
            event(
                "event_shared_reminder_outsider_own_continuation",
                "actor_user_outsider",
                "change the second member's synthetic notification wording",
            )
        ).encode(),
    ).status == 202
    outsider_revised_delivery = internal.dispatch(
        "POST",
        "/internal/channel/delivery/claim",
        headers=headers,
        body=json.dumps(
            {
                "channel": "qq",
                "channel_account": "qq_synthetic",
                "worker_id": "worker_synthetic",
            }
        ).encode(),
    ).body["delivery"]
    assert outsider_revised_delivery["text"].startswith("# 请确认计划")
    confirmation_event = event(
        "event_shared_reminder_confirmation",
        "actor_user_owner",
        revised_plan_delivery["buttons"][0]["action"],
    )
    confirmation_event["message_kind"] = "button"
    confirmation_event["mentioned"] = True
    assert internal.dispatch(
        "POST",
        "/internal/channel/event",
        headers=headers,
        body=json.dumps(confirmation_event).encode(),
    ).status == 202
    with database.connect() as connection:
        reminder_row = connection.execute(
            """
            SELECT owner_user_id,creator_user_id,target_ref
            FROM reminders WHERE title='提交合成报告'
            """
        ).fetchone()
    assert reminder_row["owner_user_id"] == route.shared_owner_user_id
    assert reminder_row["creator_user_id"] == "user_owner"
    assert reminder_row["target_ref"] == "group_synthetic"


def test_project_admin_activates_group_and_members_enrol_on_first_use(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "application.sqlite3")
    database.migrate()
    references = OpaqueReferenceFactory(b"R" * 32)
    cipher = FieldCipher(b"E" * 32)
    policy = PolicyEngine()
    users = UserRepository(database)
    users.create(
        User("user_admin", "Synthetic Administrator", UserStatus.ACTIVE, NOW),
        policy.require(
            CommandContext(actor_user_id="user_admin", now=NOW),
            Action.CREATE,
            ResourceRef("user", "user_admin", "user_admin"),
        ),
    )
    users.bind_identity(
        ExternalIdentity(
            "identity_admin",
            "user_admin",
            "qq",
            "qq_synthetic",
            EncryptedIdentifier("enc:synthetic-admin"),
            "actor_admin",
            NOW,
        ),
        policy.require(
            CommandContext(actor_user_id="user_admin", now=NOW),
            Action.CREATE,
            ResourceRef("external_identity", "identity_admin", "user_admin"),
        ),
    )
    assert users.assign_project_admin_if_vacant("user_admin", now=NOW)

    clock = FrozenClock(NOW)
    services = ZhixuServices(
        agenda=AgendaRepository(database),
        tasks=TaskRepository(database),
        notes=NoteRepository(database),
        reminders=ReminderRepository(database),
        policy=policy,
        clock=clock,
    )
    routes = ChannelRouteStore(database)
    internal = InternalChannelAPI(
        service_token=SERVICE_TOKEN,
        users=users,
        routes=routes,
        receipts=InboundReceiptStore(database, references),
        assistant=AssistantEngine(
            services=services,
            router=RuleIntentRouter(clock),
        ),
        outbox=OutboxStore(database),
        quota=QuotaManager(
            database,
            (QuotaRule("provider", QuotaWindow.SECOND, 100),),
        ),
        references=references,
        capabilities={"qq": ChannelCapabilities(outbound_text=True, groups=True)},
        field_cipher=cipher,
    )
    headers = {"authorization": f"Bearer {SERVICE_TOKEN}"}

    def dispatch(
        event_id: str,
        *,
        actor: str,
        conversation: str,
        kind: str,
        text: str,
    ):
        return internal.dispatch(
            "POST",
            "/internal/channel/event",
            headers=headers,
            body=json.dumps(
                {
                    "event_id": event_id,
                    "channel": "qq",
                    "channel_account": "qq_synthetic",
                    "actor_ref": actor,
                    "conversation_ref": conversation,
                    "conversation_kind": kind,
                    "message_kind": "text",
                    "text": text,
                    "received_at": NOW.isoformat(),
                    "mentioned": False,
                }
            ).encode(),
        )

    def claim_and_complete() -> dict[str, object]:
        delivery = internal.dispatch(
            "POST",
            "/internal/channel/delivery/claim",
            headers=headers,
            body=json.dumps(
                {
                    "channel": "qq",
                    "channel_account": "qq_synthetic",
                    "worker_id": "worker_synthetic",
                }
            ).encode(),
        ).body["delivery"]
        internal.dispatch(
            "POST",
            "/internal/channel/delivery/complete",
            headers=headers,
            body=json.dumps(
                {
                    "delivery_id": delivery["id"],
                    "lease_token": delivery["lease_token"],
                    "ok": True,
                }
            ).encode(),
        )
        return delivery

    issued = dispatch(
        "event_issue",
        actor="actor_admin",
        conversation="actor_admin",
        kind="private",
        text="/登记内部群",
    )
    assert issued.status == 202
    issue_delivery = claim_and_complete()
    code = str(issue_delivery["text"]).split("群登记码：", 1)[1][:8]
    assert code.isdigit() and len(code) == 8

    activated = dispatch(
        "event_activate",
        actor="actor_admin",
        conversation="group_synthetic",
        kind="group",
        text=f"/启用内部群 {code}",
    )
    assert activated.status == 202
    assert "内部群已自动登记" in str(claim_and_complete()["text"])
    route = routes.get("qq", "qq_synthetic", "group_synthetic")
    assert route is not None
    assert route.commands_enabled
    assert route.group_mode is GroupMode.INTERNAL
    assert route.owner_user_id == "user_admin"
    assert route.member_user_ids == ("user_admin",)

    created = dispatch(
        "event_member_create",
        actor="actor_new_member",
        conversation="group_synthetic",
        kind="group",
        text="/记 first-use shared note",
    )
    assert created.status == 202
    assert "已保存群共享备忘" in str(claim_and_complete()["text"])
    member_identity = users.identity_by_opaque_ref(
        "qq",
        "qq_synthetic",
        "actor_new_member",
    )
    assert member_identity is not None
    assert not users.has_role(member_identity.user_id, "project_admin")
    assert routes.is_member(
        "qq",
        "qq_synthetic",
        "group_synthetic",
        member_identity.user_id,
    )

    unbound = dispatch(
        "event_private_unbound",
        actor="private_new_member",
        conversation="private_new_member",
        kind="private",
        text="/帮助",
    )
    assert unbound.status == 202
    assert "当前私聊尚未绑定" in str(claim_and_complete()["text"])
    assert (
        users.identity_by_opaque_ref(
            "qq",
            "qq_synthetic",
            "private_new_member",
        )
        is None
    )

    requested = dispatch(
        "event_private_request",
        actor="private_new_member",
        conversation="private_new_member",
        kind="private",
        text="/申请绑定",
    )
    assert requested.status == 202
    link_delivery = claim_and_complete()
    link_code = str(link_delivery["text"]).split("私聊绑定码：", 1)[1][:8]
    assert link_code.isdigit() and len(link_code) == 8

    linked = dispatch(
        "event_private_link",
        actor="actor_new_member",
        conversation="group_synthetic",
        kind="group",
        text=f"/绑定私聊 {link_code}",
    )
    assert linked.status == 202
    assert "私聊身份绑定成功" in str(claim_and_complete()["text"])
    private_identity = users.identity_by_opaque_ref(
        "qq",
        "qq_synthetic",
        "private_new_member",
    )
    assert private_identity is not None
    assert private_identity.user_id == member_identity.user_id

    private_create = dispatch(
        "event_private_create",
        actor="private_new_member",
        conversation="private_new_member",
        kind="private",
        text="/记 linked private note",
    )
    assert private_create.status == 202
    assert "已保存私人备忘" in str(claim_and_complete()["text"])
    private_search = dispatch(
        "event_private_search",
        actor="private_new_member",
        conversation="private_new_member",
        kind="private",
        text="/搜索 first-use",
    )
    assert private_search.status == 202
    assert "first-use shared note" in str(claim_and_complete()["text"])

    with database.connect() as connection:
        note = connection.execute(
            """
            SELECT owner_user_id,creator_user_id FROM notes
            WHERE title='first-use shared note'
            """
        ).fetchone()
        admins = connection.execute(
            "SELECT user_id FROM role_bindings WHERE role_id='project_admin'"
        ).fetchall()
        challenge = connection.execute(
            "SELECT code_hash,consumed_at FROM group_activation_challenges"
        ).fetchone()
        private_note = connection.execute(
            """
            SELECT owner_user_id,creator_user_id FROM notes
            WHERE title='linked private note'
            """
        ).fetchone()
        private_challenge = connection.execute(
            "SELECT code_hash,consumed_at FROM private_link_challenges"
        ).fetchone()
    assert note["owner_user_id"] == route.shared_owner_user_id
    assert note["creator_user_id"] == member_identity.user_id
    assert [str(row["user_id"]) for row in admins] == ["user_admin"]
    assert challenge["code_hash"] != code
    assert challenge["consumed_at"] is not None
    assert private_note["owner_user_id"] == member_identity.user_id
    assert private_note["creator_user_id"] == member_identity.user_id
    assert private_challenge["code_hash"] != link_code
    assert private_challenge["consumed_at"] is not None
