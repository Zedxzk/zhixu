from __future__ import annotations

from datetime import datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from zhixu.adapters.storage.sqlite import (
    AgendaRepository,
    DailyBriefingRepository,
    Database,
    NotificationLeadRepository,
    ReminderRepository,
    UserRepository,
)
from zhixu.application.scheduler import NotificationLeadScheduler
from zhixu.domain import (
    DEFAULT_NOTIFICATION_LEAD_MINUTES,
    Action,
    AgendaItem,
    CommandContext,
    DailyBriefing,
    PolicyEngine,
    RecurrenceRule,
    ResourceRef,
    User,
    UserStatus,
    normalise_lead_minutes,
)
from zhixu.domain.errors import ValidationError
from zhixu.ports import FrozenClock

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "leads.sqlite3")
    value.migrate()
    policy = PolicyEngine()
    users = UserRepository(value)
    users.create(
        User("user_test", "Synthetic", UserStatus.ACTIVE, NOW),
        policy.require(
            CommandContext(actor_user_id="user_test", now=NOW),
            Action.CREATE,
            ResourceRef("user", "user_test", "user_test"),
        ),
    )
    DailyBriefingRepository(value).create(
        DailyBriefing(
            id="briefing_test",
            owner_user_id="user_test",
            creator_user_id="user_test",
            target_ref="qqc_target_test",
            time_of_day=dt_time(8, 0),
            timezone="Asia/Shanghai",
        ),
        policy.require(
            CommandContext(actor_user_id="user_test", now=NOW),
            Action.CREATE,
            ResourceRef("daily_briefing", "briefing_test", "user_test"),
        ),
    )
    return value


def _agenda(
    database: Database,
    *,
    start: datetime,
    all_day: bool = False,
    item_id: str = "agenda_test",
    recurrence: RecurrenceRule | None = None,
) -> AgendaItem:
    repository = AgendaRepository(database)
    item = AgendaItem(
        id=item_id,
        owner_user_id="user_test",
        creator_user_id="user_test",
        title="部门会议",
        start_at=start,
        end_at=start + timedelta(hours=1),
        timezone="Asia/Shanghai",
        all_day=all_day,
        recurrence=recurrence,
    )
    return repository.create(
        item,
        PolicyEngine().require(
            CommandContext(actor_user_id="user_test", now=NOW),
            Action.CREATE,
            ResourceRef("agenda", item.id, "user_test"),
        ),
    )


def test_lead_minutes_are_ordered_furthest_first_and_deduplicated() -> None:
    assert normalise_lead_minutes([60, 1440, 60, 0]) == (1440, 60, 0)
    with pytest.raises(ValidationError):
        normalise_lead_minutes([-1])
    with pytest.raises(ValidationError):
        normalise_lead_minutes(list(range(20)))


def test_defaults_apply_until_an_item_overrides_them(database: Database) -> None:
    _agenda(database, start=NOW + timedelta(hours=10))
    _agenda(database, start=NOW + timedelta(hours=11), item_id="agenda_other")
    leads = NotificationLeadRepository(database)
    assert leads.resolve("agenda_test", "user_test") == DEFAULT_NOTIFICATION_LEAD_MINUTES

    leads.set_default("user_test", [120, 10], now=NOW)
    assert leads.resolve("agenda_test", "user_test") == (120, 10)

    leads.set_override("agenda_test", "user_test", [30], now=NOW)
    assert leads.resolve("agenda_test", "user_test") == (30,)
    assert leads.resolve("agenda_other", "user_test") == (120, 10)

    # An empty override is a deliberate silence, not an absent configuration.
    leads.set_override("agenda_test", "user_test", [], now=NOW)
    assert leads.resolve("agenda_test", "user_test") == ()

    leads.clear_override("agenda_test")
    assert leads.resolve("agenda_test", "user_test") == (120, 10)


