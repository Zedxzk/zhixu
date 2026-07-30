from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from zhixu.domain import (
    AgendaItem,
    DataClassification,
    Note,
    RecurrenceRule,
    Task,
    TaskStatus,
    occurrences_between,
)
from zhixu.domain.errors import ClassificationNotSupported, InvalidTransition


def test_daily_recurrence_keeps_wall_time_across_dst() -> None:
    timezone = ZoneInfo("Europe/Paris")
    item = AgendaItem(
        id="agenda_dst",
        owner_user_id="user_test",
        title="Synthetic daily event",
        start_at=datetime(2026, 3, 28, 9, 0, tzinfo=timezone),
        end_at=datetime(2026, 3, 28, 10, 0, tzinfo=timezone),
        timezone="Europe/Paris",
        recurrence=RecurrenceRule("FREQ=DAILY;COUNT=4", "Europe/Paris"),
    )

    occurrences = occurrences_between(
        item,
        datetime(2026, 3, 27, tzinfo=UTC),
        datetime(2026, 4, 2, tzinfo=UTC),
    )

    assert [occurrence.start_at.hour for occurrence in occurrences] == [9, 9, 9, 9]
    assert [occurrence.start_at.utcoffset().total_seconds() for occurrence in occurrences] == [
        3600,
        7200,
        7200,
        7200,
    ]


def test_occurrence_window_can_be_queried_from_another_timezone() -> None:
    event_timezone = ZoneInfo("Asia/Shanghai")
    query_timezone = ZoneInfo("America/New_York")
    item = AgendaItem(
        id="agenda_cross_timezone",
        owner_user_id="user_test",
        title="Synthetic cross-timezone event",
        start_at=datetime(2026, 6, 1, 9, 0, tzinfo=event_timezone),
        end_at=datetime(2026, 6, 1, 10, 0, tzinfo=event_timezone),
        timezone="Asia/Shanghai",
    )

    occurrences = occurrences_between(
        item,
        datetime(2026, 5, 31, 20, 0, tzinfo=query_timezone),
        datetime(2026, 6, 1, 2, 0, tzinfo=query_timezone),
    )

    assert len(occurrences) == 1
    assert occurrences[0].start_at.astimezone(query_timezone) == datetime(
        2026,
        5,
        31,
        21,
        0,
        tzinfo=query_timezone,
    )


def test_month_end_and_leap_day_follow_rfc_recurrence_semantics() -> None:
    timezone = ZoneInfo("UTC")
    monthly = AgendaItem(
        id="agenda_month_end",
        owner_user_id="user_test",
        title="Synthetic month end",
        start_at=datetime(2026, 1, 31, 9, tzinfo=timezone),
        end_at=datetime(2026, 1, 31, 10, tzinfo=timezone),
        timezone="UTC",
        recurrence=RecurrenceRule("FREQ=MONTHLY;COUNT=3", "UTC"),
    )
    leap = AgendaItem(
        id="agenda_leap",
        owner_user_id="user_test",
        title="Synthetic leap day",
        start_at=datetime(2024, 2, 29, 9, tzinfo=timezone),
        end_at=datetime(2024, 2, 29, 10, tzinfo=timezone),
        timezone="UTC",
        recurrence=RecurrenceRule("FREQ=YEARLY;COUNT=3", "UTC"),
    )

    monthly_dates = [
        occurrence.start_at.date()
        for occurrence in occurrences_between(
            monthly,
            datetime(2026, 1, 1, tzinfo=timezone),
            datetime(2026, 7, 1, tzinfo=timezone),
        )
    ]
    leap_years = [
        occurrence.start_at.year
        for occurrence in occurrences_between(
            leap,
            datetime(2024, 1, 1, tzinfo=timezone),
            datetime(2033, 1, 1, tzinfo=timezone),
        )
    ]

    assert [value.isoformat() for value in monthly_dates] == [
        "2026-01-31",
        "2026-03-31",
        "2026-05-31",
    ]
    assert leap_years == [2024, 2028, 2032]


def test_terminal_task_cannot_be_postponed_or_reopened() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    task = Task("task_test", "user_test", "Synthetic task")
    completed = task.transition(TaskStatus.COMPLETED, now=now)

    with pytest.raises(InvalidTransition):
        completed.postpone(datetime(2026, 1, 2, tzinfo=UTC), now=now)
    with pytest.raises(InvalidTransition):
        completed.transition(TaskStatus.PENDING, now=now)


@pytest.mark.parametrize(
    "classification",
    [DataClassification.SECRET, DataClassification.PROHIBITED],
)
def test_high_sensitivity_data_is_rejected_before_vault(
    classification: DataClassification,
) -> None:
    with pytest.raises(ClassificationNotSupported) as captured:
        Note(
            id="note_rejected",
            owner_user_id="user_test",
            title="Synthetic",
            body="Synthetic",
            classification=classification,
        )

    assert captured.value.code == "classification_not_supported"
