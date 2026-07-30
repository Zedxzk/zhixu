"""Persistent day/month LLM budgets."""

from __future__ import annotations

from datetime import UTC, datetime

from zhixu.ports import LLMBudgetLimit

from .database import Database


def _window_start(now: datetime, kind: str) -> str:
    current = now.astimezone(UTC)
    if kind == "day":
        return current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    if kind == "month":
        return current.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    raise ValueError("LLM budget window must be day or month")


class SQLiteLLMUsage:
    def __init__(self, database: Database, clock) -> None:
        self.database = database
        self.clock = clock

    def reserve(
        self,
        *,
        owner_user_id: str,
        model_ref: str,
        estimated_input_units: int,
        limits: tuple[LLMBudgetLimit, ...],
    ) -> bool:
        now = self.clock.now()
        with self.database.transaction() as connection:
            rows: list[tuple[LLMBudgetLimit, str, object | None]] = []
            for limit in limits:
                start = _window_start(now, limit.window_kind)
                row = connection.execute(
                    """
                    SELECT calls,input_units,output_units FROM llm_usage
                    WHERE owner_user_id=? AND model_ref=?
                      AND window_kind=? AND window_start=?
                    """,
                    (owner_user_id, model_ref, limit.window_kind, start),
                ).fetchone()
                calls = int(row["calls"]) if row is not None else 0
                inputs = int(row["input_units"]) if row is not None else 0
                outputs = int(row["output_units"]) if row is not None else 0
                if (
                    calls + 1 > limit.calls
                    or inputs + estimated_input_units > limit.input_units
                    or outputs >= limit.output_units
                ):
                    return False
                rows.append((limit, start, row))
            for limit, start, _row in rows:
                connection.execute(
                    """
                    INSERT INTO llm_usage(
                        owner_user_id,model_ref,window_kind,window_start,
                        calls,input_units,output_units
                    ) VALUES(?,?,?,?,1,?,0)
                    ON CONFLICT(owner_user_id,model_ref,window_kind,window_start)
                    DO UPDATE SET
                        calls=calls+1,
                        input_units=input_units+excluded.input_units
                    """,
                    (
                        owner_user_id,
                        model_ref,
                        limit.window_kind,
                        start,
                        estimated_input_units,
                    ),
                )
        return True

    def record_output(
        self,
        *,
        owner_user_id: str,
        model_ref: str,
        output_units: int,
    ) -> None:
        now = self.clock.now()
        with self.database.transaction() as connection:
            for kind in ("day", "month"):
                connection.execute(
                    """
                    UPDATE llm_usage SET output_units=output_units+?
                    WHERE owner_user_id=? AND model_ref=?
                      AND window_kind=? AND window_start=?
                    """,
                    (
                        max(0, output_units),
                        owner_user_id,
                        model_ref,
                        kind,
                        _window_start(now, kind),
                    ),
                )
