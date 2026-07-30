"""Injectable time source for deterministic scheduling and policy checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from zhixu.domain.agenda import require_aware


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(slots=True)
class FrozenClock:
    current: datetime

    def __post_init__(self) -> None:
        require_aware(self.current, "current")

    def now(self) -> datetime:
        return self.current

    def set(self, value: datetime) -> None:
        require_aware(value, "value")
        self.current = value
