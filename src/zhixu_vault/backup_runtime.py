"""Automated encrypted vault backup with a restore drill."""

from __future__ import annotations

import argparse
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from .backup import VaultBackupManager
from .database import VaultDatabase


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zhixu-vault-backup")
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
    target = destination / f"vault-{timestamp}.zxb"
    passphrase = _credential(args.passphrase_file)
    manager = VaultBackupManager(VaultDatabase(args.database))
    manager.create(target, backup_passphrase=passphrase)
    with tempfile.TemporaryDirectory(prefix="zhixu-vault-restore-drill-") as temporary:
        restored = Path(temporary) / "vault.sqlite3"
        VaultBackupManager.restore(
            target,
            restored,
            backup_passphrase=passphrase,
        )
        with VaultDatabase(restored).connect() as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            if row is None or str(row[0]) != "ok":
                raise RuntimeError("vault restore drill failed")
    _retain(destination, "vault-*.zxb", args.keep)
    print("Encrypted vault backup and restore drill completed.")
    return 0


def _credential(path: str | Path) -> str:
    value = Path(path).read_text(encoding="utf-8").strip()
    if not 12 <= len(value) <= 4096 or "\0" in value:
        raise ValueError("vault backup credential file is invalid")
    return value


def _retain(directory: Path, pattern: str, keep: int) -> None:
    candidates = sorted(directory.glob(pattern), reverse=True)
    for path in candidates[keep:]:
        if path.is_symlink() or not path.is_file() or path.parent != directory:
            raise RuntimeError("unsafe backup retention target")
        path.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
