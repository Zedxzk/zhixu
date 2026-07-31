"""Supplier-neutral LLM and usage-budget ports."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class LLMRequest:
    model: str
    system_prompt: str = field(repr=False)
    user_prompt: str = field(repr=False)
    response_schema: dict[str, Any] | None = field(default=None, repr=False)
    web_search: bool = False


@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: str = field(repr=False)
    input_units: int = 0
    output_units: int = 0


class LLMCallReason(StrEnum):
    SCHEDULE_PARSE = "schedule_parse"
    DETERMINISTIC_PARSER_MISS = "deterministic_parser_miss"
    NOTE_SUMMARY_REQUESTED = "note_summary_requested"
    GENERAL_QUESTION = "general_question"


class LLMPort(Protocol):
    provider_ref: str
    is_local: bool

    def generate(self, request: LLMRequest, *, timeout_seconds: float) -> LLMResponse: ...


@dataclass(frozen=True, slots=True)
class LLMBudgetLimit:
    window_kind: str
    calls: int
    input_units: int
    output_units: int

    def __post_init__(self) -> None:
        if self.window_kind not in {"day", "month"}:
            raise ValueError("LLM budget window must be day or month")
        if min(self.calls, self.input_units, self.output_units) < 1:
            raise ValueError("LLM budget limits must be positive")


class LLMUsagePort(Protocol):
    def reserve(
        self,
        *,
        owner_user_id: str,
        model_ref: str,
        reason: LLMCallReason,
        estimated_input_units: int,
        limits: tuple[LLMBudgetLimit, ...],
    ) -> int | None: ...

    def record_result(
        self,
        *,
        call_id: int,
        owner_user_id: str,
        model_ref: str,
        outcome: str,
        output_units: int,
    ) -> None: ...
