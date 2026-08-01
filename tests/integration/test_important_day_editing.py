from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest

from zhixu.adapters.storage.sqlite import (
    AgendaRepository,
    AnniversaryRepository,
    DailyBriefingRepository,
    Database,
    NoteRepository,
    NotificationLeadRepository,
    PendingPlanStore,
    ReminderRepository,
    TaskRepository,
    UserRepository,
)
from zhixu.application import AssistantEngine, RuleIntentRouter, ZhixuServices
from zhixu.domain import (
    UNKNOWN_YEAR,
    Action,
    CalendarSystem,
    CommandContext,
    ImportantDayKind,
    PolicyEngine,
    ResourceRef,
    User,
    UserStatus,
)
from zhixu.ports import FrozenClock

NOW = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def assistant(tmp_path: Path) -> tuple[AssistantEngine, CommandContext, Database]:
    database = Database(tmp_path / "edit.sqlite3")
    database.migrate()
    policy = PolicyEngine()
    UserRepository(database).create(
        User("owner", "Owner", UserStatus.ACTIVE, NOW),
        policy.require(
            CommandContext(actor_user_id="owner", now=NOW),
            Action.CREATE,
            ResourceRef("user", "owner", "owner"),
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
        anniversaries=AnniversaryRepository(database),
        daily_briefings=DailyBriefingRepository(database),
        notification_leads=NotificationLeadRepository(database),
    )
    engine = AssistantEngine(
        services=services,
        router=RuleIntentRouter(clock, timezone="Asia/Shanghai"),
        pending_plans=PendingPlanStore(database),
    )
    return engine, CommandContext(actor_user_id="owner", now=NOW), database


def _only(database: Database):
    return AnniversaryRepository(database).list_for_owner("owner")[0]


def test_an_anniversary_can_be_corrected_into_a_birthday(assistant) -> None:
    engine, context, database = assistant
    engine.handle("/纪念日 臭宝生日 2026-12-17", context, target_ref="qqc_group")
    stored = _only(database)
    assert stored.kind is ImportantDayKind.ANNIVERSARY

    reply = engine.handle(
        f"/重要日子 改 {stored.id} 类型 生日", context, target_ref="qqc_group"
    )
    assert reply.code == "updated"
    assert _only(database).kind is ImportantDayKind.BIRTHDAY
    # Correcting the kind must not disturb any other stored field.
    assert _only(database).anchor_date == date(2026, 12, 17)
    assert _only(database).title == "臭宝生日"


def test_each_field_can_be_edited_on_its_own(assistant) -> None:
    engine, context, database = assistant
    engine.handle("/纪念日 结婚 2020-05-20", context, target_ref="qqc_group")
    identifier = _only(database).id

    engine.handle(f"/重要日子 改 {identifier} 名称 结婚纪念", context, target_ref="qqc_group")
    assert _only(database).title == "结婚纪念"

    engine.handle(f"/重要日子 改 {identifier} 日期 2020-05-21", context, target_ref="qqc_group")
    assert _only(database).anchor_date == date(2020, 5, 21)

    engine.handle(f"/重要日子 改 {identifier} 预告 30 15 7 1", context, target_ref="qqc_group")
    assert _only(database).advance_days == (30, 15, 7, 1)

    engine.handle(f"/重要日子 改 {identifier} 预告 关闭", context, target_ref="qqc_group")
    assert _only(database).advance_days == ()

    # Everything else survived the sequence of single-field edits.
    final = _only(database)
    assert final.title == "结婚纪念"
    assert final.anchor_date == date(2020, 5, 21)
    assert final.kind is ImportantDayKind.ANNIVERSARY


def test_switching_to_the_lunisolar_calendar_carries_the_new_date(assistant) -> None:
    engine, context, database = assistant
    engine.handle("/生日 奶奶 1960-08-20", context, target_ref="qqc_group")
    identifier = _only(database).id

    reply = engine.handle(
        f"/重要日子 改 {identifier} 日期 农历 7-25", context, target_ref="qqc_group"
    )
    assert reply.code == "updated"
    stored = _only(database)
    assert stored.calendar is CalendarSystem.LUNAR
    assert (stored.lunar_month, stored.lunar_day, stored.lunar_leap) == (7, 25, False)
    assert stored.occurrence_in(2026) == date(2026, 9, 6)

    # And back again; the solar date must not keep the lunar fields alive.
    engine.handle(f"/重要日子 改 {identifier} 日期 1960-08-20", context, target_ref="qqc_group")
    restored = _only(database)
    assert restored.calendar is CalendarSystem.SOLAR
    assert restored.lunar_month is None and restored.lunar_day is None
    assert restored.occurrence_in(2026) == date(2026, 8, 20)


def test_a_leap_month_birthday_round_trips(assistant) -> None:
    engine, context, database = assistant
    engine.handle("/生日 阿姨 1990-01-01", context, target_ref="qqc_group")
    identifier = _only(database).id

    engine.handle(f"/重要日子 改 {identifier} 日期 农历 闰6-15", context, target_ref="qqc_group")
    stored = _only(database)
    assert stored.lunar_leap is True
    assert (stored.lunar_month, stored.lunar_day) == (6, 15)
    # 2025 has an intercalary sixth month; 2026 does not, so it falls back.
    assert stored.occurrence_in(2025) == date(2025, 8, 8)
    assert stored.occurrence_in(2026) is not None


def test_an_important_day_can_be_deleted(assistant) -> None:
    engine, context, database = assistant
    engine.handle("/纪念日 结婚 2020-05-20", context, target_ref="qqc_group")
    identifier = _only(database).id

    # Deleting is staged for confirmation like every other destructive action.
    staged = engine.handle(f"/重要日子 删除 {identifier}", context, target_ref="qqc_group")
    assert staged.code == "plan_preview"
    assert AnniversaryRepository(database).list_for_owner("owner") != []

    accept = next(b for b in staged.buttons if "接受" in b.label)
    confirmed = engine.handle(accept.action, context, target_ref="qqc_group")
    assert confirmed.code == "deleted"
    assert AnniversaryRepository(database).list_for_owner("owner") == []


def test_editing_something_that_does_not_exist_is_reported(assistant) -> None:
    engine, context, _database = assistant
    missing = engine.handle(
        "/重要日子 改 anniversary_missing 类型 生日", context, target_ref="qqc_group"
    )
    assert missing.code in {"invalid_intent", "not_found"}
    staged = engine.handle("/重要日子 删除 anniversary_missing", context, target_ref="qqc_group")
    accept = next(b for b in staged.buttons if "接受" in b.label)
    assert engine.handle(accept.action, context, target_ref="qqc_group").code == "not_found"


def test_a_daily_briefing_can_be_retimed_disabled_and_deleted(assistant) -> None:
    engine, context, database = assistant
    engine.handle("/每日简报 08:00", context, target_ref="qqc_group")
    briefings = DailyBriefingRepository(database).list_for_owner("owner")
    identifier = briefings[0].id

    engine.handle(f"/每日简报 改 {identifier} 时间 07:30", context, target_ref="qqc_group")
    assert DailyBriefingRepository(database).get(identifier).time_of_day == time(7, 30)

    engine.handle(f"/每日简报 改 {identifier} 开关 关", context, target_ref="qqc_group")
    assert DailyBriefingRepository(database).get(identifier).enabled is False

    engine.handle(f"/每日简报 改 {identifier} 开关 开", context, target_ref="qqc_group")
    assert DailyBriefingRepository(database).get(identifier).enabled is True

    staged = engine.handle(f"/每日简报 删除 {identifier}", context, target_ref="qqc_group")
    assert staged.code == "plan_preview"
    accept = next(b for b in staged.buttons if "接受" in b.label)
    assert engine.handle(accept.action, context, target_ref="qqc_group").code == "deleted"
    assert DailyBriefingRepository(database).get(identifier) is None


def test_a_birthday_without_a_year_keeps_the_sentinel_through_an_edit(
    assistant,
) -> None:
    engine, context, database = assistant
    engine.handle("/生日 同事 8-20", context, target_ref="qqc_group")
    stored = _only(database)
    assert stored.anchor_date.year == UNKNOWN_YEAR

    engine.handle(f"/重要日子 改 {stored.id} 名称 同事小王", context, target_ref="qqc_group")
    updated = _only(database)
    assert updated.anchor_date.year == UNKNOWN_YEAR
    assert updated.title == "同事小王"
    assert updated.occurrence_in(2026) == date(2026, 8, 20)


def test_duplicate_important_day_requires_a_second_confirmation(assistant) -> None:
    engine, context, database = assistant
    first = engine.handle("/生日 同事 8-20", context, target_ref="qqc_group")
    assert first.code == "created"

    staged = engine.handle("/生日 同事 8-20", context, target_ref="qqc_group")
    assert staged.code == "plan_preview"
    assert "检测到可能重复" in staged.text
    assert len(AnniversaryRepository(database).list_for_owner("owner")) == 1

    accept = next(button for button in staged.buttons if "接受" in button.label)
    created = engine.handle(accept.action, context, target_ref="qqc_group")
    assert created.code == "created"
    assert len(AnniversaryRepository(database).list_for_owner("owner")) == 2
