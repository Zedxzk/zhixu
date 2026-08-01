"""Versioned Hong Kong public-holiday business calendar snapshot.

Source: HKSAR Government 1823 public-holiday iCal, which follows the Gazette.
Snapshot coverage: 2025-2027. Runtime scheduling never depends on public network access.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date

from .errors import ValidationError

SOURCE_URL = "https://www.1823.gov.hk/common/ical/en.ics"
SUPPORTED_YEARS = frozenset({2025, 2026, 2027})
_HOLIDAY_VALUES = """
2025-01-01 2025-01-29 2025-01-30 2025-01-31 2025-04-04 2025-04-18
2025-04-19 2025-04-21 2025-05-01 2025-05-05 2025-05-31 2025-07-01
2025-10-01 2025-10-07 2025-10-29 2025-12-25 2025-12-26
2026-01-01 2026-02-17 2026-02-18 2026-02-19 2026-04-03 2026-04-04
2026-04-06 2026-04-07 2026-05-01 2026-05-25 2026-06-19 2026-07-01
2026-09-26 2026-10-01 2026-10-19 2026-12-25 2026-12-26
2027-01-01 2027-02-06 2027-02-08 2027-02-09 2027-03-26 2027-03-27
2027-03-29 2027-04-05 2027-05-01 2027-05-13 2027-06-09 2027-07-01
2027-09-16 2027-10-01 2027-10-08 2027-12-25 2027-12-27
"""
HOLIDAYS = frozenset(
    date.fromisoformat(value) for value in _HOLIDAY_VALUES.split()
)


def is_hong_kong_business_day(value: date) -> bool:
    if value.year not in SUPPORTED_YEARS:
        raise ValidationError("Hong Kong holiday calendar year is not installed")
    return value.weekday() < 5 and value not in HOLIDAYS


def monthly_business_day(year: int, month: int, position: int) -> date:
    """Return a 1-based or negative-position HK business day within one month."""

    if position == 0 or abs(position) > 31:
        raise ValidationError("business-day position is invalid")
    days = [
        date(year, month, day)
        for day in range(1, monthrange(year, month)[1] + 1)
        if is_hong_kong_business_day(date(year, month, day))
    ]
    try:
        return days[position - 1] if position > 0 else days[position]
    except IndexError as exc:
        raise ValidationError("business-day position is outside the month") from exc
