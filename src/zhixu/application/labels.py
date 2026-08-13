"""Shared presentation labels for agenda and reminder lines.

Kept apart from the assistant so the scheduler can render identical lines
without depending on it.
"""

from __future__ import annotations

from datetime import datetime

# U+1F550 starts the o'clock faces and U+1F55C the half-past faces, each
# running one through twelve.
_CLOCK_ON_THE_HOUR = 0x1F550
_CLOCK_HALF_PAST = 0x1F55C

REMINDER_MARK = "⏰"


def agenda_mark(when: datetime) -> str:
    """A clock face showing roughly when the event starts.

    The calendar emoji is unusable here: its artwork carries a printed date,
    so every entry appears to happen on the same day whatever we pass.
    """

    hour = when.hour % 12 or 12
    base = _CLOCK_HALF_PAST if when.minute >= 30 else _CLOCK_ON_THE_HOUR
    return chr(base + hour - 1)
