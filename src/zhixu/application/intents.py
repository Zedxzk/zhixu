"""Deterministic intents and strictly validated model proposals."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhixu.channels import CalendarPreview, MessageButton


class IntentAction(StrEnum):
    HELP = "help"
    LIST_AGENDA = "list_agenda"
    VIEW_CALENDAR = "view_calendar"
    CREATE_AGENDA = "create_agenda"
    CREATE_ANNIVERSARY = "create_anniversary"
    CREATE_DAILY_BRIEFING = "create_daily_briefing"
    CONFIRM_PLAN = "confirm_plan"
    REJECT_PLAN = "reject_plan"
    LIST_ANNIVERSARIES = "list_anniversaries"
    LIST_DAILY_BRIEFINGS = "list_daily_briefings"
    LIST_TASKS = "list_tasks"
    LIST_REMINDERS = "list_reminders"
    SEARCH_NOTES = "search_notes"
    CREATE_TASK = "create_task"
    CREATE_NOTE = "create_note"
    CREATE_REMINDER = "create_reminder"
    CANCEL_REMINDER = "cancel_reminder"
    ACKNOWLEDGE_REMINDER = "acknowledge_reminder"
    SNOOZE_REMINDER = "snooze_reminder"
    COMPLETE_TASK = "complete_task"
    POSTPONE_TASK = "postpone_task"
    SUMMARIZE_NOTES = "summarize_notes"
    ANSWER = "answer"
    DELETE_RESOURCE = "delete_resource"


@dataclass(frozen=True, slots=True)
class ParsedIntent:
    action: IntentAction
    arguments: dict[str, Any] = field(default_factory=dict, repr=False)
    source: str = "deterministic"
    requires_confirmation: bool = False


class ModelIntentProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: IntentAction
    confidence: float = Field(ge=0, le=1)
    query: str | None = Field(default=None, max_length=500)
    title: str | None = Field(default=None, max_length=500)
    answer: str | None = Field(default=None, max_length=4000)
    fire_at: datetime | None = None
    due_at: datetime | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    recurrence_rule: str | None = Field(default=None, max_length=500)
    anchor_date: date | None = None
    briefing_time: time | None = None
    notifications: list[ModelNotificationProposal] = Field(
        default_factory=list,
        max_length=8,
    )
    task_id: str | None = Field(default=None, max_length=160)
    reminder_id: str | None = Field(default=None, max_length=160)
    resource_id: str | None = Field(default=None, max_length=160)

    @field_validator("fire_at", "due_at", "start_at", "end_at")
    @classmethod
    def require_aware_datetime(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("model-proposed datetimes must include a timezone")
        return value


class ModelNotificationProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    time_of_day: time
    day_offset: int = Field(default=0, ge=-366, le=366)
    text: str = Field(min_length=1, max_length=500)

    @field_validator("time_of_day")
    @classmethod
    def require_wall_time(cls, value: time) -> time:
        if value.tzinfo is not None:
            raise ValueError("notification time must be a local wall time")
        return value


@dataclass(frozen=True, slots=True)
class AssistantReply:
    text: str
    code: str
    source: str
    buttons: tuple[MessageButton, ...] = field(default_factory=tuple, repr=False)
    rich_text: bool = False
    calendar_preview: CalendarPreview | None = field(default=None, repr=False)
