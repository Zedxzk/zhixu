"""Trusted, deterministic calendar context for model prompts and agent tools."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from zhixu.domain.errors import ValidationError

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _next_month(value: date) -> date:
    return date(value.year + int(value.month == 12), 1 if value.month == 12 else value.month + 1, 1)


def temporal_context(reference_time: datetime) -> dict[str, Any]:
    """Return explicit local calendar anchors without consulting wall time implicitly."""

    if reference_time.tzinfo is None:
        raise ValidationError("temporal context requires a timezone-aware reference time")
    today = reference_time.date()
    week_start = today - timedelta(days=today.weekday())
    timezone = getattr(reference_time.tzinfo, "key", None) or str(reference_time.tzinfo)
    offset = reference_time.utcoffset()
    return {
        "now": reference_time.isoformat(),
        "timezone": timezone,
        "utc_offset_seconds": int(offset.total_seconds()) if offset is not None else 0,
        "local_date": today.isoformat(),
        "local_time": reference_time.timetz().replace(tzinfo=None).isoformat(),
        "weekday": _WEEKDAYS[today.weekday()],
        "tomorrow": (today + timedelta(days=1)).isoformat(),
        "day_after_tomorrow": (today + timedelta(days=2)).isoformat(),
        "week_start_monday": week_start.isoformat(),
        "next_week_start_monday": (week_start + timedelta(days=7)).isoformat(),
        "month_start": today.replace(day=1).isoformat(),
        "next_month_start": _next_month(today).isoformat(),
    }


def temporal_context_prompt(reference_time: datetime) -> str:
    """Render a compact data block whose shape stays consistent across prompts."""

    return "Trusted temporal context: " + json.dumps(
        temporal_context(reference_time),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
