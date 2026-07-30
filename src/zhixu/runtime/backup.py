"""Automated encrypted backup with immediate isolated restore verification."""

from __future__ import annotations

import argparse
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from zhixu.adapters.storage.sqlite import ApplicationBackupManager, Database

from .common import read_text_credential


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zhixu-backup")
    parser.add_argument("--database", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--passphrase-file", required=True)
    parser.add_argument("--keep", type=int, default=14)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 2 <= args.keep <= 365:
        raise SystemExit("backup retention count is invalid")
    destination = Path(args.destination)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = destination / f"application-{timestamp}.zxb"
    passphrase = read_text_credential(args.passphrase_file)
    manager = ApplicationBackupManager(Database(args.database))
    manager.create(target, backup_passphrase=passphrase)
    with tempfile.TemporaryDirectory(prefix="zhixu-restore-drill-") as temporary:
        restored = Path(temporary) / "application.sqlite3"
        ApplicationBackupManager.restore(
            target,
            restored,
            backup_passphrase=passphrase,
        )
        with Database(restored).connect() as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            if row is None or str(row[0]) != "ok":
                raise RuntimeError("application restore drill failed")
    _retain(destination, "application-*.zxb", args.keep)
    print("Encrypted application backup and restore drill completed.")
    return 0


def _retain(directory: Path, pattern: str, keep: int) -> None:
    candidates = sorted(directory.glob(pattern), reverse=True)
    for path in candidates[keep:]:
        if path.is_symlink() or not path.is_file() or path.parent != directory:
            raise RuntimeError("unsafe backup retention target")
        path.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
