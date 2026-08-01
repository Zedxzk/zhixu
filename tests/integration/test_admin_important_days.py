from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zhixu.adapters.storage.sqlite import (
    AdminReadStore,
    AdminSessionStore,
    AgendaRepository,
    AnniversaryRepository,
    Database,
    GrantRepository,
    IdentityLinkStore,
    NoteRepository,
    ReminderRepository,
    TaskRepository,
    UserRepository,
)
from zhixu.adapters.web import AdminAPI
from zhixu.application import ZhixuServices
from zhixu.domain import (
    Action,
    AuthenticationStrength,
    CommandContext,
    PolicyEngine,
    ResourceRef,
    User,
    UserStatus,
)
from zhixu.ports import FrozenClock
from zhixu.security import FieldCipher, OpaqueReferenceFactory

NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


@pytest.fixture
def api(tmp_path: Path) -> tuple[AdminAPI, dict[str, str]]:
    database = Database(tmp_path / "admin-important-days.sqlite3")
    database.migrate()
    clock = FrozenClock(NOW)
    grants = GrantRepository(database)
    policy = PolicyEngine(grants.has_grant)
    users = UserRepository(database)
    users.create(
        User("user_owner", "Synthetic Owner", UserStatus.ACTIVE, NOW),
        policy.require(
            CommandContext(actor_user_id="user_owner", now=NOW),
            Action.CREATE,
            ResourceRef("user", "user_owner", "user_owner"),
        ),
    )
    sessions = AdminSessionStore(database)
    session = sessions.create(
        user_id="user_owner",
        authentication=AuthenticationStrength.STEP_UP,
        now=NOW,
    )
    value = AdminAPI(
        services=ZhixuServices(
            agenda=AgendaRepository(database),
            tasks=TaskRepository(database),
            notes=NoteRepository(database),
            reminders=ReminderRepository(database),
            anniversaries=AnniversaryRepository(database),
            policy=policy,
            clock=clock,
        ),
        policy=policy,
        users=users,
        grants=grants,
        sessions=sessions,
        identity_links=IdentityLinkStore(database, challenge_key=b"c" * 32),
        reads=AdminReadStore(database),
        clock=clock,
        field_cipher=FieldCipher(b"e" * 32),
        references=OpaqueReferenceFactory(b"r" * 32),
    )
    return value, {"Authorization": f"Bearer {session.value}"}


def _body(value: dict[str, object]) -> bytes:
    return json.dumps(value).encode()


def test_admin_creates_and_lists_a_solar_birthday(
    api: tuple[AdminAPI, dict[str, str]],
) -> None:
    admin, headers = api
    created = admin.dispatch(
        "POST",
        "/admin/important-days",
        headers=headers,
        body=_body(
            {
                "title": "Synthetic Person",
                "anchor_date": "0001-08-20",
                "timezone": "Asia/Shanghai",
                "kind": "birthday",
                "calendar": "solar",
                "advance_days": [7, 3, 1],
                "private": True,
            }
        ),
    )

    assert created.status == 201
    assert created.body["kind"] == "birthday"
    assert created.body["anchor_date"] == "0001-08-20"
    assert created.body["next_occurrence"] == "2026-08-20"
    assert created.body["advance_days"] == [7, 3, 1]
    listed = admin.dispatch("GET", "/admin/important-days", headers=headers)
    assert listed.status == 200
    assert listed.body == [created.body]


def test_admin_creates_a_lunar_birthday(
    api: tuple[AdminAPI, dict[str, str]],
) -> None:
    admin, headers = api
    created = admin.dispatch(
        "POST",
        "/admin/important-days",
        headers=headers,
        body=_body(
            {
                "title": "Synthetic Lunar Birthday",
                "anchor_date": "1960-01-01",
                "timezone": "Asia/Shanghai",
                "kind": "birthday",
                "calendar": "lunar",
                "lunar_month": 7,
                "lunar_day": 25,
                "lunar_leap": False,
            }
        ),
    )

    assert created.status == 201
    assert created.body["calendar"] == "lunar"
    assert created.body["lunar_month"] == 7
    assert created.body["lunar_day"] == 25
    assert created.body["next_occurrence"] == "2026-09-06"


def test_admin_rejects_invalid_or_unauthenticated_important_days(
    api: tuple[AdminAPI, dict[str, str]],
) -> None:
    admin, headers = api
    payload = {
        "title": "Synthetic Invalid",
        "anchor_date": "2020-05-20",
        "timezone": "Asia/Shanghai",
        "kind": "anniversary",
        "calendar": "solar",
        "advance_days": [7, 7],
    }
    assert admin.dispatch(
        "POST",
        "/admin/important-days",
        headers=headers,
        body=_body(payload),
    ).status == 422
    assert admin.dispatch("GET", "/admin/important-days").status == 403
