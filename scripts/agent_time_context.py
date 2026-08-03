"""Print the same trusted time context supplied to Zhixu model requests."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from zhixu.application.temporal_context import temporal_context


def main() -> int:
    parser = argparse.ArgumentParser(prog="agent_time_context")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    args = parser.parse_args()
    try:
        current = datetime.now(UTC).astimezone(ZoneInfo(args.timezone))
    except ZoneInfoNotFoundError as exc:
        parser.error(f"unknown timezone: {exc}")
    print(json.dumps(temporal_context(current), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
