"""Deterministic intents and strictly validated model proposals."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IntentAction(StrEnum):
    LIST_AGENDA = "list_agenda"
    LIST_TASKS = "list_tasks"
    SEARCH_NOTES = "search_notes"
    CREATE_TASK = "create_task"
    CREATE_NOTE = "create_note"
    CREATE_REMINDER = "create_reminder"
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
    task_id: str | None = Field(default=None, max_length=160)
    resource_id: str | None = Field(default=None, max_length=160)

    @field_validator("fire_at", "due_at")
    @classmethod
    def require_aware_datetime(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("model-proposed datetimes must include a timezone")
        return value


@dataclass(frozen=True, slots=True)
class AssistantReply:
    text: str
    code: str
    source: str
