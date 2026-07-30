"""Persistent, atomic quotas across provider/account/conversation/user scopes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from zhixu.adapters.storage.sqlite.database import Database
from zhixu.domain.agenda import require_aware
from zhixu.domain.errors import ValidationError


class QuotaWindow(StrEnum):
    SECOND = "second"
    MINUTE = "minute"
    DAY = "day"


@dataclass(frozen=True, slots=True)
class QuotaScope:
    kind: str
    ref: str

    def __post_init__(self) -> None:
        if self.kind not in {"provider", "account", "conversation", "user"}:
            raise ValidationError("unsupported quota scope")
        if not self.ref.strip():
            raise ValidationError("quota scope reference is required")


@dataclass(frozen=True, slots=True)
class QuotaRule:
    scope_kind: str
    window: QuotaWindow
    limit: int
    timezone: str = "UTC"

    def __post_init__(self) -> None:
        if self.scope_kind not in {"provider", "account", "conversation", "user"}:
            raise ValidationError("unsupported quota scope rule")
        if self.limit < 1:
            raise ValidationError("quota limit must be positive")
        ZoneInfo(self.timezone)


@dataclass(frozen=True, slots=True)
class QuotaDecision:
    allowed: bool
    next_available_at: datetime
    reason_code: str


def _window(now: datetime, rule: QuotaRule) -> tuple[datetime, datetime]:
    local = now.astimezone(ZoneInfo(rule.timezone))
    if rule.window is QuotaWindow.SECOND:
        start = local.replace(microsecond=0)
        end = start + timedelta(seconds=1)
    elif rule.window is QuotaWindow.MINUTE:
        start = local.replace(second=0, microsecond=0)
        end = start + timedelta(minutes=1)
    else:
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


class QuotaManager:
    def __init__(self, database: Database, rules: tuple[QuotaRule, ...]) -> None:
        if not rules:
            raise ValidationError("quota rules are required")
        self.database = database
        self.rules = rules

    def reserve(
        self,
        scopes: tuple[QuotaScope, ...],
        *,
        now: datetime,
    ) -> QuotaDecision:
        require_aware(now, "now")
        by_kind = {scope.kind: scope for scope in scopes}
        if len(by_kind) != len(scopes):
            raise ValidationError("quota scopes must be unique by kind")
        checks: list[tuple[QuotaRule, QuotaScope, datetime, datetime]] = []
        for rule in self.rules:
            scope = by_kind.get(rule.scope_kind)
            if scope is None:
                raise ValidationError(f"missing quota scope: {rule.scope_kind}")
            start, end = _window(now, rule)
            checks.append((rule, scope, start, end))
        with self.database.transaction() as connection:
            blocked_until: list[datetime] = []
            blocked_windows: list[str] = []
            for rule, scope, start, end in checks:
                row = connection.execute(
                    """
                    SELECT used FROM quota_usage
                    WHERE scope_kind=? AND scope_ref=?
                      AND window_kind=? AND window_start=?
                    """,
                    (
                        scope.kind,
                        scope.ref,
                        rule.window.value,
                        start.isoformat(),
                    ),
                ).fetchone()
                if row is not None and int(row["used"]) >= rule.limit:
                    blocked_until.append(end)
                    blocked_windows.append(f"{scope.kind}:{rule.window.value}")
            if blocked_until:
                return QuotaDecision(
                    False,
                    max(blocked_until),
                    "quota_exhausted:" + ",".join(sorted(blocked_windows)),
                )
            for rule, scope, start, _end in checks:
                connection.execute(
                    """
                    INSERT INTO quota_usage(
                        scope_kind,scope_ref,window_kind,window_start,used
                    ) VALUES(?,?,?,?,1)
                    ON CONFLICT(scope_kind,scope_ref,window_kind,window_start)
                    DO UPDATE SET used=used+1
                    """,
                    (scope.kind, scope.ref, rule.window.value, start.isoformat()),
                )
        return QuotaDecision(True, now, "reserved")
