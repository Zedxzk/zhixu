"""Deterministic reminder scheduler process."""

from __future__ import annotations

import argparse
import logging
import signal
import threading
from collections.abc import Sequence
from pathlib import Path

from zhixu.adapters.storage.sqlite import Database, ReminderRepository
from zhixu.application import ReminderScheduler
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
    scheduler = ReminderScheduler(
        ReminderRepository(database),
        SystemClock(),
        late_grace_seconds=args.late_grace_seconds,
    )
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    while not stop.is_set():
        inserted = scheduler.tick()
        if inserted:
            logger.info("reminders_enqueued count=%d", inserted)
        if args.once:
            break
        stop.wait(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
