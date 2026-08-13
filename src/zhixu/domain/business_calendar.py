"""Region-independent business-day arithmetic over a versioned holiday snapshot.

Each region supplies its own gazetted snapshot; the arithmetic here never knows
which region it is serving and never reaches the network.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date

from .errors import ValidationError


@dataclass(frozen=True, slots=True)
class BusinessCalendar:
    """A versioned public-holiday snapshot for one region.

    ``token`` is the stable identifier used inside a recurrence rule; ``label``
    is the English region name used in error messages.
    """

    token: str
    label: str
    supported_years: frozenset[int]
    holidays: frozenset[date]

    def is_business_day(self, value: date) -> bool:
        if value.year not in self.supported_years:
            raise ValidationError(f"{self.label} holiday calendar year is not installed")
        return value.weekday() < 5 and value not in self.holidays

    def monthly_business_day(self, year: int, month: int, position: int) -> date:
        """Return a 1-based or negative-position business day within one month."""

        if position == 0 or abs(position) > 31:
            raise ValidationError("business-day position is invalid")
        days = [
            date(year, month, day)
            for day in range(1, monthrange(year, month)[1] + 1)
            if self.is_business_day(date(year, month, day))
        ]
        try:
            return days[position - 1] if position > 0 else days[position]
        except IndexError as exc:
            raise ValidationError("business-day position is outside the month") from exc


def parse_holiday_values(values: str) -> frozenset[date]:
    return frozenset(date.fromisoformat(value) for value in values.split())
