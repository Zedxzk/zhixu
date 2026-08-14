from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from zhixu.adapters.storage.sqlite import (
    AgendaNotificationRepository,
    AgendaRepository,
    AnniversaryRepository,
    ChannelRouteStore,
    DailyBriefingRepository,
    Database,
    GroupMode,
    NoteRepository,
    OutboxRepository,
    PendingPlanStore,
    ReminderRepository,
    ScheduledJobRepository,
    TaskRepository,
    UserRepository,
)
from zhixu.application import DailyBriefingScheduler, ReminderScheduler, ZhixuServices
from zhixu.application.commands import (
    CreateAgenda,
    CreateAnniversary,
    CreateDailyBriefing,
    CreateNote,
    CreateReminder,
    CreateTask,
    TransitionTask,
)
from zhixu.application.queries import AgendaBetween, ListTasks, SearchNotes
from zhixu.delivery import OutboxStore
from zhixu.domain import (
    Action,
    AgendaItem,
    CommandContext,
    DataClassification,
    MissedReminderPolicy,
    PolicyEngine,
    RecurrenceRule,
    RequestChannel,
    ResourceRef,
    ScheduledJob,
    TaskStatus,
    User,
    UserStatus,
    occurrences_between,
)
from zhixu.domain.errors import ConcurrencyConflict
from zhixu.ports import FrozenClock

NOW = datetime(2026, 1, 1, 8, tzinfo=UTC)


class SequentialIds:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def __call__(self, prefix: str) -> str:
        self.counts[prefix] = self.counts.get(prefix, 0) + 1
        return f"{prefix}_test_{self.counts[prefix]}"


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "zhixu.sqlite3")
    assert database.migrate() == list(range(1, 21))
    assert database.migrate() == []
    return database


@pytest.fixture
def app(database: Database) -> tuple[ZhixuServices, FrozenClock, UserRepository]:
    clock = FrozenClock(NOW)
    policy = PolicyEngine()
    users = UserRepository(database)
    owner_context = CommandContext(actor_user_id="user_test", now=NOW)
    authorization = policy.require(
        owner_context,
        Action.CREATE,
        ResourceRef(
            "user",
            "user_test",
            "user_test",
            DataClassification.PERSONAL,
        ),
    )
    users.create(
        User("user_test", "Synthetic User", UserStatus.ACTIVE, NOW),
        authorization,
    )
    services = ZhixuServices(
        agenda=AgendaRepository(database),
        tasks=TaskRepository(database),
        notes=NoteRepository(database),
        reminders=ReminderRepository(database),
        anniversaries=AnniversaryRepository(database),
        daily_briefings=DailyBriefingRepository(database),
        agenda_notifications=AgendaNotificationRepository(database),
        policy=policy,
        clock=clock,
        id_factory=SequentialIds(),
    )
    return services, clock, users


def test_migration_creates_required_phase_one_tables(database: Database) -> None:
    with database.connect() as connection:
        names = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }

    assert {
        "users",
        "external_identities",
        "resource_acl",
        "agenda_items",
        "tasks",
        "notes",
        "note_attachments",
        "note_categories",
        "note_content_blocks",
        "note_content_fields",
        "notes_fts",
        "reminders",
        "outbox_deliveries",
        "llm_usage",
        "llm_call_events",
        "audit_events",
        "schema_migrations",
        "group_activation_challenges",
        "qq_reply_contexts",
        "private_link_challenges",
        "anniversaries",
        "daily_briefings",
        "agenda_notification_rules",
        "assistant_pending_plans",
    } <= names


