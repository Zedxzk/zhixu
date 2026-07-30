"""Typed deterministic commands and a closed command bus."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

from zhixu.domain import (
    DataClassification,
    ExceptionAction,
    MissedReminderPolicy,
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
    all_day: bool = False
    recurrence_rule: str | None = None
    classification: DataClassification = DataClassification.PERSONAL


@dataclass(frozen=True, slots=True)
class UpdateAgenda:
    item_id: str
    expected_version: int
    title: str
    start_at: datetime
    end_at: datetime
    timezone: str
    description: str = ""
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
    tags: tuple[str, ...] = ()
    classification: DataClassification = DataClassification.PERSONAL


@dataclass(frozen=True, slots=True)
class UpdateNote:
    note_id: str
    expected_version: int
    title: str
    body: str
    tags: tuple[str, ...] = ()
    classification: DataClassification = DataClassification.PERSONAL


@dataclass(frozen=True, slots=True)
class DeleteNote:
    note_id: str


@dataclass(frozen=True, slots=True)
class CreateReminder:
    title: str
    fire_at: datetime
    target_ref: str
    missed_policy: MissedReminderPolicy = MissedReminderPolicy.FIRE
    classification: DataClassification = DataClassification.PERSONAL
    related_kind: str | None = None
    related_id: str | None = None


@dataclass(frozen=True, slots=True)
class AcknowledgeReminder:
    reminder_id: str


@dataclass(frozen=True, slots=True)
class SnoozeReminder:
    reminder_id: str
    fire_at: datetime


Command = (
    CreateAgenda
    | UpdateAgenda
    | DeleteAgenda
    | SetAgendaException
    | CreateTask
    | UpdateTask
    | DeleteTask
    | TransitionTask
    | PostponeTask
    | CreateNote
    | UpdateNote
    | DeleteNote
    | CreateReminder
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
