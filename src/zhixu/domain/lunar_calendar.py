"""Versioned Chinese lunisolar calendar snapshot.

Source: Hong Kong Observatory Gregorian-Lunar Calendar Conversion Table.
        https://www.hko.gov.hk/en/gts/time/conversion.htm
The Observatory computes the lunisolar calendar against the 120 degrees East
meridian, the same basis as GB/T 33661-2017 as edited by Purple Mountain
Observatory, so this snapshot is the mainland calendar. Its Lunar New Year,
Dragon Boat and Mid-Autumn dates agree with the independently vendored
public-holiday table in hong_kong_calendar.
Snapshot coverage: lunar years 2000-2027. Extend one year at a time; runtime
scheduling never depends on public network access.

Each row is: lunar year, solar date of the first day of month 1, the
intercalary month number or 0, then the length of every month in order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .errors import ValidationError

SOURCE_URL = "https://www.hko.gov.hk/en/gts/time/conversion.htm"

_YEAR_VALUES = """
2000 2000-02-05 0 30,30,29,29,30,29,29,30,29,30,30,29
2001 2001-01-24 4 30,30,29,30,29,30,29,29,30,29,30,29,30
2002 2002-02-12 0 30,30,29,30,29,30,29,29,30,29,30,29
2003 2003-02-01 0 30,30,29,30,30,29,30,29,29,30,29,30
2004 2004-01-22 2 29,30,29,30,30,29,30,29,30,29,30,29,30
2005 2005-02-09 0 29,30,29,30,29,30,30,29,30,29,30,29
2006 2006-01-29 7 30,29,30,29,30,29,30,29,30,30,29,30,30
2007 2007-02-18 0 29,29,30,29,29,30,29,30,30,30,29,30
2008 2008-02-07 0 30,29,29,30,29,29,30,29,30,30,29,30
2009 2009-01-26 5 30,30,29,29,30,29,29,30,29,30,29,30,30
2010 2010-02-14 0 30,29,30,29,30,29,29,30,29,30,29,30
2011 2011-02-03 0 30,29,30,30,29,30,29,29,30,29,30,29
2012 2012-01-23 4 30,29,30,30,29,30,29,30,29,30,29,30,29
2013 2013-02-10 0 30,29,30,29,30,30,29,30,29,30,29,30
2014 2014-01-31 9 29,30,29,30,29,30,29,30,30,29,30,29,30
2015 2015-02-19 0 29,30,29,29,30,29,30,30,30,29,30,29
2016 2016-02-08 0 30,29,30,29,29,30,29,30,30,29,30,30
2017 2017-01-28 6 29,30,29,30,29,29,30,29,30,29,30,30,30
2018 2018-02-16 0 29,30,29,30,29,29,30,29,30,29,30,30
2019 2019-02-05 0 30,29,30,29,30,29,29,30,29,29,30,30
2020 2020-01-25 4 29,30,30,30,29,30,29,29,30,29,30,29,30
2021 2021-02-12 0 29,30,30,29,30,29,30,29,30,29,30,29
2022 2022-02-01 0 30,29,30,29,30,30,29,30,29,30,29,30
2023 2023-01-22 2 29,30,29,29,30,30,29,30,30,29,30,29,30
2024 2024-02-10 0 29,30,29,29,30,29,30,30,29,30,30,29
2025 2025-01-29 6 30,29,30,29,29,30,29,30,29,30,30,30,29
2026 2026-02-17 0 30,29,30,29,29,30,29,29,30,30,30,29
2027 2027-02-06 0 30,30,29,30,29,29,30,29,29,30,30,29
"""


@dataclass(frozen=True, slots=True)
class LunarDate:
    year: int
    month: int
    day: int
    leap: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12:
            raise ValidationError("lunar month is out of range")
        if not 1 <= self.day <= 30:
            raise ValidationError("lunar day is out of range")


@dataclass(frozen=True, slots=True)
class _LunarYear:
    start: date
    leap_month: int
    lengths: tuple[int, ...]

    def months(self) -> tuple[tuple[int, bool, int], ...]:
        result: list[tuple[int, bool, int]] = []
        number = 1
        seen_leap = False
        for length in self.lengths:
            leap = not seen_leap and self.leap_month != 0 and number - 1 == self.leap_month
            if leap:
                seen_leap = True
                result.append((self.leap_month, True, length))
                continue
            result.append((number, False, length))
            number += 1
        return tuple(result)


def _load() -> dict[int, _LunarYear]:
    table: dict[int, _LunarYear] = {}
    for line in _YEAR_VALUES.split("\n"):
        if not line.strip():
            continue
        year, start, leap_month, lengths = line.split()
        table[int(year)] = _LunarYear(
            date.fromisoformat(start),
            int(leap_month),
            tuple(int(value) for value in lengths.split(",")),
        )
    return table


_YEARS = _load()
SUPPORTED_LUNAR_YEARS = frozenset(_YEARS)


def _year(lunar_year: int) -> _LunarYear:
    entry = _YEARS.get(lunar_year)
    if entry is None:
        raise ValidationError("lunisolar calendar year is not installed")
    return entry


def month_length(lunar_year: int, month: int, *, leap: bool = False) -> int:
    for number, is_leap, length in _year(lunar_year).months():
        if number == month and is_leap == leap:
            return length
    raise ValidationError("lunisolar month does not exist in that year")


def to_solar(value: LunarDate, *, clamp: bool = False) -> date:
    """Convert a lunar date to its Gregorian date.

    A day that the month does not reach, such as the thirtieth of a short
    month, is rejected unless ``clamp`` moves it to the last day of the month.
    """

    entry = _year(value.year)
    offset = 0
    for number, is_leap, length in entry.months():
        if number == value.month and is_leap == value.leap:
            if value.day > length:
                if not clamp:
                    raise ValidationError("lunisolar day does not exist in that month")
                return entry.start + timedelta(days=offset + length - 1)
            return entry.start + timedelta(days=offset + value.day - 1)
        offset += length
    raise ValidationError("lunisolar month does not exist in that year")


def from_solar(value: date) -> LunarDate:
    for lunar_year in sorted(_YEARS):
        entry = _YEARS[lunar_year]
        span = sum(entry.lengths)
        if not entry.start <= value < entry.start + timedelta(days=span):
            continue
        offset = (value - entry.start).days
        for number, is_leap, length in entry.months():
            if offset < length:
                return LunarDate(lunar_year, number, offset + 1, is_leap)
            offset -= length
    raise ValidationError("lunisolar calendar year is not installed")


def leap_month(lunar_year: int) -> int:
    """Return the intercalary month number, or 0 when the year has none."""

    return _year(lunar_year).leap_month
