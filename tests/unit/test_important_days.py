from __future__ import annotations

from datetime import date

import pytest

from zhixu.application.scheduler import _important_day_lines
from zhixu.domain import (
    DEFAULT_ANNIVERSARY_ADVANCE_DAYS,
    UNKNOWN_YEAR,
    Anniversary,
    CalendarSystem,
    ImportantDayKind,
)
from zhixu.domain.errors import ValidationError


def _day(**overrides: object) -> Anniversary:
    fields: dict[str, object] = {
        "id": "anniversary_test",
        "owner_user_id": "user_test",
        "creator_user_id": "user_test",
        "title": "结婚",
        "anchor_date": date(2020, 5, 20),
        "timezone": "Asia/Shanghai",
    }
    fields.update(overrides)
    return Anniversary(**fields)  # type: ignore[arg-type]


def test_a_solar_anniversary_recurs_on_its_month_and_day() -> None:
    value = _day()
    assert value.occurrence_in(2026) == date(2026, 5, 20)
    assert value.elapsed_years(date(2026, 5, 20)) == 6
    assert value.next_occurrence(date(2026, 8, 1)) == date(2027, 5, 20)


def test_a_leap_day_anniversary_falls_back_to_the_twenty_eighth() -> None:
    value = _day(anchor_date=date(2020, 2, 29))
    assert value.occurrence_in(2024) == date(2024, 2, 29)
    assert value.occurrence_in(2026) == date(2026, 2, 28)


def test_a_lunar_birthday_moves_with_the_lunisolar_calendar() -> None:
    value = _day(
        title="奶奶",
        kind=ImportantDayKind.BIRTHDAY,
        calendar=CalendarSystem.LUNAR,
        anchor_date=date(1960, 1, 1),
        lunar_month=7,
        lunar_day=25,
    )
    # The same lunar date lands on a different Gregorian day every year.
    assert value.occurrence_in(2025) == date(2025, 9, 16)
    assert value.occurrence_in(2026) == date(2026, 9, 6)
    assert value.occurrence_in(2027) == date(2027, 8, 26)


def test_a_birthday_does_not_count_elapsed_days() -> None:
    birthday = _day(kind=ImportantDayKind.BIRTHDAY)
    anniversary = _day()
    assert not birthday.counts_elapsed_days()
    assert anniversary.counts_elapsed_days()


def test_advance_notice_fires_only_on_the_configured_days() -> None:
    value = _day(advance_days=DEFAULT_ANNIVERSARY_ADVANCE_DAYS)
    assert value.advance_notice_for(date(2026, 4, 20)) == (date(2026, 5, 20), 30)
    assert value.advance_notice_for(date(2026, 5, 5)) == (date(2026, 5, 20), 15)
    assert value.advance_notice_for(date(2026, 5, 13)) == (date(2026, 5, 20), 7)
    assert value.advance_notice_for(date(2026, 5, 12)) is None


def test_a_lunar_entry_needs_a_lunar_date_and_a_solar_one_must_not_carry_it() -> None:
    with pytest.raises(ValidationError):
        _day(calendar=CalendarSystem.LUNAR)
    with pytest.raises(ValidationError):
        _day(lunar_month=7, lunar_day=25)
    with pytest.raises(ValidationError):
        _day(advance_days=(0,))
    with pytest.raises(ValidationError):
        _day(advance_days=(7, 7))


def test_briefing_renders_an_anniversary_day_count_on_ordinary_days() -> None:
    lines = _important_day_lines(_day(advance_days=(30, 15, 7)), date(2026, 8, 1))
    assert lines == ["今天是 结婚的第 **2265** 天。"]


def test_briefing_announces_the_anniversary_and_keeps_the_count() -> None:
    lines = _important_day_lines(_day(advance_days=(30, 15, 7)), date(2026, 5, 20))
    assert lines[0] == "🎉 今天是 结婚 的 **6** 周年。"
    assert lines[1].startswith("今天是 结婚的第")


def test_briefing_gives_the_anniversary_advance_notice() -> None:
    lines = _important_day_lines(_day(advance_days=(30, 15, 7)), date(2026, 5, 5))
    assert lines[0] == "🎉 结婚 的 6 周年还有 **15** 天（05月20日）。"


def test_briefing_says_nothing_for_a_birthday_that_is_not_near() -> None:
    birthday = _day(
        title="张三",
        kind=ImportantDayKind.BIRTHDAY,
        anchor_date=date(1995, 5, 20),
        advance_days=(7, 3, 1),
    )
    assert _important_day_lines(birthday, date(2026, 8, 1)) == []
    assert _important_day_lines(birthday, date(2026, 5, 17)) == [
        "🎂 张三 的生日还有 **3** 天（05月20日）。"
    ]
    assert _important_day_lines(birthday, date(2026, 5, 20)) == [
        "🎂 今天是 张三 的生日（31 岁）。"
    ]


def test_briefing_omits_an_age_when_the_birth_year_is_unknown() -> None:
    # A date recorded without its year anchors on the sentinel year, which must
    # never be counted as an age.
    birthday = _day(
        title="奶奶",
        kind=ImportantDayKind.BIRTHDAY,
        calendar=CalendarSystem.LUNAR,
        anchor_date=date(UNKNOWN_YEAR, 1, 1),
        lunar_month=7,
        lunar_day=25,
    )
    assert not birthday.has_known_year()
    assert birthday.elapsed_years(date(2026, 9, 6)) == 0
    assert _important_day_lines(birthday, date(2026, 9, 6)) == [
        "🎂 今天是 奶奶 的生日。"
    ]


def test_a_solar_birthday_without_a_year_still_recurs() -> None:
    birthday = _day(
        title="同事",
        kind=ImportantDayKind.BIRTHDAY,
        anchor_date=date(UNKNOWN_YEAR, 8, 20),
        advance_days=(3,),
    )
    assert birthday.occurrence_in(2026) == date(2026, 8, 20)
    assert _important_day_lines(birthday, date(2026, 8, 20)) == [
        "🎂 今天是 同事 的生日。"
    ]


def test_plan_preview_names_the_kind_and_calendar_it_will_store() -> None:
    # The preview is what the user confirms against; labelling a birthday as an
    # anniversary is the difference between accepting and rejecting the plan.
    from zhixu.application.assistant import _important_day_preview_lines

    lines = _important_day_preview_lines(
        {
            "title": "香宝生日",
            "anchor_date": date(2001, 9, 20),
            "kind": "birthday",
            "calendar": "solar",
            "advance_days": (7, 3, 1),
        }
    )
    assert any("**生日：** 香宝生日" in line for line in lines)
    assert not any("纪念日" in line for line in lines)
    assert any("出生日期" in line for line in lines)
    assert any("提前预告" in line for line in lines)

    lunar_lines = _important_day_preview_lines(
        {
            "title": "奶奶",
            "anchor_date": date(UNKNOWN_YEAR, 1, 1),
            "kind": "birthday",
            "calendar": "lunar",
            "lunar_month": 7,
            "lunar_day": 25,
        }
    )
    assert any("农历日期" in line and "7月25日" in line for line in lunar_lines)
    assert not any("出生年" in line for line in lunar_lines)
