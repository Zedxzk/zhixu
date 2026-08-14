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


def test_the_prompt_states_both_default_notification_times() -> None:
    """The two defaults are a stated contract, not something the model invents."""

    from zhixu.application.intent_router import _system_prompt

    prompt = _system_prompt()
    assert "20:00" in prompt
    assert "09:00" in prompt
    assert "never invent a different one" in prompt


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


def test_every_outbound_card_shares_one_shape() -> None:
    """One builder defines the style, so confirmations cannot drift apart."""

    from zhixu.application.labels import card

    rendered = card(
        "提醒已设置",
        [("事项", "合成事项"), ("时间", "2026-08-31 09:00 周一")],
        note="接受后才会写入",
    )
    assert rendered.splitlines()[0] == "# 提醒已设置"
    assert "**事项：** `合成事项`" in rendered
    assert "**时间：** `2026-08-31 09:00 周一`" in rendered
    assert rendered.endswith("> 接受后才会写入")


def test_a_moment_is_shown_the_way_a_person_reads_it() -> None:
    """An ISO string with an offset is not a confirmation anyone wants."""

    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from zhixu.application.labels import local_moment

    moment = datetime(2026, 8, 31, 1, 0, tzinfo=UTC)
    shown = local_moment(moment, ZoneInfo("Asia/Shanghai"))

    assert shown == "2026-08-31 09:00 周一"
    assert "T" not in shown
    assert "+08:00" not in shown


def test_no_preview_field_switches_to_a_monospace_value() -> None:
    """A backticked value renders in another typeface inside the same card."""

    import re
    from pathlib import Path

    source = Path("src/zhixu/application/assistant.py").read_text(encoding="utf-8")
    offenders = re.findall(r"\*\*[^*\n]+：\*\* *`", source)
    assert offenders == []


def test_button_labels_fit_the_narrow_qq_keyboard() -> None:
    """QQ clipped 取消创建 to 取消… and 改提前通知 to 改提…"""

    import ast
    from pathlib import Path

    source = Path("src/zhixu/application/assistant.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    labels: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "MessageButton"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            labels.append(node.args[0].value)

    too_long = [value for value in labels if len(value) > 4]
    assert too_long == [], too_long
