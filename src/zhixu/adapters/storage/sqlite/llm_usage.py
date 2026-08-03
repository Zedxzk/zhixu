"""Persistent day/month LLM budgets."""

from __future__ import annotations

from datetime import UTC, datetime

from zhixu.ports import LLMBudgetLimit, LLMCallReason

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
        reason: LLMCallReason,
        estimated_input_units: int,
        limits: tuple[LLMBudgetLimit, ...],
    ) -> int | None:
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
                    return None
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
            cursor = connection.execute(
                """
                INSERT INTO llm_call_events(
                    occurred_at,owner_user_id,model_ref,reason,outcome,
                    estimated_input_units,output_units
                ) VALUES(?,?,?,?,?, ?,0)
                """,
                (
                    now.astimezone(UTC).isoformat(),
                    owner_user_id,
                    model_ref,
                    reason.value,
                    "reserved",
                    estimated_input_units,
                ),
            )
            call_id = int(cursor.lastrowid)
        return call_id

    def record_result(
        self,
        *,
        call_id: int,
        owner_user_id: str,
        model_ref: str,
        outcome: str,
        input_units: int,
        output_units: int,
        cached_input_units: int,
    ) -> None:
        if outcome not in {"completed", "failed"}:
            raise ValueError("LLM call outcome is invalid")
        now = self.clock.now()
        with self.database.transaction() as connection:
            recorded_output = max(0, output_units) if outcome == "completed" else 0
            recorded_input = max(0, input_units) if outcome == "completed" else 0
            recorded_cached_input = (
                min(recorded_input, max(0, cached_input_units))
                if outcome == "completed"
                else 0
            )
            if outcome == "completed":
                for kind in ("day", "month"):
                    connection.execute(
                        """
                        UPDATE llm_usage SET output_units=output_units+?
                        WHERE owner_user_id=? AND model_ref=?
                          AND window_kind=? AND window_start=?
                        """,
                        (
                            recorded_output,
                            owner_user_id,
                            model_ref,
                            kind,
                            _window_start(now, kind),
                        ),
                    )
            changed = connection.execute(
                """
                UPDATE llm_call_events
                SET outcome=?,input_units=?,output_units=?,cached_input_units=?
                WHERE id=? AND owner_user_id=? AND model_ref=? AND outcome='reserved'
                """,
                (
                    outcome,
                    recorded_input,
                    recorded_output,
                    recorded_cached_input,
                    call_id,
                    owner_user_id,
                    model_ref,
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError("LLM call event is missing or already finalized")
