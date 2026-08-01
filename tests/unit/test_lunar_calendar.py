from __future__ import annotations

from datetime import date, timedelta

import pytest

from zhixu.domain.errors import ValidationError
from zhixu.domain.hong_kong_calendar import HOLIDAYS
from zhixu.domain.lunar_calendar import (
    SUPPORTED_LUNAR_YEARS,
    LunarDate,
    from_solar,
    leap_month,
    month_length,
    to_solar,
)

# Lunar New Year, Dragon Boat and the day after Mid-Autumn are Hong Kong public
# holidays, so the independently vendored holiday table cross-checks this
# snapshot. A wrong month length would move at least one of these.
LUNAR_NEW_YEAR = {2025: date(2025, 1, 29), 2026: date(2026, 2, 17), 2027: date(2027, 2, 6)}
DRAGON_BOAT = {2025: date(2025, 5, 31), 2026: date(2026, 6, 19), 2027: date(2027, 6, 9)}
MID_AUTUMN = {2025: date(2025, 10, 6), 2026: date(2026, 9, 25), 2027: date(2027, 9, 15)}


@pytest.mark.parametrize(("lunar_year", "expected"), sorted(LUNAR_NEW_YEAR.items()))
def test_lunar_new_year_matches_the_public_holiday_table(
    lunar_year: int,
    expected: date,
) -> None:
    assert to_solar(LunarDate(lunar_year, 1, 1)) == expected
    assert expected in HOLIDAYS


@pytest.mark.parametrize(("lunar_year", "expected"), sorted(DRAGON_BOAT.items()))
def test_dragon_boat_matches_the_public_holiday_table(
    lunar_year: int,
    expected: date,
) -> None:
    assert to_solar(LunarDate(lunar_year, 5, 5)) == expected
    assert expected in HOLIDAYS


@pytest.mark.parametrize(("lunar_year", "expected"), sorted(MID_AUTUMN.items()))
def test_the_day_after_mid_autumn_is_the_public_holiday(
    lunar_year: int,
    expected: date,
) -> None:
    assert to_solar(LunarDate(lunar_year, 8, 15)) == expected
    assert expected + timedelta(days=1) in HOLIDAYS


def test_known_intercalary_months() -> None:
    # The intercalary sequence is fixed history; a mis-parsed row would move it.
    assert {year for year in SUPPORTED_LUNAR_YEARS if leap_month(year)} == {
        2001, 2004, 2006, 2009, 2012, 2014, 2017, 2020, 2023, 2025
    }
    assert leap_month(2020) == 4
    assert leap_month(2023) == 2
    assert to_solar(LunarDate(2020, 4, 1, leap=True)) == date(2020, 5, 23)
    assert to_solar(LunarDate(2020, 4, 1)) == date(2020, 4, 23)
    assert to_solar(LunarDate(2023, 2, 1, leap=True)) == date(2023, 3, 22)


def test_round_trip_across_every_installed_day() -> None:
    for lunar_year in sorted(SUPPORTED_LUNAR_YEARS):
        start = to_solar(LunarDate(lunar_year, 1, 1))
        cursor = start
        end = start + timedelta(days=sum(
            month_length(lunar_year, number, leap=leap)
            for number, leap in _months(lunar_year)
        ))
        while cursor < end:
            assert to_solar(from_solar(cursor)) == cursor
            cursor += timedelta(days=1)


def _months(lunar_year: int) -> list[tuple[int, bool]]:
    result: list[tuple[int, bool]] = []
    for number in range(1, 13):
        result.append((number, False))
        if leap_month(lunar_year) == number:
            result.append((number, True))
    return result


def test_every_installed_year_is_structurally_sound() -> None:
    for lunar_year in sorted(SUPPORTED_LUNAR_YEARS):
        months = _months(lunar_year)
        assert len(months) == (13 if leap_month(lunar_year) else 12)
        lengths = [month_length(lunar_year, n, leap=lp) for n, lp in months]
        assert set(lengths) <= {29, 30}
        # A lunisolar year stays inside the astronomical bounds.
        assert 353 <= sum(lengths) <= 385


def test_consecutive_years_are_contiguous() -> None:
    years = sorted(SUPPORTED_LUNAR_YEARS)
    for lunar_year, following in zip(years, years[1:], strict=False):
        if following != lunar_year + 1:
            continue
        span = sum(
            month_length(lunar_year, n, leap=lp) for n, lp in _months(lunar_year)
        )
        assert to_solar(LunarDate(lunar_year, 1, 1)) + timedelta(days=span) == to_solar(
            LunarDate(following, 1, 1)
        )


def test_a_day_the_month_never_reaches_is_rejected_unless_clamped() -> None:
    short = next(
        (year, number, leap)
        for year in sorted(SUPPORTED_LUNAR_YEARS)
        for number, leap in _months(year)
        if month_length(year, number, leap=leap) == 29
    )
    lunar_year, number, leap = short
    value = LunarDate(lunar_year, number, 30, leap)
    with pytest.raises(ValidationError):
        to_solar(value)
    assert to_solar(value, clamp=True) == to_solar(
        LunarDate(lunar_year, number, 29, leap)
    )


def test_years_outside_the_snapshot_fail_loudly() -> None:
    with pytest.raises(ValidationError):
        to_solar(LunarDate(1899, 1, 1))
    with pytest.raises(ValidationError):
        from_solar(date(1899, 1, 1))
    with pytest.raises(ValidationError):
        LunarDate(2026, 13, 1)
