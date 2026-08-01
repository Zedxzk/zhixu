"""Anniversaries and user-configured daily briefing schedules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum

from .agenda import require_aware, require_timezone
from .classification import DataClassification, require_ordinary_storage
from .errors import ValidationError
from .lunar_calendar import LunarDate, from_solar, to_solar

# A date recorded without its year anchors on year 1 as a sentinel.
UNKNOWN_YEAR = 1
DEFAULT_ANNIVERSARY_ADVANCE_DAYS = (30, 15, 7)
DEFAULT_BIRTHDAY_ADVANCE_DAYS = (7, 3, 1)
_MAXIMUM_ADVANCE_ENTRIES = 8


class ImportantDayKind(StrEnum):
    ANNIVERSARY = "anniversary"
    BIRTHDAY = "birthday"


class CalendarSystem(StrEnum):
    SOLAR = "solar"
    LUNAR = "lunar"


@dataclass(frozen=True, slots=True)
class Anniversary:
    """An important day that recurs every year.

    ``anchor_date`` is the day the thing being marked happened. Its year is what
    the elapsed-year and age counts are measured from; for a solar entry its
    month and day are also what recurs. A lunar entry recurs on
    ``lunar_month``/``lunar_day`` instead, because a lunisolar date does not
    keep a fixed Gregorian month and day.
    """

    id: str
    owner_user_id: str
    creator_user_id: str
    title: str
    anchor_date: date
    timezone: str
    kind: ImportantDayKind = ImportantDayKind.ANNIVERSARY
    calendar: CalendarSystem = CalendarSystem.SOLAR
    lunar_month: int | None = None
    lunar_day: int | None = None
    lunar_leap: bool = False
    advance_days: tuple[int, ...] = ()
    classification: DataClassification = DataClassification.PERSONAL
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (self.id, self.owner_user_id, self.creator_user_id, self.title)
        ):
            raise ValidationError("anniversary fields must not be empty")
        require_timezone(self.timezone)
        require_ordinary_storage(self.classification)
        if self.created_at is not None:
            require_aware(self.created_at, "created_at")
        if self.calendar is CalendarSystem.LUNAR:
            if self.lunar_month is None or self.lunar_day is None:
                raise ValidationError("a lunisolar important day needs a lunar date")
            # Validates the month and day ranges.
            LunarDate(self.anchor_date.year, self.lunar_month, self.lunar_day)
        elif self.lunar_month is not None or self.lunar_day is not None:
            raise ValidationError("a solar important day must not carry a lunar date")
        if len(self.advance_days) > _MAXIMUM_ADVANCE_ENTRIES:
            raise ValidationError("advance notice limit exceeded")
        if any(not 1 <= value <= 366 for value in self.advance_days):
            raise ValidationError("advance notice days are out of range")
        if len(set(self.advance_days)) != len(self.advance_days):
            raise ValidationError("advance notice days must be distinct")

    def day_number(self, today: date) -> int:
        return (today - self.anchor_date).days + 1

    def counts_elapsed_days(self) -> bool:
        """A birthday marks a date, it does not accumulate a day count."""

        return self.kind is ImportantDayKind.ANNIVERSARY

    def occurrence_in(self, gregorian_year: int) -> date | None:
        """The day this recurs on within one Gregorian year, if it recurs."""

        if self.calendar is CalendarSystem.SOLAR:
            return _solar_occurrence(self.anchor_date, gregorian_year)
        assert self.lunar_month is not None and self.lunar_day is not None
        for lunar_year in (gregorian_year - 1, gregorian_year):
            when = _lunar_occurrence(
                lunar_year,
                self.lunar_month,
                self.lunar_day,
                self.lunar_leap,
            )
            if when is not None and when.year == gregorian_year:
                return when
        return None

    def next_occurrence(self, today: date) -> date | None:
        for gregorian_year in (today.year, today.year + 1):
            when = self.occurrence_in(gregorian_year)
            if when is not None and when >= today:
                return when
        return None

    def has_known_year(self) -> bool:
        """A birthday may be recorded without the year the person was born."""

        return self.anchor_date.year > UNKNOWN_YEAR

    def elapsed_years(self, occurrence: date) -> int:
        if not self.has_known_year():
            return 0
        return occurrence.year - self.anchor_date.year

    def advance_notice_for(self, today: date) -> tuple[date, int] | None:
        """The upcoming occurrence and its distance, when today is a notice day."""

        when = self.next_occurrence(today)
        if when is None:
            return None
        remaining = (when - today).days
        if remaining in self.advance_days:
            return when, remaining
        return None


def _solar_occurrence(anchor: date, gregorian_year: int) -> date:
    try:
        return anchor.replace(year=gregorian_year)
    except ValueError:
        # 29 February only exists in a leap year; mark it on the 28th otherwise.
        return date(gregorian_year, 2, 28)


def _lunar_occurrence(
    lunar_year: int,
    month: int,
    day: int,
    leap: bool,
) -> date | None:
    for use_leap in (leap, False) if leap else (False,):
        try:
            return to_solar(LunarDate(lunar_year, month, day, use_leap), clamp=True)
        except ValidationError:
            continue
    return None


def lunar_date_of(value: date) -> LunarDate | None:
    try:
        return from_solar(value)
    except ValidationError:
        return None


@dataclass(frozen=True, slots=True)
class DailyBriefing:
    id: str
    owner_user_id: str
    creator_user_id: str
    target_ref: str
    time_of_day: time
    timezone: str
    classification: DataClassification = DataClassification.PERSONAL
    enabled: bool = True
    last_sent_on: date | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.id,
                self.owner_user_id,
                self.creator_user_id,
                self.target_ref,
            )
        ):
            raise ValidationError("daily briefing fields must not be empty")
        if self.time_of_day.tzinfo is not None:
            raise ValidationError("daily briefing wall time must not include timezone")
        require_timezone(self.timezone)
        require_ordinary_storage(self.classification)
        if self.created_at is not None:
            require_aware(self.created_at, "created_at")
        if self.updated_at is not None:
            require_aware(self.updated_at, "updated_at")
