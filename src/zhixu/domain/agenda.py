"""Calendar items and deterministic recurrence expansion."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

from .action_link import ActionLink
from .business_calendar import BusinessCalendar
from .classification import DataClassification, require_ordinary_storage
from .errors import ValidationError
from .hong_kong_calendar import HONG_KONG
from .macau_calendar import MACAU

#: Every regional business calendar the deterministic engine can resolve.
#: Adding a region here is the only change a new payday calendar needs; the
#: model prompt is generated from this mapping rather than restating it.
BUSINESS_CALENDARS = {
    calendar.token: calendar for calendar in (HONG_KONG, MACAU)
}
_BUSINESS_RULE = re.compile(
    r"^X-BUSINESS-DAY;CALENDAR=(?P<calendar>[A-Z_]+);BYSETPOS=(?P<position>-?\d{1,2})$"
)


@dataclass(frozen=True, slots=True)
class BusinessDayRule:
    """A payday-style rule resolved by the deterministic calendar engine."""

    calendar: BusinessCalendar
    position: int


def parse_business_day_rule(recurrence_rule: str) -> BusinessDayRule | None:
    """Business-day rule carried by a recurrence value, or None for other rules.

    A positive position counts business days from the start of the month and a
    negative one from its end, so the last business day is -1 and the
    second-to-last is -2. An unknown calendar token is not a business-day rule.
    """

    match = _BUSINESS_RULE.match(recurrence_rule)
    if match is None:
        return None
    calendar = BUSINESS_CALENDARS.get(match.group("calendar"))
    if calendar is None:
        return None
    return BusinessDayRule(calendar, int(match.group("position")))


# Minutes before an occurrence starts. Zero is the start itself.
DEFAULT_NOTIFICATION_LEAD_MINUTES = (1440, 360, 60, 30, 1, 0)
_MAXIMUM_LEAD_ENTRIES = 12
_MAXIMUM_LEAD_MINUTES = 366 * 24 * 60


def normalise_lead_minutes(values: Sequence[int]) -> tuple[int, ...]:
    """Order lead times furthest-first and reject nonsense."""

    unique = sorted({int(value) for value in values}, reverse=True)
    if len(unique) > _MAXIMUM_LEAD_ENTRIES:
        raise ValidationError("notification lead time limit exceeded")
    if any(not 0 <= value <= _MAXIMUM_LEAD_MINUTES for value in unique):
        raise ValidationError("notification lead time is out of range")
    return tuple(unique)



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
        if parse_business_day_rule(self.value) is not None:
            return
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
    creator_user_id: str | None = None
    all_day: bool = False
    description: str = ""
    action_links: tuple[ActionLink, ...] = field(default_factory=tuple, repr=False)
    classification: DataClassification = DataClassification.PERSONAL
    recurrence: RecurrenceRule | None = None
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.owner_user_id.strip() or not self.title.strip():
            raise ValidationError("agenda id, owner, and title are required")
        if self.creator_user_id is not None and not self.creator_user_id.strip():
            raise ValidationError("agenda creator must not be empty")
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
        if len(self.action_links) > 8:
            raise ValidationError("agenda action link limit exceeded")
        if self.recurrence is not None and self.recurrence.timezone != self.timezone:
            raise ValidationError("recurrence timezone must match agenda timezone")


@dataclass(frozen=True, slots=True)
class AgendaNotificationRule:
    id: str
    agenda_item_id: str
    owner_user_id: str
    creator_user_id: str
    target_ref: str
    time_of_day: time
    day_offset: int
    text: str
    timezone: str
    action_links: tuple[ActionLink, ...] = field(default_factory=tuple, repr=False)
    classification: DataClassification = DataClassification.PERSONAL
    enabled: bool = True
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        required = (
            self.id,
            self.agenda_item_id,
            self.owner_user_id,
            self.creator_user_id,
            self.target_ref,
            self.text,
        )
        if any(not value.strip() for value in required):
            raise ValidationError("agenda notification fields must not be empty")
        if self.time_of_day.tzinfo is not None:
            raise ValidationError("agenda notification wall time must be timezone-free")
        if not -366 <= self.day_offset <= 366:
            raise ValidationError("agenda notification day offset is out of range")
        require_timezone(self.timezone)
        require_ordinary_storage(self.classification)
        if len(self.action_links) > 8:
            raise ValidationError("agenda notification action link limit exceeded")
        if self.created_at is not None:
            require_aware(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class AgendaOccurrence:
    agenda_item_id: str
    start_at: datetime
    end_at: datetime
    title: str = ""
    replaced: bool = False
    creator_user_id: str | None = None


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
        business_rule = parse_business_day_rule(item.recurrence.value)
        if business_rule:
            local_start = item.start_at.astimezone(ZoneInfo(item.timezone))
            local_window_start = (window_start - duration).astimezone(
                ZoneInfo(item.timezone)
            )
            local_window_end = window_end.astimezone(ZoneInfo(item.timezone))
            starts = []
            year, month = local_window_start.year, local_window_start.month
            while (year, month) <= (local_window_end.year, local_window_end.month):
                occurrence_date = business_rule.calendar.monthly_business_day(
                    year,
                    month,
                    business_rule.position,
                )
                occurrence = datetime.combine(
                    occurrence_date,
                    local_start.timetz(),
                )
                if occurrence >= item.start_at:
                    starts.append(occurrence)
                if month == 12:
                    year, month = year + 1, 1
                else:
                    month += 1
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
                        item.title,
                        replaced=True,
                        creator_user_id=item.creator_user_id,
                    )
                )
            continue
        result.append(
            AgendaOccurrence(
                item.id,
                start,
                end,
                item.title,
                creator_user_id=item.creator_user_id,
            )
        )
    return result
