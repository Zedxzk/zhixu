"""Clock-driven reminder scheduling with transactional outbox handoff."""

from __future__ import annotations

from dataclasses import dataclass

from zhixu.ports import Clock, ReminderRepositoryPort


@dataclass(slots=True)
class ReminderScheduler:
    reminders: ReminderRepositoryPort
    clock: Clock
    late_grace_seconds: int = 300

    def tick(self) -> int:
        return self.reminders.enqueue_due(
            self.clock.now(),
            late_grace_seconds=self.late_grace_seconds,
        )
