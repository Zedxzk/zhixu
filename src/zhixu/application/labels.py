"""Shared presentation labels for agenda and reminder lines.

Kept apart from the assistant so the scheduler can render identical lines
without depending on it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

# U+1F550 starts the o'clock faces and U+1F55C the half-past faces, each
# running one through twelve.
_CLOCK_ON_THE_HOUR = 0x1F550
_CLOCK_HALF_PAST = 0x1F55C

REMINDER_MARK = "⏰"


_MARKDOWN_SPECIALS = re.compile(r"([\\`*_{}\[\]()#+\-.!>|])")
_WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")


def escape_markdown(value: str) -> str:
    return _MARKDOWN_SPECIALS.sub(r"\\\1", value)


def local_moment(value: datetime, timezone: ZoneInfo, *, with_weekday: bool = True) -> str:
    """A timestamp a person reads, not an ISO string with an offset."""

    local = value.astimezone(timezone)
    stamp = f"{local:%Y-%m-%d %H:%M}"
    return f"{stamp} 周{_WEEKDAYS[local.weekday()]}" if with_weekday else stamp


def card(title: str, fields: Sequence[tuple[str, str]], *, note: str = "") -> str:
    """One outbound card shape, so every confirmation reads the same.

    ``fields`` are already-escaped label/value pairs; ``note`` becomes a quoted
    footer. Kept here rather than in the assistant so the scheduler and the
    repositories can produce identical cards.
    """

    lines = [f"# {title}"]
    for label, value in fields:
        lines.extend(["", f"**{label}：** {value}"])
    if note:
        lines.extend(["", f"> {note}"])
    return "\n".join(lines)


def agenda_mark(when: datetime) -> str:
    """A clock face showing roughly when the event starts.

    The calendar emoji is unusable here: its artwork carries a printed date,
    so every entry appears to happen on the same day whatever we pass.
    """

    hour = when.hour % 12 or 12
    base = _CLOCK_HALF_PAST if when.minute >= 30 else _CLOCK_ON_THE_HOUR
    return chr(base + hour - 1)
