from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zhixu.application.intent_router import RuleIntentRouter
from zhixu.application.intents import IntentAction


class FrozenClock:
    """2026-08-13 12:00 Asia/Shanghai."""

    def now(self) -> datetime:
        return datetime(2026, 8, 13, 4, 0, tzinfo=UTC)


@pytest.fixture
def router() -> RuleIntentRouter:
    return RuleIntentRouter(FrozenClock())


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/日历", None),
        ("/日历 2026-08", (2026, 8)),
        ("/日历 2026-9", (2026, 9)),
        ("/日历 2027年3月", (2027, 3)),
        ("/日历 本月", (2026, 8)),
        ("/日历 下个月", (2026, 9)),
        ("/日历 上个月", (2026, 7)),
        ("/日历 +3", (2026, 11)),
        ("/日历 -8", (2025, 12)),
        ("/日历 +5", (2027, 1)),
        ("/日历 9月", (2026, 9)),
        ("/日历 3月", (2027, 3)),
        ("/月历 下个月", (2026, 9)),
    ],
)
def test_month_selectors_resolve_deterministically(
    router: RuleIntentRouter,
    command: str,
    expected: tuple[int, int] | None,
) -> None:
    intent = router.route(command)
    assert intent is not None
    assert intent.action is IntentAction.VIEW_CALENDAR
    if expected is None:
        assert "year" not in intent.arguments
    else:
        assert (intent.arguments["year"], intent.arguments["month"]) == expected


@pytest.mark.parametrize("command", ["/日历 明年", "/日历 2026-13", "/日历 abc"])
def test_unreadable_selectors_are_reported_not_silently_ignored(
    router: RuleIntentRouter,
    command: str,
) -> None:
    intent = router.route(command)
    assert intent is not None
    assert intent.action is IntentAction.VIEW_CALENDAR
    assert "invalid_month" in intent.arguments
    assert "year" not in intent.arguments
