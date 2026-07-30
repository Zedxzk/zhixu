"""Supplier-neutral LLM and usage-budget ports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class LLMRequest:
    model: str
    system_prompt: str = field(repr=False)
    user_prompt: str = field(repr=False)
    response_schema: dict[str, Any] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: str = field(repr=False)
    input_units: int = 0
    output_units: int = 0


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
        estimated_input_units: int,
        limits: tuple[LLMBudgetLimit, ...],
    ) -> bool: ...

    def record_output(
        self,
        *,
        owner_user_id: str,
        model_ref: str,
        output_units: int,
    ) -> None: ...
