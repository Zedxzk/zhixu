from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zhixu.adapters.storage.sqlite import (
    AgendaRepository,
    Database,
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
    PolicyEngine,
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
    assert database.migrate() == [1]
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
        "notes_fts",
        "reminders",
        "outbox_deliveries",
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
