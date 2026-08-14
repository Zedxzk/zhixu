"""Typed deterministic commands and a closed command bus."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, TypeVar

from zhixu.domain import (
    ActionLink,
    CalendarSystem,
    DataClassification,
    ExceptionAction,
    ImportantDayKind,
    MissedReminderPolicy,
    NoteAttachment,
    NoteContentBlock,
    NoteField,
    TaskStatus,
)
from zhixu.domain.errors import ConflictError, ValidationError
from zhixu.domain.policy import CommandContext


@dataclass(frozen=True, slots=True)
class CreateAgenda:
    title: str
    start_at: datetime
    end_at: datetime
    timezone: str
    description: str = ""
    action_links: tuple[ActionLink, ...] = ()
    all_day: bool = False
    recurrence_rule: str | None = None
    classification: DataClassification = DataClassification.PERSONAL
    private: bool = False


@dataclass(frozen=True, slots=True)
class CreateAgendaNotification:
    agenda_item_id: str
    time_of_day: time
    day_offset: int
    text: str
    timezone: str
    target_ref: str
    action_links: tuple[ActionLink, ...] = ()
    classification: DataClassification = DataClassification.PERSONAL


@dataclass(frozen=True, slots=True)
class CreateAnniversary:
    title: str
    anchor_date: date
    timezone: str
    kind: ImportantDayKind = ImportantDayKind.ANNIVERSARY
    calendar: CalendarSystem = CalendarSystem.SOLAR
    lunar_month: int | None = None
    lunar_day: int | None = None
    lunar_leap: bool = False
    advance_days: tuple[int, ...] | None = None
    classification: DataClassification = DataClassification.PERSONAL
    private: bool = False
    allow_duplicate: bool = False


@dataclass(frozen=True, slots=True)
class UpdateAnniversary:
    """An unset field keeps its stored value, so one field can be edited alone.

    ``calendar`` carries its own date fields, so changing it always arrives
    together with the date it switches to.
    """

    anniversary_id: str
    title: str | None = None
    anchor_date: date | None = None
    kind: ImportantDayKind | None = None
    calendar: CalendarSystem | None = None
    lunar_month: int | None = None
    lunar_day: int | None = None
    lunar_leap: bool | None = None
    advance_days: tuple[int, ...] | None = None
    timezone: str | None = None
    classification: DataClassification | None = None


@dataclass(frozen=True, slots=True)
class DeleteAnniversary:
    anniversary_id: str


@dataclass(frozen=True, slots=True)
class UpdateDailyBriefing:
    briefing_id: str
    time_of_day: time | None = None
    target_ref: str | None = None
    timezone: str | None = None
    enabled: bool | None = None
    classification: DataClassification | None = None


@dataclass(frozen=True, slots=True)
class DeleteDailyBriefing:
    briefing_id: str


@dataclass(frozen=True, slots=True)
class UpdateAgendaNotification:
    rule_id: str
    text: str | None = None
    time_of_day: time | None = None
    day_offset: int | None = None
    target_ref: str | None = None
    timezone: str | None = None
    enabled: bool | None = None
    classification: DataClassification | None = None


@dataclass(frozen=True, slots=True)
class DeleteAgendaNotification:
    rule_id: str


@dataclass(frozen=True, slots=True)
class SetNotificationLeads:
    lead_minutes: tuple[int, ...]
    agenda_item_id: str | None = None


@dataclass(frozen=True, slots=True)
class CreateDailyBriefing:
    time_of_day: time
    timezone: str
    target_ref: str
    classification: DataClassification = DataClassification.PERSONAL
    private: bool = False


@dataclass(frozen=True, slots=True)
class UpdateAgenda:
    item_id: str
    expected_version: int
    title: str
    start_at: datetime
    end_at: datetime
    timezone: str
    description: str = ""
    action_links: tuple[ActionLink, ...] = ()
    all_day: bool = False
    recurrence_rule: str | None = None
    classification: DataClassification = DataClassification.PERSONAL


@dataclass(frozen=True, slots=True)
class DeleteAgenda:
    item_id: str


@dataclass(frozen=True, slots=True)
class SetAgendaException:
    item_id: str
    occurrence_at: datetime
    action: ExceptionAction
    replacement_start: datetime | None = None
    replacement_end: datetime | None = None


@dataclass(frozen=True, slots=True)
class CreateTask:
    title: str
    description: str = ""
    priority: int = 0
    due_at: datetime | None = None
    classification: DataClassification = DataClassification.PERSONAL
    private: bool = False


@dataclass(frozen=True, slots=True)
class UpdateTask:
    task_id: str
    expected_version: int
    title: str
    description: str = ""
    priority: int = 0
    due_at: datetime | None = None
    classification: DataClassification = DataClassification.PERSONAL


@dataclass(frozen=True, slots=True)
class DeleteTask:
    task_id: str


@dataclass(frozen=True, slots=True)
class TransitionTask:
    task_id: str
    expected_version: int
    status: TaskStatus


@dataclass(frozen=True, slots=True)
class PostponeTask:
    task_id: str
    expected_version: int
    due_at: datetime


@dataclass(frozen=True, slots=True)
class CreateNote:
    title: str
    body: str
    category_path: tuple[str, ...] = ("未分类",)
    content_blocks: tuple[NoteContentBlock, ...] = ()
    tags: tuple[str, ...] = ()
    attachments: tuple[NoteAttachment, ...] = ()
    classification: DataClassification = DataClassification.PERSONAL
    private: bool = False


@dataclass(frozen=True, slots=True)
class UpdateNote:
    note_id: str
    expected_version: int
    title: str
    body: str
    tags: tuple[str, ...] = ()
    attachments: tuple[NoteAttachment, ...] = ()
    classification: DataClassification = DataClassification.PERSONAL


@dataclass(frozen=True, slots=True)
class DeleteNote:
    note_id: str


@dataclass(frozen=True, slots=True)
class AddNoteContentBlock:
    note_id: str
    name: str
    body: str = ""
    fields: tuple[NoteField, ...] = ()


@dataclass(frozen=True, slots=True)
class MoveNoteCategory:
    note_id: str
    category_path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CreateReminder:
    title: str
    fire_at: datetime
    target_ref: str
    action_links: tuple[ActionLink, ...] = ()
    missed_policy: MissedReminderPolicy = MissedReminderPolicy.FIRE
    classification: DataClassification = DataClassification.PERSONAL
    related_kind: str | None = None
    related_id: str | None = None
    # When the thing being announced actually happens, so an advance
    # notice can say so instead of only naming its own fire time.
    related_start_at: datetime | None = None
    private: bool = False


@dataclass(frozen=True, slots=True)
class CancelReminder:
    reminder_id: str


@dataclass(frozen=True, slots=True)
class AcknowledgeReminder:
    reminder_id: str


@dataclass(frozen=True, slots=True)
class SnoozeReminder:
    reminder_id: str
    fire_at: datetime


Command = (
    CreateAgenda
    | CreateAgendaNotification
    | CreateAnniversary
    | CreateDailyBriefing
    | SetNotificationLeads
    | UpdateAnniversary
    | DeleteAnniversary
    | UpdateDailyBriefing
    | DeleteDailyBriefing
    | UpdateAgendaNotification
    | DeleteAgendaNotification
    | UpdateAgenda
    | DeleteAgenda
    | SetAgendaException
    | CreateTask
    | UpdateTask
    | DeleteTask
    | TransitionTask
    | PostponeTask
    | CreateNote
    | AddNoteContentBlock
    | MoveNoteCategory
    | UpdateNote
    | DeleteNote
    | CreateReminder
    | CancelReminder
    | AcknowledgeReminder
    | SnoozeReminder
)
CommandT = TypeVar("CommandT", bound=Command)
Handler = Callable[[Any, CommandContext], Any]


class CommandBus:
    """No dynamic imports or model-generated handler names are accepted."""

    def __init__(self) -> None:
        self._handlers: dict[type[Any], Handler] = {}

    def register(self, command_type: type[CommandT], handler: Handler) -> None:
        if command_type in self._handlers:
            raise ConflictError(f"handler already registered for {command_type.__name__}")
        self._handlers[command_type] = handler

    def execute(self, command: CommandT, context: CommandContext) -> Any:
        handler = self._handlers.get(type(command))
        if handler is None:
            raise ValidationError(f"unregistered command: {type(command).__name__}")
        return handler(command, context)
