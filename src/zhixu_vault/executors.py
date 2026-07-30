"""Trusted machine-secret executors that never return the credential."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .service import ExecutionResult
from .types import SecretValue

PATOperation = Callable[[str, dict[str, Any]], ExecutionResult]


@dataclass(slots=True)
class PATIntegrationExecutor:
    """Dispatches an allowlisted PAT operation inside the isolated executor boundary."""

    operations: dict[str, PATOperation]

    def execute(
        self,
        secret: SecretValue,
        request: dict[str, Any],
    ) -> ExecutionResult:
        operation_name = str(request.get("operation") or "")
        operation = self.operations.get(operation_name)
        if operation is None:
            return ExecutionResult(False, "operation_denied", {})
        safe_request = {
            key: value for key, value in request.items() if key != "credential"
        }
        return operation(secret.text(), safe_request)