def test_structured_note_migration_preserves_existing_note(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    migration_dir = (
        Path(__file__).parents[2]
        / "src/zhixu/adapters/storage/sqlite/migrations"
    )
    legacy_migrations = sorted(migration_dir.glob("00[01][0-9]_*.sql"))
    assert [int(item.name[:4]) for item in legacy_migrations] == list(range(1, 20))
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        for migration in legacy_migrations:
            version_text, _, name = migration.stem.partition("_")
            script = migration.read_text(encoding="utf-8")
            connection.executescript(script)
            connection.execute(
                """
                INSERT INTO schema_migrations(version,name,checksum,applied_at)
                VALUES(?,?,?,?)
                """,
                (
                    int(version_text),
                    name,
                    hashlib.sha256(script.encode("utf-8")).hexdigest(),
                    NOW.isoformat(),
                ),
            )
        connection.execute(
            "INSERT INTO users(id,display_name,status,created_at) VALUES(?,?,?,?)",
            ("user_legacy", "Legacy Synthetic", "active", NOW.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO notes(
                id,owner_user_id,creator_user_id,title,body,classification,
                version,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                "note_legacy",
                "user_legacy",
                "user_legacy",
                "Legacy entry",
                "Legacy synthetic body",
                int(DataClassification.PERSONAL),
                1,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO notes_fts(note_id,owner_user_id,title,body)
            VALUES(?,?,?,?)
            """,
            (
                "note_legacy",
                "user_legacy",
                "Legacy entry",
                "Legacy synthetic body",
            ),
        )

    database = Database(path)
    assert database.migrate() == [20]
    note = NoteRepository(database).get("note_legacy")
    assert note is not None
    assert note.category_path == ("未分类",)
    assert len(note.content_blocks) == 1
    assert note.content_blocks[0].name == "默认内容"
    assert note.content_blocks[0].body == "Legacy synthetic body"


def test_hong_kong_business_calendar_is_scoped_to_explicit_rule() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    salary = AgendaItem(
        id="agenda_salary_synthetic",
        owner_user_id="user_synthetic",
        title="Synthetic salary",
        start_at=datetime(2026, 6, 29, tzinfo=timezone),
        end_at=datetime(2026, 6, 30, tzinfo=timezone),
        timezone="Asia/Shanghai",
        recurrence=RecurrenceRule(
            "X-BUSINESS-DAY;CALENDAR=HK_GENERAL_HOLIDAYS;BYSETPOS=-2",
            "Asia/Shanghai",
        ),
    )
    ordinary = AgendaItem(
        id="agenda_ordinary_synthetic",
        owner_user_id="user_synthetic",
        title="Synthetic ordinary monthly event",
        start_at=datetime(2026, 6, 29, 10, tzinfo=timezone),
        end_at=datetime(2026, 6, 29, 11, tzinfo=timezone),
        timezone="Asia/Shanghai",
        recurrence=RecurrenceRule("FREQ=MONTHLY", "Asia/Shanghai"),
    )
    window_start = datetime(2026, 8, 1, tzinfo=timezone)
    window_end = datetime(2026, 9, 1, tzinfo=timezone)

    salary_occurrences = occurrences_between(salary, window_start, window_end)
    ordinary_occurrences = occurrences_between(ordinary, window_start, window_end)

    assert [value.start_at.day for value in salary_occurrences] == [28]
    assert [value.start_at.day for value in ordinary_occurrences] == [29]


def test_daily_briefing_enqueues_anniversary_schedule_image_once(
    app: tuple[ZhixuServices, FrozenClock, UserRepository],
    database: Database,
) -> None:
    services, clock, _users = app
    context = CommandContext(actor_user_id="user_test")
    timezone = ZoneInfo("Asia/Shanghai")
    target = "qqc_synthetic_daily_briefing"
    services.command_bus().execute(
        CreateAnniversary(
            title="Synthetic relationship",
            anchor_date=datetime(2025, 1, 1).date(),
            timezone="Asia/Shanghai",
        ),
        context,
    )
    services.command_bus().execute(
        CreateAgenda(
            title="Synthetic daily event",
            start_at=datetime(2026, 1, 1, 18, tzinfo=timezone),
            end_at=datetime(2026, 1, 1, 19, tzinfo=timezone),
            timezone="Asia/Shanghai",
        ),
        context,
    )
    services.command_bus().execute(
        CreateDailyBriefing(
            time_of_day=time(16),
            timezone="Asia/Shanghai",
            target_ref=target,
        ),
        context,
    )
    ChannelRouteStore(database).observe(
        channel="qq",
        channel_account="bot_synthetic",
        opaque_ref=target,
        kind="private",
        now=clock.now(),
    )
    outbox = OutboxStore(database)
    scheduler = DailyBriefingScheduler(
        services.daily_briefings,
        services.anniversaries,
        services.agenda,
        services.reminders,
        outbox,
        clock,
    )

    assert scheduler.tick() == 1
    assert scheduler.tick() == 0
    claimed = outbox.claim(worker_id="synthetic-daily-worker", now=clock.now())
    assert claimed is not None
    assert "Synthetic relationship" in claimed.message.text
    assert "Synthetic daily event" in claimed.message.text
    assert claimed.message.daily_agenda_preview is not None
    # The title now travels with the entry so the image can draw it.
    assert claimed.message.daily_agenda_preview.entries == (
        (1080, 1140, "agenda", "Synthetic daily event"),
    )
    assert claimed.message.daily_agenda_preview.anniversary_day_numbers == (366,)


def test_pending_plan_sessions_are_actor_isolated_and_expire(
    app: tuple[ZhixuServices, FrozenClock, UserRepository],
    database: Database,
) -> None:
    _services, clock, users = app
    policy = PolicyEngine()
    users.create(
        User("user_second", "Synthetic Second User", UserStatus.ACTIVE, NOW),
        policy.require(
            CommandContext(actor_user_id="user_second", now=NOW),
            Action.CREATE,
            ResourceRef("user", "user_second", "user_second"),
        ),
    )
    store = PendingPlanStore(database)
    plan = store.put(
        actor_user_id="user_test",
        target_ref="qqc_synthetic_continuation",
        action="create_agenda",
        payload_json="{}",
        now=clock.now(),
    )
    second_plan = store.put(
        actor_user_id="user_second",
        target_ref="qqc_synthetic_continuation",
        action="create_reminder",
        payload_json="{}",
        now=clock.now(),
    )

    assert store.current(
        actor_user_id="user_test",
        target_ref="qqc_synthetic_continuation",
        now=clock.now() + timedelta(minutes=29, seconds=59),
    ) == plan
    assert store.current(
        actor_user_id="user_second",
        target_ref="qqc_synthetic_continuation",
        now=clock.now(),
    ) == second_plan
    assert store.consume(plan.id, now=clock.now())
    assert (
        store.current(
            actor_user_id="user_test",
            target_ref="qqc_synthetic_continuation",
            now=clock.now(),
        )
        is None
    )
    assert store.current(
        actor_user_id="user_second",
        target_ref="qqc_synthetic_continuation",
        now=clock.now(),
    ) == second_plan
    expiring_plan = store.put(
        actor_user_id="user_test",
        target_ref="qqc_synthetic_continuation",
        action="create_agenda",
        payload_json="{}",
        now=clock.now(),
    )
    assert store.current(
        actor_user_id="user_test",
        target_ref="qqc_synthetic_continuation",
        now=clock.now() + timedelta(minutes=29, seconds=59),
    ) == expiring_plan
    assert (
        store.current(
            actor_user_id="user_test",
            target_ref="qqc_synthetic_continuation",
            now=clock.now() + timedelta(minutes=30),
        )
        is None
    )


def test_project_admin_bootstrap_role_is_singleton(
    app: tuple[ZhixuServices, FrozenClock, UserRepository],
) -> None:
    _services, _clock, users = app
    assert users.assign_project_admin_if_vacant("user_test", now=NOW)
    assert users.has_role("user_test", "project_admin")

    policy = PolicyEngine()
    users.create(
        User("user_second", "Synthetic Second User", UserStatus.ACTIVE, NOW),
        policy.require(
            CommandContext(actor_user_id="user_second", now=NOW),
            Action.CREATE,
            ResourceRef("user", "user_second", "user_second"),
        ),
    )
    assert not users.assign_project_admin_if_vacant("user_second", now=NOW)
    assert not users.has_role("user_second", "project_admin")


def test_private_link_challenge_locks_after_five_invalid_attempts(
    app: tuple[ZhixuServices, FrozenClock, UserRepository],
    database: Database,
) -> None:
    _services, _clock, _users = app
    routes = ChannelRouteStore(database)
    routes.issue_private_link(
        code_hash="private_link_valid_synthetic",
        channel="qq",
        channel_account="qq_synthetic",
        private_actor_ref="private_actor_synthetic",
        expires_at=NOW + timedelta(minutes=20),
        now=NOW,
    )
    for index in range(5):
        assert (
            routes.consume_private_link(
                code_hash=f"private_link_invalid_{index}",
                channel="qq",
                channel_account="qq_synthetic",
                consumed_by_user_id="user_test",
                now=NOW,
            )
            is None
        )
    assert (
        routes.consume_private_link(
            code_hash="private_link_valid_synthetic",
            channel="qq",
            channel_account="qq_synthetic",
            consumed_by_user_id="user_test",
            now=NOW,
        )
        is None
    )
    with database.connect() as connection:
        denied = connection.execute(
            """
            SELECT COUNT(*) FROM audit_events
            WHERE actor_user_id='user_test'
              AND resource_kind='private_link_challenge'
              AND outcome='denied'
            """
        ).fetchone()[0]
        consumed_at = connection.execute(
            """
            SELECT consumed_at FROM private_link_challenges
            WHERE code_hash='private_link_valid_synthetic'
            """
        ).fetchone()[0]
    assert denied == 5
    assert consumed_at is None


def test_command_and_query_buses_execute_without_model_dependency(
    app: tuple[ZhixuServices, FrozenClock, UserRepository],
) -> None:
    services, _clock, _users = app
    context = CommandContext(actor_user_id="user_test")
    commands = services.command_bus()
    queries = services.query_bus()

    commands.execute(
        CreateAgenda(
            title="Synthetic agenda",
            start_at=NOW + timedelta(hours=1),
            end_at=NOW + timedelta(hours=2),
            timezone="UTC",
        ),
        context,
    )
    commands.execute(CreateTask(title="Synthetic task"), context)
    commands.execute(
        CreateNote(
            title="Synthetic router note",
            body="The synthetic router is stored in the lab.",
            tags=("synthetic",),
        ),
        context,
    )

    agenda = queries.execute(
        AgendaBetween(NOW, NOW + timedelta(days=1)),
        context,
    )
    tasks = queries.execute(ListTasks(), context)
    notes = queries.execute(SearchNotes("router"), context)

    assert len(agenda) == 1
    assert len(tasks) == 1
    assert [note.id for note in notes] == ["note_test_1"]


def test_task_optimistic_concurrency_rejects_stale_update(
    app: tuple[ZhixuServices, FrozenClock, UserRepository],
    database: Database,
) -> None:
    services, _clock, _users = app
    context = CommandContext(actor_user_id="user_test")
    created = services.create_task(CreateTask(title="Synthetic task"), context)
    stale = created

    updated = services.transition_task(
        TransitionTask(created.id, expected_version=1, status=TaskStatus.IN_PROGRESS),
        context,
    )
    assert updated.version == 2

    repository = TaskRepository(database)
    stale_update = replace(stale, title="Stale synthetic update", version=2)
    authorization = services.policy.require(
        replace(context, now=NOW),
        Action.UPDATE,
        ResourceRef(
            "task",
            stale.id,
            stale.owner_user_id,
            stale.classification,
        ),
    )
    with pytest.raises(ConcurrencyConflict):
        repository.update(
            stale_update,
            expected_version=1,
            authorization=authorization,
        )


def test_full_text_search_is_scoped_to_owner(
    app: tuple[ZhixuServices, FrozenClock, UserRepository],
    database: Database,
) -> None:
    services, _clock, users = app
    owner_context = CommandContext(actor_user_id="user_test")
    services.create_note(
        CreateNote("Synthetic network", "Synthetic router maintenance"),
        owner_context,
    )

    policy = services.policy
    other_user = User("user_other", "Other Synthetic User", UserStatus.ACTIVE, NOW)
    users.create(
        other_user,
        policy.require(
            CommandContext(actor_user_id=other_user.id, now=NOW),
            Action.CREATE,
            ResourceRef("user", other_user.id, other_user.id),
        ),
    )
    other_note = replace(
        services.create_note(
            CreateNote("Temporary", "Temporary"),
            owner_context,
        ),
        id="note_other",
        owner_user_id=other_user.id,
        title="Other router",
        body="Other synthetic router",
        content_blocks=(),
        version=1,
        created_at=None,
        updated_at=None,
    )
    notes = NoteRepository(database)
    notes.create(
        other_note,
        policy.require(
            CommandContext(actor_user_id=other_user.id, now=NOW),
            Action.CREATE,
            ResourceRef("note", other_note.id, other_user.id),
        ),
    )

    results = notes.search("user_test", "router")
    assert results
    assert {note.owner_user_id for note in results} == {"user_test"}


def test_internal_group_uses_only_shared_data_while_private_chat_aggregates_it(
    app: tuple[ZhixuServices, FrozenClock, UserRepository],
    database: Database,
) -> None:
    services, _clock, _users = app
    routes = ChannelRouteStore(database)
    routes.observe(
        channel="qq",
        channel_account="qq_synthetic",
        opaque_ref="group_synthetic",
        kind="group",
        now=NOW,
    )
    assert routes.set_commands_enabled(
        channel="qq",
        channel_account="qq_synthetic",
        opaque_ref="group_synthetic",
        enabled=True,
        actor_user_id="user_test",
        now=NOW,
        group_mode=GroupMode.INTERNAL,
        member_user_ids=("user_test",),
    )
    route = routes.get("qq", "qq_synthetic", "group_synthetic")
    assert route is not None
    assert route.group_mode is GroupMode.INTERNAL
    assert route.shared_owner_user_id is not None
    assert route.member_user_ids == ("user_test",)

    private_context = CommandContext(actor_user_id="user_test")
    private_note = services.create_note(
        CreateNote("Private router", "private-only router material"),
        private_context,
    )
    internal_context = CommandContext(
        actor_user_id="user_test",
        roles=frozenset({"internal_group_member", "shared_workspace_member"}),
        shared_owner_user_id=route.shared_owner_user_id,
        readable_shared_owner_user_ids=(route.shared_owner_user_id,),
        request_channel=RequestChannel.GROUP_CHAT,
    )
    shared_note = services.create_note(
        CreateNote("Shared router", "group-shared router material"),
        internal_context,
    )
    shared_reminder = services.create_reminder(
        CreateReminder(
            title="Shared synthetic reminder",
            fire_at=NOW + timedelta(hours=2),
            target_ref="group_synthetic",
        ),
        internal_context,
    )

    assert private_note.owner_user_id == "user_test"
    assert shared_note.owner_user_id == route.shared_owner_user_id
    assert shared_note.creator_user_id == "user_test"
    assert shared_reminder.owner_user_id == route.shared_owner_user_id
    assert shared_reminder.creator_user_id == "user_test"
    assert shared_reminder.target_ref == "group_synthetic"
    assert [
        note.id
        for note in services.search_notes(SearchNotes("router"), internal_context)
    ] == [shared_note.id]

    aggregate_private_context = CommandContext(
        actor_user_id="user_test",
        roles=frozenset({"shared_workspace_member"}),
        readable_shared_owner_user_ids=routes.shared_owners_for_member("user_test"),
        request_channel=RequestChannel.PRIVATE_CHAT,
    )
    assert {
        note.id
        for note in services.search_notes(
            SearchNotes("router"),
            aggregate_private_context,
        )
    } == {private_note.id, shared_note.id}


def test_public_group_has_no_member_or_shared_database_access(
    app: tuple[ZhixuServices, FrozenClock, UserRepository],
    database: Database,
) -> None:
    _services, _clock, _users = app
    routes = ChannelRouteStore(database)
    routes.observe(
        channel="qq",
        channel_account="qq_synthetic",
        opaque_ref="public_group_synthetic",
        kind="group",
        now=NOW,
    )
    assert routes.set_commands_enabled(
        channel="qq",
        channel_account="qq_synthetic",
        opaque_ref="public_group_synthetic",
        enabled=True,
        actor_user_id="user_test",
        now=NOW,
        group_mode=GroupMode.PUBLIC,
    )
    route = routes.get("qq", "qq_synthetic", "public_group_synthetic")
    assert route is not None
    assert route.group_mode is GroupMode.PUBLIC
    assert route.shared_owner_user_id is None
    assert route.member_user_ids == ()


def test_reminder_tick_is_atomic_and_idempotent(
    app: tuple[ZhixuServices, FrozenClock, UserRepository],
    database: Database,
) -> None:
    services, clock, _users = app
    context = CommandContext(actor_user_id="user_test")
    reminder = services.create_reminder(
        CreateReminder(
            title="Synthetic reminder",
            fire_at=NOW + timedelta(minutes=5),
            target_ref="target_opaque_test",
        ),
        context,
    )
    scheduler = ReminderScheduler(ReminderRepository(database), clock)
    outbox = OutboxRepository(database)

    assert scheduler.tick() == 0
    clock.set(NOW + timedelta(minutes=5))
    assert scheduler.tick() == 1
    assert scheduler.tick() == 0
    assert outbox.count() == 1
    assert ReminderRepository(database).get(reminder.id).status.value == "fired"
    with database.connect() as connection:
        payload = connection.execute(
            "SELECT payload_json FROM outbox_deliveries"
        ).fetchone()
    value = json.loads(str(payload["payload_json"]))
    # Built by the shared card builder, so it matches every confirmation.
    assert value["text"] == (
        "# 日程提醒\n\n"
        "**事项：** Synthetic reminder\n\n"
        "**时间：** 2026-01-01 16:05（北京时间）"
    )
    assert [button["label"] for button in value["buttons"]] == [
        "5分钟",
        "15分钟",
        "30分钟",
        "60分钟",
        "完成",
        "取消",
    ]


def test_missed_reminder_policy_fires_or_skips_after_grace_window(
    app: tuple[ZhixuServices, FrozenClock, UserRepository],
    database: Database,
) -> None:
    services, clock, _users = app
    context = CommandContext(actor_user_id="user_test", now=NOW)
    fire = services.create_reminder(
        CreateReminder(
            title="Synthetic late reminder to deliver",
            fire_at=NOW - timedelta(minutes=10),
            target_ref="target_opaque_test",
            missed_policy=MissedReminderPolicy.FIRE,
        ),
        context,
    )
    skip = services.create_reminder(
        CreateReminder(
            title="Synthetic late reminder to skip",
            fire_at=NOW - timedelta(minutes=10),
            target_ref="target_opaque_test",
            missed_policy=MissedReminderPolicy.SKIP,
        ),
        context,
    )

    scheduler = ReminderScheduler(
        ReminderRepository(database),
        clock,
        late_grace_seconds=300,
    )
    assert scheduler.tick() == 1
    repository = ReminderRepository(database)
    assert repository.get(fire.id).status.value == "fired"
    assert repository.get(skip.id).status.value == "cancelled"
    assert OutboxRepository(database).count() == 1
    with database.connect() as connection:
        skipped = connection.execute(
            """
            SELECT reason_code FROM audit_events
            WHERE resource_kind='reminder' AND resource_id=? AND action='skip'
            """,
            (skip.id,),
        ).fetchone()
    assert skipped is not None and skipped["reason_code"] == "missed_policy"


def test_scheduled_job_run_is_idempotent(
    app: tuple[ZhixuServices, FrozenClock, UserRepository],
    database: Database,
) -> None:
    services, _clock, _users = app
    repository = ScheduledJobRepository(database)
    context = CommandContext(actor_user_id="user_test", now=NOW)
    job = ScheduledJob(
        id="job_test",
        owner_user_id="user_test",
        job_kind="synthetic",
        schedule_spec="FREQ=DAILY;COUNT=2",
        timezone="UTC",
    )
    created = repository.create(
        job,
        services.policy.require(
            context,
            Action.CREATE,
            ResourceRef("scheduled_job", job.id, job.owner_user_id),
        ),
    )
    authorization = services.policy.require(
        context,
        Action.UPDATE,
        ResourceRef("scheduled_job", job.id, job.owner_user_id),
    )

    first, first_created = repository.create_run(created, NOW, authorization)
    second, second_created = repository.create_run(created, NOW, authorization)

    assert first_created is True
    assert second_created is False
    assert first.id == second.id
