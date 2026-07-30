"""Deterministic policy for sending user data to model providers."""

from __future__ import annotations

from dataclasses import dataclass

from zhixu.domain import DataClassification
from zhixu.domain.errors import PermissionDenied


@dataclass(frozen=True, slots=True)
class LLMEgressPolicy:
    allow_personal_to_external: bool = False
    allow_confidential_to_local: bool = False

    def require(
        self,
        classification: DataClassification,
        *,
        provider_is_local: bool,
    ) -> None:
        if classification >= DataClassification.SECRET:
            raise PermissionDenied("high-sensitivity data cannot enter an LLM prompt")
        if classification is DataClassification.CONFIDENTIAL:
            if not provider_is_local or not self.allow_confidential_to_local:
                raise PermissionDenied("confidential LLM egress is disabled")
            return
        if (
            classification is DataClassification.PERSONAL
            and not provider_is_local
            and not self.allow_personal_to_external
        ):
            raise PermissionDenied("personal data egress requires explicit configuration")
