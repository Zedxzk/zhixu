"""Budgeted, fail-closed access to an optional language model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from zhixu.domain import DataClassification
from zhixu.domain.errors import LLMBudgetExceeded, LLMUnavailable
from zhixu.ports import (
    Clock,
    LLMBudgetLimit,
    LLMPort,
    LLMRequest,
    LLMResponse,
    LLMUsagePort,
)
from zhixu.security import LLMEgressPolicy


@dataclass(slots=True)
class _CircuitState:
    failures: int = 0
    open_until: datetime | None = None


class LLMGateway:
    def __init__(
        self,
        *,
        client: LLMPort,
        usage: LLMUsagePort,
        clock: Clock,
        egress: LLMEgressPolicy,
        limits: tuple[LLMBudgetLimit, ...],
        timeout_seconds: float = 15,
        failure_threshold: int = 3,
        recovery_seconds: int = 60,
    ) -> None:
        if timeout_seconds <= 0 or failure_threshold < 1 or recovery_seconds < 1:
            raise ValueError("LLM timeout and circuit breaker limits must be positive")
        if not limits:
            raise ValueError("at least one LLM budget limit is required")
        self.client = client
        self.usage = usage
        self.clock = clock
        self.egress = egress
        self.limits = limits
        self.timeout_seconds = timeout_seconds
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._circuit = _CircuitState()

    def generate(
        self,
        *,
        owner_user_id: str,
        request: LLMRequest,
        classification: DataClassification,
    ) -> LLMResponse:
        self.egress.require(classification, provider_is_local=self.client.is_local)
        now = self.clock.now()
        if self._circuit.open_until is not None and now < self._circuit.open_until:
            raise LLMUnavailable("LLM circuit breaker is open")
        model_ref = f"{self.client.provider_ref}:{request.model}"
        estimated_input = max(
            1,
            (len(request.system_prompt) + len(request.user_prompt) + 3) // 4,
        )
        if not self.usage.reserve(
            owner_user_id=owner_user_id,
            model_ref=model_ref,
            estimated_input_units=estimated_input,
            limits=self.limits,
        ):
            raise LLMBudgetExceeded("LLM usage budget has been exhausted")
        try:
            response = self.client.generate(
                request,
                timeout_seconds=self.timeout_seconds,
            )
        except Exception as exc:
            self._circuit.failures += 1
            if self._circuit.failures >= self.failure_threshold:
                self._circuit.open_until = now + timedelta(seconds=self.recovery_seconds)
            raise LLMUnavailable("LLM request failed") from exc
        self._circuit.failures = 0
        self._circuit.open_until = None
        self.usage.record_output(
            owner_user_id=owner_user_id,
            model_ref=model_ref,
            output_units=response.output_units,
        )
        return response
