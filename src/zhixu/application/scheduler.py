"""Clock-driven reminder and daily-briefing scheduling."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from zhixu.channels import DailyAgendaPreview, MessageButton, MessageKind, OutboundMessage
from zhixu.domain import (
    Action,
    AgendaNotificationRule,
    AuthorizedAction,
    DataClassification,
    Reminder,
    ReminderStatus,
    ResourceRef,
)
from zhixu.domain.errors import ConflictError
from zhixu.ports import (
    AgendaNotificationRepositoryPort,
    AgendaRepositoryPort,
    AnniversaryRepositoryPort,
    Clock,
    DailyBriefingRepositoryPort,
    ReminderRepositoryPort,
)


class OutboxPort(Protocol):
    def enqueue(
        self,
        *,
        delivery_id: str,
        idempotency_key: str,
        owner_user_id: str,
        message: OutboundMessage,
        now: datetime,
        priority: int = 30,
        max_attempts: int = 5,
        actor_user_id: str = "service:application",
    ) -> bool: ...


def _escape_markdown_text(value: str) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"([\\`*_{}\[\]()#+\-.!>|])", r"\\\1", compact)


def _minute(value: datetime, timezone: ZoneInfo) -> int:
    local = value.astimezone(timezone)
    return local.hour * 60 + local.minute


def _preview_bounds(
    start_at: datetime,
    end_at: datetime,
    day_start: datetime,
    day_end: datetime,
) -> tuple[int, int]:
    clipped_start = max(start_at.astimezone(day_start.tzinfo), day_start)
    clipped_end = min(end_at.astimezone(day_end.tzinfo), day_end)
    start = int((clipped_start - day_start).total_seconds() // 60)
    end = int((clipped_end - day_start).total_seconds() // 60)
    return max(0, start), min(1440, max(start + 1, end))


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


@dataclass(slots=True)
class AgendaNotificationScheduler:
    rules: AgendaNotificationRepositoryPort
    agenda: AgendaRepositoryPort
    reminders: ReminderRepositoryPort
    clock: Clock

    def tick(self) -> int:
        now = self.clock.now()
        created = 0
        for rule in self.rules.list_enabled():
            created += self._materialize_rule(rule, now)
        return created

    def _materialize_rule(
        self,
        rule: AgendaNotificationRule,
        now: datetime,
    ) -> int:
        timezone = ZoneInfo(rule.timezone)
        local_now = now.astimezone(timezone)
        if local_now.time().replace(tzinfo=None) < rule.time_of_day:
            return 0
        occurrence_date = local_now.date() - timedelta(days=rule.day_offset)
        occurrence_start = datetime.combine(occurrence_date, time.min, tzinfo=timezone)
        occurrence_end = occurrence_start + timedelta(days=1)
        occurrences = [
            occurrence
            for occurrence in self.agenda.occurrences(
                rule.owner_user_id,
                occurrence_start,
                occurrence_end,
            )
            if occurrence.agenda_item_id == rule.agenda_item_id
        ]
        materialized = 0
        for occurrence in occurrences:
            identity = (
                f"{rule.id}:{occurrence.start_at.astimezone(ZoneInfo('UTC')).isoformat()}"
            )
            reminder_id = "reminder_" + hashlib.sha256(
                identity.encode("utf-8")
            ).hexdigest()[:32]
            if self.reminders.get(reminder_id) is not None:
                continue
            fire_at = datetime.combine(
                local_now.date(),
                rule.time_of_day,
                tzinfo=timezone,
            )
            reminder = Reminder(
                id=reminder_id,
                owner_user_id=rule.owner_user_id,
                creator_user_id=rule.creator_user_id,
                title=rule.text,
                fire_at=fire_at,
                target_ref=rule.target_ref,
                classification=rule.classification,
                related_kind="agenda",
                related_id=rule.agenda_item_id,
            )
            authorization = AuthorizedAction(
                actor_user_id="service:agenda-notification",
                action=Action.CREATE,
                resource=ResourceRef(
                    "reminder",
                    reminder.id,
                    reminder.owner_user_id,
                    reminder.classification,
                ),
                authorized_at=now,
            )
            try:
                self.reminders.create(reminder, authorization)
            except ConflictError:
                continue
            materialized += 1
        return materialized


@dataclass(slots=True)
class DailyBriefingScheduler:
    briefings: DailyBriefingRepositoryPort
    anniversaries: AnniversaryRepositoryPort
    agenda: AgendaRepositoryPort
    reminders: ReminderRepositoryPort
    outbox: OutboxPort
    clock: Clock

    def tick(self) -> int:
        now = self.clock.now()
        inserted = 0
        for briefing, local_date in self.briefings.due(now):
            channel = self.briefings.target_channel(briefing.target_ref)
            if channel is None:
                continue
            timezone = ZoneInfo(briefing.timezone)
            day_start = datetime.combine(local_date, time.min, tzinfo=timezone)
            day_end = day_start + timedelta(days=1)
            occurrences = self.agenda.occurrences(
                briefing.owner_user_id,
                day_start,
                day_end,
            )
            reminders = [
                reminder
                for reminder in self.reminders.list_for_owner(briefing.owner_user_id)
                if reminder.status in {ReminderStatus.PENDING, ReminderStatus.FIRED}
                and day_start <= reminder.fire_at.astimezone(timezone) < day_end
                and reminder.classification < DataClassification.CONFIDENTIAL
            ]
            anniversaries = [
                anniversary
                for anniversary in self.anniversaries.list_for_owner(
                    briefing.owner_user_id
                )
                if anniversary.anchor_date <= local_date
                and anniversary.classification < DataClassification.CONFIDENTIAL
            ]
            safe_occurrences = [
                occurrence
                for occurrence in occurrences
                if (item := self.agenda.get(occurrence.agenda_item_id)) is not None
                and item.classification < DataClassification.CONFIDENTIAL
            ]

            lines = [f"# {local_date:%Y年%m月%d日} · 每日简报"]
            for anniversary in anniversaries:
                lines.extend(
                    [
                        "",
                        (
                            f"今天是 {_escape_markdown_text(anniversary.title)}的"
                            f"第 **{anniversary.day_number(local_date)}** 天。"
                        ),
                    ]
                )
            lines.extend(["", "## 今日日程"])
            schedule_lines: list[tuple[datetime, str]] = []
            for occurrence in safe_occurrences:
                schedule_lines.append(
                    (
                        occurrence.start_at,
                        (
                            f"- `{occurrence.start_at.astimezone(timezone):%H:%M}` "
                            f"📅 {_escape_markdown_text(occurrence.title)}"
                        ),
                    )
                )
            for reminder in reminders:
                schedule_lines.append(
                    (
                        reminder.fire_at,
                        (
                            f"- `{reminder.fire_at.astimezone(timezone):%H:%M}` "
                            f"⏰ {_escape_markdown_text(reminder.title)}"
                        ),
                    )
                )
            schedule_lines.sort(key=lambda entry: entry[0])
            lines.extend(
                [line for _when, line in schedule_lines]
                or ["> 今天没有日程或提醒。"]
            )

            preview_entries = [
                (
                    *_preview_bounds(
                        occurrence.start_at,
                        occurrence.end_at,
                        day_start,
                        day_end,
                    ),
                    "agenda",
                )
                for occurrence in safe_occurrences
            ]
            preview_entries.extend(
                (
                    _minute(reminder.fire_at, timezone),
                    min(1440, _minute(reminder.fire_at, timezone) + 15),
                    "reminder",
                )
                for reminder in reminders
                if _minute(reminder.fire_at, timezone) < 1440
            )
            key = f"daily-briefing:{briefing.id}:{local_date.isoformat()}"
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
            message = OutboundMessage(
                channel=channel[0],
                channel_account=channel[1],
                target_ref=briefing.target_ref,
                kind=MessageKind.BUTTON,
                text="\n".join(lines),
                buttons=(
                    MessageButton("今天", "/今天"),
                    MessageButton("月历", "/日历"),
                    MessageButton("待办", "/待办"),
                    MessageButton("提醒", "/提醒"),
                ),
                classification=briefing.classification,
                daily_agenda_preview=DailyAgendaPreview(
                    year=local_date.year,
                    month=local_date.month,
                    day=local_date.day,
                    entries=tuple(sorted(preview_entries)),
                    anniversary_day_numbers=tuple(
                        anniversary.day_number(local_date)
                        for anniversary in anniversaries
                    ),
                ),
            )
            created = self.outbox.enqueue(
                delivery_id=f"delivery_{digest}",
                idempotency_key=key,
                owner_user_id=briefing.owner_user_id,
                message=message,
                now=now,
                priority=20,
                actor_user_id="service:daily-briefing",
            )
            self.briefings.mark_sent(briefing.id, local_date, now)
            inserted += int(created)
        return inserted