def test_every_default_lead_materialises_one_reminder(database: Database) -> None:
    # Far enough ahead that every default lead, including the 24-hour one,
    # still lies in the future.
    start = NOW + timedelta(days=1, hours=2)
    _agenda(database, start=start)
    reminders = ReminderRepository(database)
    scheduler = NotificationLeadScheduler(
        NotificationLeadRepository(database),
        AgendaRepository(database),
        reminders,
        DailyBriefingRepository(database),
        FrozenClock(NOW),
    )

    assert scheduler.tick() == len(DEFAULT_NOTIFICATION_LEAD_MINUTES)
    fire_times = sorted(
        reminder.fire_at for reminder in reminders.list_for_owner("user_test")
    )
    assert fire_times == sorted(
        start - timedelta(minutes=minutes)
        for minutes in DEFAULT_NOTIFICATION_LEAD_MINUTES
    )
    # Ticking again must not duplicate anything.
    assert scheduler.tick() == 0


def test_an_all_day_item_gets_no_lead_reminders(database: Database) -> None:
    _agenda(
        database,
        start=datetime(2026, 8, 2, 0, 0, tzinfo=SHANGHAI),
        all_day=True,
    )
    reminders = ReminderRepository(database)
    scheduler = NotificationLeadScheduler(
        NotificationLeadRepository(database),
        AgendaRepository(database),
        reminders,
        DailyBriefingRepository(database),
        FrozenClock(NOW),
    )

    assert scheduler.tick() == 0
    assert reminders.list_for_owner("user_test") == []


def test_a_lead_time_already_in_the_past_is_not_resurrected(
    database: Database,
) -> None:
    # The event starts in 20 minutes, so only the one-minute lead and the start
    # itself are still ahead; the 24h, 6h, 1h and 30min leads have all passed.
    start = NOW + timedelta(minutes=20)
    _agenda(database, start=start)
    reminders = ReminderRepository(database)
    scheduler = NotificationLeadScheduler(
        NotificationLeadRepository(database),
        AgendaRepository(database),
        reminders,
        DailyBriefingRepository(database),
        FrozenClock(NOW),
    )

    assert scheduler.tick() == 2
    assert sorted(
        reminder.fire_at for reminder in reminders.list_for_owner("user_test")
    ) == [start - timedelta(minutes=1), start]


def test_a_silenced_item_produces_nothing(database: Database) -> None:
    _agenda(database, start=NOW + timedelta(hours=10))
    leads = NotificationLeadRepository(database)
    leads.set_override("agenda_test", "user_test", [], now=NOW)
    reminders = ReminderRepository(database)
    scheduler = NotificationLeadScheduler(
        leads,
        AgendaRepository(database),
        reminders,
        DailyBriefingRepository(database),
        FrozenClock(NOW),
    )

    assert scheduler.tick() == 0


def test_a_recurring_item_only_covers_the_horizon(database: Database) -> None:
    _agenda(
        database,
        start=datetime(2026, 8, 1, 15, 0, tzinfo=SHANGHAI),
        recurrence=RecurrenceRule("FREQ=DAILY", "Asia/Shanghai"),
    )
    leads = NotificationLeadRepository(database)
    leads.set_override("agenda_test", "user_test", [60], now=NOW)
    reminders = ReminderRepository(database)
    scheduler = NotificationLeadScheduler(
        leads,
        AgendaRepository(database),
        reminders,
        DailyBriefingRepository(database),
        FrozenClock(NOW),
        horizon_days=2,
    )

    created = scheduler.tick()
    assert created == 2
    assert sorted(
        reminder.fire_at for reminder in reminders.list_for_owner("user_test")
    ) == [
        datetime(2026, 8, 1, 14, 0, tzinfo=SHANGHAI),
        datetime(2026, 8, 2, 14, 0, tzinfo=SHANGHAI),
    ]


