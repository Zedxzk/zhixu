"""Versioned Macau public-holiday business calendar snapshot.

Source: Macao SAR Government Portal public-holiday tables and their iCal feed,
which follow the Boletim Oficial. Compensatory days (補假) are included because
they are genuine non-working days for Macau public institutions.
Snapshot coverage: 2025-2027. Runtime scheduling never depends on public network access.
"""

from __future__ import annotations

from datetime import date

from .business_calendar import BusinessCalendar, parse_holiday_values

SOURCE_URL = "https://www.gov.mo/en/public-holidays/ical/"
SUPPORTED_YEARS = frozenset({2025, 2026, 2027})

# Gazetted public holidays. Macau keeps several days Hong Kong does not:
# 復活節前日, 追思節, 聖母無原罪瞻禮, 澳門特別行政區成立紀念日, 冬至 and 聖誕節前日.
_HOLIDAY_VALUES = """
2025-01-01 2025-01-29 2025-01-30 2025-01-31 2025-04-04 2025-04-18
2025-04-19 2025-05-01 2025-05-05 2025-05-31 2025-10-01 2025-10-02
2025-10-07 2025-10-29 2025-11-02 2025-12-08 2025-12-20 2025-12-21
2025-12-24 2025-12-25
2026-01-01 2026-02-17 2026-02-18 2026-02-19 2026-04-03 2026-04-04
2026-04-05 2026-05-01 2026-05-24 2026-06-19 2026-09-26 2026-10-01
2026-10-02 2026-10-18 2026-11-02 2026-12-08 2026-12-20 2026-12-22
2026-12-24 2026-12-25
2027-01-01 2027-02-06 2027-02-07 2027-02-08 2027-03-26 2027-03-27
2027-04-05 2027-05-01 2027-05-13 2027-06-09 2027-09-16 2027-10-01
2027-10-02 2027-10-08 2027-11-02 2027-12-08 2027-12-20 2027-12-22
2027-12-24 2027-12-25
"""

# Compensatory days granted when a public holiday falls on a Sunday.
_COMPENSATORY_VALUES = """
2025-04-21 2025-06-02 2025-11-03 2025-12-22 2025-12-23
2026-04-06 2026-04-07 2026-05-25 2026-09-28 2026-10-19 2026-12-21
2027-02-09 2027-02-10 2027-03-29 2027-05-03 2027-10-04 2027-12-27
"""

HOLIDAYS = parse_holiday_values(_HOLIDAY_VALUES) | parse_holiday_values(
    _COMPENSATORY_VALUES
)

MACAU = BusinessCalendar(
    token="MO_GENERAL_HOLIDAYS",
    label="Macau",
    supported_years=SUPPORTED_YEARS,
    holidays=HOLIDAYS,
)


def is_macau_business_day(value: date) -> bool:
    return MACAU.is_business_day(value)


def monthly_business_day(year: int, month: int, position: int) -> date:
    """Return a 1-based or negative-position Macau business day within one month."""

    return MACAU.monthly_business_day(year, month, position)
