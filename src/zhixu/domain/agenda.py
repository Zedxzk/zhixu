"""Calendar items and deterministic recurrence expansion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

from .classification import DataClassification, require_ordinary_storage
from .errors import ValidationError


def require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")


def require_timezone(value: str) -> None:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValidationError(f"unknown timezone: {value}") from exc


@dataclass(frozen=True, slots=True)
class RecurrenceRule:
    value: str
    timezone: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValidationError("recurrence rule is required")
        require_timezone(self.timezone)
        try:
            rrulestr(self.value, dtstart=datetime.now(ZoneInfo(self.timezone)))
        except (TypeError, ValueError) as exc:
            raise ValidationError("invalid recurrence rule") from exc


class ExceptionAction(StrEnum):
    CANCEL = "cancel"
    REPLACE = "replace"


@dataclass(frozen=True, slots=True)
class RecurrenceException:
    occurrence_at: datetime
    action: ExceptionAction
    replacement_start: datetime | None = None
    replacement_end: datetime | None = None

    def __post_init__(self) -> None:
        require_aware(self.occurrence_at, "occurrence_at")
        if self.action is ExceptionAction.REPLACE:
            if self.replacement_start is None or self.replacement_end is None:
                raise ValidationError("replacement requires start and end")
            require_aware(self.replacement_start, "replacement_start")
            require_aware(self.replacement_end, "replacement_end")
            if self.replacement_end <= self.replacement_start:
                raise ValidationError("replacement end must be after start")


@dataclass(frozen=True, slots=True)
class AgendaItem:
    id: str
    owner_user_id: str
    title: str
    start_at: datetime
    end_at: datetime
    timezone: str
    all_day: bool = False
    description: str = ""
    classification: DataClassification = DataClassification.PERSONAL
    recurrence: RecurrenceRule | None = None
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.owner_user_id.strip() or not self.title.strip():
            raise ValidationError("agenda id, owner, and title are required")
        require_aware(self.start_at, "start_at")
        require_aware(self.end_at, "end_at")
        require_timezone(self.timezone)
        if self.end_at <= self.start_at:
            raise ValidationError("agenda end must be after start")
        if self.version < 1:
            raise ValidationError("version must be positive")
        if self.created_at is not None:
            require_aware(self.created_at, "created_at")
        if self.updated_at is not None:
            require_aware(self.updated_at, "updated_at")
        require_ordinary_storage(self.classification)
        if self.recurrence is not None and self.recurrence.timezone != self.timezone:
            raise ValidationError("recurrence timezone must match agenda timezone")


@dataclass(frozen=True, slots=True)
class AgendaOccurrence:
    agenda_item_id: str
    start_at: datetime
    end_at: datetime
    replaced: bool = False


def occurrences_between(
    item: AgendaItem,
    window_start: datetime,
    window_end: datetime,
    exceptions: tuple[RecurrenceException, ...] = (),
) -> list[AgendaOccurrence]:
    """Expand one item without changing wall time across DST boundaries."""

    require_aware(window_start, "window_start")
    require_aware(window_end, "window_end")
    if window_end <= window_start:
        raise ValidationError("window end must be after start")

    duration = item.end_at - item.start_at
    if item.recurrence is None:
        if item.end_at <= window_start or item.start_at >= window_end:
            return []
        starts = [item.start_at]
    else:
        rule = rrulestr(item.recurrence.value, dtstart=item.start_at)
        starts = list(rule.between(window_start - duration, window_end, inc=True))

    exception_map = {exception.occurrence_at: exception for exception in exceptions}
    result: list[AgendaOccurrence] = []
    for start in starts:
        end = start + duration
        if end <= window_start or start >= window_end:
            continue
        exception = exception_map.get(start)
        if exception is not None and exception.action is ExceptionAction.CANCEL:
            continue
        if exception is not None and exception.action is ExceptionAction.REPLACE:
            assert exception.replacement_start is not None
            assert exception.replacement_end is not None
            if (
                exception.replacement_end > window_start
                and exception.replacement_start < window_end
            ):
                result.append(
                    AgendaOccurrence(
                        item.id,
                        exception.replacement_start,
                        exception.replacement_end,
                        replaced=True,
                    )
                )
            continue
        result.append(AgendaOccurrence(item.id, start, end))
    return result
