from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zhixu.adapters.storage.sqlite import (
    AgendaRepository,
    ChannelRouteStore,
    Database,
    GroupMode,
    NoteRepository,
    OutboxRepository,
    ReminderRepository,
    ScheduledJobRepository,
    TaskRepository,
    UserRepository,
)
from zhixu.application import ReminderScheduler, ZhixuServices
from zhixu.application.commands import (
    CreateAgenda,
    CreateNote,
    CreateReminder,
    CreateTask,
    TransitionTask,
)
from zhixu.application.queries import AgendaBetween, ListTasks, SearchNotes
from zhixu.domain import (
    Action,
    CommandContext,
    DataClassification,
    MissedReminderPolicy,
    PolicyEngine,
    RequestChannel,
    ResourceRef,
    ScheduledJob,
    TaskStatus,
    User,
    UserStatus,
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
    assert database.migrate() == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
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
        "notes_fts",
        "reminders",
        "outbox_deliveries",
        "llm_usage",
        "llm_call_events",
        "audit_events",
        "schema_migrations",
    } <= names


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
    assert value["text"] == (
        "# ⏰ 日程提醒\n\n"
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
