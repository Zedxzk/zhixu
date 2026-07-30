"""Domain errors with stable, non-sensitive public codes."""

from __future__ import annotations


class ZhixuError(Exception):
    """Base error safe to map to a public error response."""

    code = "zhixu_error"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)


class ValidationError(ZhixuError):
    code = "validation_error"


class NotFoundError(ZhixuError):
    code = "not_found"


class ConflictError(ZhixuError):
    code = "conflict"


class ConcurrencyConflict(ConflictError):
    code = "concurrency_conflict"


class PermissionDenied(ZhixuError):
    code = "permission_denied"


class ConfirmationRequired(PermissionDenied):
    code = "confirmation_required"


class ClassificationNotSupported(ZhixuError):
    code = "classification_not_supported"


class InvalidTransition(ConflictError):
    code = "invalid_transition"


class LLMUnavailable(ZhixuError):
    code = "llm_unavailable"


class LLMBudgetExceeded(LLMUnavailable):
    code = "llm_budget_exceeded"


class InvalidModelOutput(LLMUnavailable):
    code = "invalid_model_output"
