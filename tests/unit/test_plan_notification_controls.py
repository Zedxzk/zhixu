from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zhixu.application.intent_router import RuleIntentRouter
from zhixu.application.intents import IntentAction
from zhixu.channels import DailyAgendaPreview
from zhixu.domain.errors import ValidationError

PLAN = "plan_abcdefgh1234"


class FrozenClock:
    def now(self) -> datetime:
        return datetime(2026, 8, 13, 4, 0, tzinfo=UTC)


@pytest.fixture
def router() -> RuleIntentRouter:
    return RuleIntentRouter(FrozenClock())


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (f"/计划通知 {PLAN}", {"plan_id": PLAN}),
        (f"/计划通知 {PLAN} 09:00", {"plan_id": PLAN, "time_of_day": "09:00"}),
        (f"/计划通知 {PLAN} 8:30", {"plan_id": PLAN, "time_of_day": "08:30"}),
        (f"/计划免通知 {PLAN}", {"plan_id": PLAN, "disable": True}),
    ],
)
def test_notification_buttons_parse_deterministically(
    router: RuleIntentRouter,
    command: str,
    expected: dict[str, object],
) -> None:
    intent = router.route(command)
    assert intent is not None
    assert intent.action is IntentAction.ADJUST_PLAN_NOTIFICATION
    assert intent.arguments == expected
    assert intent.source == "deterministic"


@pytest.mark.parametrize(
    "command",
    [f"/计划通知 {PLAN} 25:00", f"/计划通知 {PLAN} 09:60", "/计划通知 not_a_plan 09:00"],
)
def test_out_of_range_button_payloads_are_refused(
    router: RuleIntentRouter,
    command: str,
) -> None:
    intent = router.route(command)
    assert intent is None or intent.action is not IntentAction.ADJUST_PLAN_NOTIFICATION


def test_entries_queued_before_titles_existed_still_load() -> None:
    """Rows written by the previous release carry three-field entries."""

    from zhixu.delivery.outbox import _message_from_row

    row = {
        "channel": "qq",
        "channel_account": "bot_test_a",
        "target_ref": "qqc_test",
        "message_kind": "text",
        "payload_json": (
            '{"text":"synthetic","buttons":[],"attachment_url":null,'
            '"calendar_preview":null,'
            '"daily_agenda_preview":{"year":2026,"month":8,"day":13,'
            '"entries":[[540,600,"agenda"]],"anniversary_day_numbers":[]},'
            '"reply_context_ref":""}'
        ),
        "classification": 1,
    }
    message = _message_from_row(row)
    assert message.daily_agenda_preview is not None
    assert message.daily_agenda_preview.entries == ((540, 600, "agenda", ""),)


def test_preview_rejects_an_over_long_title() -> None:
    with pytest.raises(ValidationError):
        DailyAgendaPreview(
            year=2026,
            month=8,
            day=13,
            entries=((540, 600, "agenda", "x" * 61),),
        )
