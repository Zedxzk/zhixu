from __future__ import annotations

from datetime import date

import pytest

from zhixu.application.assistant import _business_day_rule_label
from zhixu.domain.agenda import parse_business_day_rule
from zhixu.domain.errors import ValidationError
from zhixu.domain.hong_kong_calendar import HOLIDAYS as HK_HOLIDAYS
from zhixu.domain.hong_kong_calendar import HONG_KONG
from zhixu.domain.macau_calendar import HOLIDAYS as MO_HOLIDAYS
from zhixu.domain.macau_calendar import MACAU


def test_the_two_regional_calendars_are_not_interchangeable() -> None:
    # 2026-11-01 is a Sunday and 2026-11-02 is 追思節, a Macau holiday only, so
    # the first business day of that month differs between the two regions.
    assert date(2026, 11, 2) in MO_HOLIDAYS
    assert date(2026, 11, 2) not in HK_HOLIDAYS
    assert HONG_KONG.monthly_business_day(2026, 11, 1) == date(2026, 11, 2)
    assert MACAU.monthly_business_day(2026, 11, 1) == date(2026, 11, 3)


def test_compensatory_days_are_not_business_days() -> None:
    # 追思節 falls on a Sunday in 2025, so the Monday after it is a holiday.
    assert not MACAU.is_business_day(date(2025, 11, 3))


def test_a_year_outside_the_snapshot_is_refused() -> None:
    with pytest.raises(ValidationError):
        MACAU.is_business_day(date(2030, 1, 2))


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ("X-BUSINESS-DAY;CALENDAR=MO_GENERAL_HOLIDAYS;BYSETPOS=-1", (MACAU, -1)),
        ("X-BUSINESS-DAY;CALENDAR=HK_GENERAL_HOLIDAYS;BYSETPOS=-2", (HONG_KONG, -2)),
        ("X-BUSINESS-DAY;CALENDAR=SG_GENERAL_HOLIDAYS;BYSETPOS=-1", None),
        ("FREQ=MONTHLY;BYMONTHDAY=-1", None),
    ],
)
def test_rule_parsing_selects_the_requested_calendar(
    rule: str,
    expected: tuple[object, int] | None,
) -> None:
    parsed = parse_business_day_rule(rule)
    if expected is None:
        assert parsed is None
    else:
        assert parsed is not None
        assert (parsed.calendar, parsed.position) == expected


def test_preview_label_names_the_region() -> None:
    macau = parse_business_day_rule("X-BUSINESS-DAY;CALENDAR=MO_GENERAL_HOLIDAYS;BYSETPOS=-1")
    hong_kong = parse_business_day_rule("X-BUSINESS-DAY;CALENDAR=HK_GENERAL_HOLIDAYS;BYSETPOS=-2")
    assert macau is not None and hong_kong is not None
    assert _business_day_rule_label(macau) == "每月最后一个澳门工作日"
    # The Hong Kong wording predates the Macau calendar and must not change.
    assert _business_day_rule_label(hong_kong) == "每月倒数第二个香港工作日"