def test_the_notification_states_when_the_event_starts(database: Database) -> None:
    from zhixu.adapters.storage.sqlite.repositories import (
        _reminder_notification_text,
    )

    start = datetime(2026, 8, 2, 9, 0, tzinfo=SHANGHAI)
    _agenda(database, start=start)
    leads = NotificationLeadRepository(database)
    leads.set_override("agenda_test", "user_test", [120, 1440], now=NOW)
    reminders = ReminderRepository(database)
    NotificationLeadScheduler(
        leads,
        AgendaRepository(database),
        reminders,
        DailyBriefingRepository(database),
        FrozenClock(NOW),
    ).tick()

    stored = sorted(
        reminders.list_for_owner("user_test"), key=lambda r: r.fire_at
    )
    # The title is the event's own name; the lead lives in the rendered card.
    assert [r.title for r in stored] == ["部门会议", "部门会议"]
    assert all(r.related_start_at == start for r in stored)

    day_ahead, two_hours = stored
    early = _reminder_notification_text(day_ahead)
    assert "**事项：** `部门会议`" in early
    assert "2026-08-02 09:00" in early
    assert "周日" in early
    assert "还有 1 天" in early
    assert "（今天）" not in early

    late = _reminder_notification_text(two_hours)
    # Same calendar day, so the date is redundant but the weekday is not.
    assert "09:00" in late
    assert "（今天）" in late
    assert "还有 2 小时" in late


def test_a_plain_reminder_keeps_its_own_time(database: Database) -> None:
    from zhixu.adapters.storage.sqlite.repositories import (
        _reminder_notification_text,
    )
    from zhixu.domain import Reminder

    plain = Reminder(
        id="reminder_plain",
        owner_user_id="user_test",
        title="喝水",
        fire_at=datetime(2026, 8, 1, 15, 0, tzinfo=SHANGHAI),
        target_ref="qqc_target_test",
    )
    text = _reminder_notification_text(plain)
    assert "**时间：** `2026-08-01 15:00（北京时间）`" in text
    assert "距开始" not in text


def test_the_card_is_read_in_the_timezone_of_the_event(database: Database) -> None:
    from zhixu.adapters.storage.sqlite.repositories import (
        _reminder_notification_text,
    )
    from zhixu.domain import AgendaItem

    # An event kept in London: 09:00 there is 16:00 in Shanghai, so a card that
    # silently rendered Shanghai time would name the wrong hour and weekday.
    london = ZoneInfo("Europe/London")
    start = datetime(2026, 8, 6, 9, 0, tzinfo=london)
    item = AgendaItem(
        id="agenda_london",
        owner_user_id="user_test",
        creator_user_id="user_test",
        title="London review",
        start_at=start,
        end_at=start + timedelta(hours=1),
        timezone="Europe/London",
    )
    AgendaRepository(database).create(
        item,
        PolicyEngine().require(
            CommandContext(actor_user_id="user_test", now=NOW),
            Action.CREATE,
            ResourceRef("agenda", item.id, "user_test"),
        ),
    )
    leads = NotificationLeadRepository(database)
    leads.set_override("agenda_london", "user_test", [60], now=NOW)
    reminders = ReminderRepository(database)
    NotificationLeadScheduler(
        leads,
        AgendaRepository(database),
        reminders,
        DailyBriefingRepository(database),
        FrozenClock(NOW),
        horizon_days=10,
    ).tick()

    stored = reminders.list_for_owner("user_test")[0]
    assert stored.timezone == "Europe/London"
    text = _reminder_notification_text(stored)
    assert "09:00" in text
    assert "Europe/London" in text
    assert "北京时间" not in text
    assert "16:00" not in text


def test_a_reminder_without_a_timezone_keeps_the_previous_wording(
    database: Database,
) -> None:
    from zhixu.adapters.storage.sqlite.repositories import (
        _reminder_notification_text,
    )
    from zhixu.domain import Reminder

    legacy = Reminder(
        id="reminder_legacy",
        owner_user_id="user_test",
        title="旧提醒",
        fire_at=datetime(2026, 8, 1, 15, 0, tzinfo=SHANGHAI),
        target_ref="qqc_target_test",
    )
    text = _reminder_notification_text(legacy)
    assert "2026-08-01 15:00（北京时间）" in text
