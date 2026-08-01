"""Deterministic reminder scheduler process."""

from __future__ import annotations

import argparse
import logging
import signal
import threading
from collections.abc import Sequence
from pathlib import Path

from zhixu.adapters.storage.sqlite import (
    AgendaNotificationRepository,
    AgendaRepository,
    AnniversaryRepository,
    DailyBriefingRepository,
    Database,
    ReminderRepository,
)
from zhixu.application import (
    AgendaNotificationScheduler,
    DailyBriefingScheduler,
    ReminderScheduler,
)
from zhixu.delivery import OutboxStore
from zhixu.ports import SystemClock

from .common import configure_logging

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zhixu-worker")
    parser.add_argument("--database", required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--late-grace-seconds", type=int, default=300)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    if not 0.1 <= args.interval <= 60:
        raise SystemExit("worker interval must be between 0.1 and 60 seconds")
    if args.late_grace_seconds < 0:
        raise SystemExit("late grace must not be negative")
    database = Database(Path(args.database))
    database.migrate()
    reminders = ReminderRepository(database)
    scheduler = ReminderScheduler(
        reminders,
        SystemClock(),
        late_grace_seconds=args.late_grace_seconds,
    )
    briefing_scheduler = DailyBriefingScheduler(
        DailyBriefingRepository(database),
        AnniversaryRepository(database),
        AgendaRepository(database),
        reminders,
        OutboxStore(database),
        scheduler.clock,
    )
    notification_scheduler = AgendaNotificationScheduler(
        AgendaNotificationRepository(database),
        AgendaRepository(database),
        reminders,
        scheduler.clock,
    )
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    while not stop.is_set():
        materialized = notification_scheduler.tick()
        if materialized:
            logger.info("agenda_notifications_materialized count=%d", materialized)
        inserted = scheduler.tick()
        if inserted:
            logger.info("reminders_enqueued count=%d", inserted)
        briefings_inserted = briefing_scheduler.tick()
        if briefings_inserted:
            logger.info("daily_briefings_enqueued count=%d", briefings_inserted)
        if args.once:
            break
        stop.wait(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
