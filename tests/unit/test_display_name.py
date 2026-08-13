from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zhixu.application.intent_router import RuleIntentRouter
from zhixu.application.intents import IntentAction
from zhixu.application.services import _PLACEHOLDER_DISPLAY_NAMES, ZhixuServices
from zhixu.domain import CommandContext, PolicyEngine, User, UserStatus

NOW = datetime(2026, 8, 13, 4, 0, tzinfo=UTC)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


class FakeUsers:
    def __init__(self, users: dict[str, str]) -> None:
        self.users = dict(users)
        self.renamed: list[tuple[str, str]] = []

    def get(self, user_id: str) -> User | None:
        name = self.users.get(user_id)
        if name is None:
            return None
        return User(user_id, name, UserStatus.ACTIVE, NOW)

    def rename(self, user_id: str, display_name: str, _authorization) -> User | None:
        if user_id not in self.users:
            return None
        self.renamed.append((user_id, display_name))
        self.users[user_id] = display_name
        return self.get(user_id)


def build_services(users: FakeUsers) -> ZhixuServices:
    return ZhixuServices(
        agenda=object(),  # type: ignore[arg-type]
        tasks=object(),  # type: ignore[arg-type]
        notes=object(),  # type: ignore[arg-type]
        reminders=object(),  # type: ignore[arg-type]
        policy=PolicyEngine(),
        clock=FrozenClock(),
        users=users,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("command", ["/我叫 张三", "/改名 张三"])
def test_rename_command_is_deterministic(command: str) -> None:
    intent = RuleIntentRouter(FrozenClock()).route(command)
    assert intent is not None
    assert intent.action is IntentAction.SET_DISPLAY_NAME
    assert intent.arguments == {"display_name": "张三"}
    assert intent.source == "deterministic"


def test_a_member_can_only_rename_themselves() -> None:
    users = FakeUsers({"user_a": "QQ group member", "user_b": "QQ group member"})
    services = build_services(users)
    context = CommandContext(actor_user_id="user_a", now=NOW)

    services.rename_user("张三", context)

    # The target comes from the authenticated context, never from the message.
    assert users.renamed == [("user_a", "张三")]
    assert users.users["user_b"] == "QQ group member"


def test_placeholder_names_are_not_credited() -> None:
    users = FakeUsers({"user_a": "QQ group member", "user_b": "李四"})
    services = build_services(users)

    names = services.creator_names(["user_a", "user_b", "", "missing"])

    assert names == {"user_b": "李四"}
    assert "QQ group member" in _PLACEHOLDER_DISPLAY_NAMES
