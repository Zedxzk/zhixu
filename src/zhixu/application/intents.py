"""Deterministic intents and strictly validated model proposals."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhixu.channels import CalendarPreview, MessageButton


class IntentAction(StrEnum):
    HELP = "help"
    LIST_AGENDA = "list_agenda"
    LIST_ALL_AGENDA = "list_all_agenda"
    VIEW_CALENDAR = "view_calendar"
    CREATE_AGENDA = "create_agenda"
    CREATE_ANNIVERSARY = "create_anniversary"
    SET_NOTIFICATION_LEADS = "set_notification_leads"
    UPDATE_ANNIVERSARY = "update_anniversary"
    DELETE_ANNIVERSARY = "delete_anniversary"
    UPDATE_DAILY_BRIEFING = "update_daily_briefing"
    DELETE_DAILY_BRIEFING = "delete_daily_briefing"
    UPDATE_AGENDA_NOTIFICATION = "update_agenda_notification"
    DELETE_AGENDA_NOTIFICATION = "delete_agenda_notification"
    CREATE_DAILY_BRIEFING = "create_daily_briefing"
    CONFIRM_PLAN = "confirm_plan"
    REJECT_PLAN = "reject_plan"
    CANCEL_PLAN = "cancel_plan"
    LIST_ANNIVERSARIES = "list_anniversaries"
    LIST_DAILY_BRIEFINGS = "list_daily_briefings"
    LIST_TASKS = "list_tasks"
    LIST_REMINDERS = "list_reminders"
    LIST_NOTES = "list_notes"
    SEARCH_NOTES = "search_notes"
    CREATE_TASK = "create_task"
    CREATE_NOTE = "create_note"
    ADD_NOTE_CONTENT_BLOCK = "add_note_content_block"
    MOVE_NOTE_CATEGORY = "move_note_category"
    CREATE_REMINDER = "create_reminder"
    CANCEL_REMINDER = "cancel_reminder"
    CANCEL_AGENDA = "cancel_agenda"
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


class ModelLinkLabelProposal(BaseModel):
    """Presentation metadata for a URL that remains hidden from the model."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source_index: int = Field(ge=1, le=8)
    label: str = Field(min_length=1, max_length=40)


class ModelIntentProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: IntentAction
    confidence: float = Field(ge=0, le=1)
    query: str | None = Field(default=None, max_length=500)
    title: str | None = Field(default=None, max_length=500)
    body: str | None = Field(default=None, max_length=4000)
    answer: str | None = Field(default=None, max_length=4000)
    fire_at: datetime | None = None
    due_at: datetime | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    recurrence_rule: str | None = Field(default=None, max_length=500)
    anchor_date: date | None = None
    briefing_time: time | None = None
    important_day_kind: Literal["anniversary", "birthday"] | None = None
    calendar_system: Literal["solar", "lunar"] | None = None
    lunar_month: int | None = Field(default=None, ge=1, le=12)
    lunar_day: int | None = Field(default=None, ge=1, le=30)
    lunar_leap: bool = False
    advance_days: list[int] = Field(default_factory=list, max_length=8)
    lead_minutes: list[int] = Field(default_factory=list, max_length=12)
    notifications: list[ModelNotificationProposal] = Field(
        default_factory=list,
        max_length=8,
    )
    links: list[ModelLinkLabelProposal] = Field(default_factory=list, max_length=8)
    include_in_daily_briefing: bool = False
    private: bool = False
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
