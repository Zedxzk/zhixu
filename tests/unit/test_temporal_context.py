from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from zhixu.application.temporal_context import temporal_context, temporal_context_prompt
from zhixu.domain.errors import ValidationError


def test_temporal_context_exposes_relative_calendar_anchors() -> None:
    current = datetime(2026, 12, 31, 23, 45, tzinfo=ZoneInfo("Asia/Shanghai"))

    value = temporal_context(current)

    assert value == {
        "now": "2026-12-31T23:45:00+08:00",
        "timezone": "Asia/Shanghai",
        "utc_offset_seconds": 28800,
        "local_date": "2026-12-31",
        "local_time": "23:45:00",
        "weekday": "Thursday",
        "tomorrow": "2027-01-01",
        "day_after_tomorrow": "2027-01-02",
        "week_start_monday": "2026-12-28",
        "next_week_start_monday": "2027-01-04",
        "month_start": "2026-12-01",
        "next_month_start": "2027-01-01",
    }
    assert temporal_context_prompt(current).startswith("Trusted temporal context: {")


def test_temporal_context_rejects_naive_time() -> None:
    with pytest.raises(ValidationError):
        temporal_context(datetime(2026, 1, 1, 12))
