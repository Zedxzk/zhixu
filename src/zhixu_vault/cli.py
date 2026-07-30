"""Offline-safe vault initialization and audit verification CLI."""

from __future__ import annotations

import argparse
import getpass
from datetime import UTC, datetime

from .audit import VaultAuditLog
from .crypto import VaultKeyring
from .database import VaultDatabase


def _now() -> datetime:
    return datetime.now(UTC)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zhixu-vault")
    parser.add_argument("--database", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("initialize")
    commands.add_parser("verify-audit")
    commands.add_parser("status")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database = VaultDatabase(args.database)
    keyring = VaultKeyring(database, _now)
    if args.command == "initialize":
        first = getpass.getpass("New vault passphrase: ")
        second = getpass.getpass("Repeat vault passphrase: ")
        if first != second:
            print("Passphrases do not match.")
            return 2
        keyring.initialize(first)
        keyring.lock()
        print("Vault initialized and sealed.")
        return 0
    if args.command == "status":
        database.migrate()
        with database.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM secret_records"
            ).fetchone()
        print(f"sealed=true records={int(count[0]) if count else 0}")
        return 0
    if args.command == "verify-audit":
        keyring.unlock(getpass.getpass("Vault passphrase: "))
        try:
            valid = VaultAuditLog(database).verify(audit_key=keyring.audit_key())
        finally:
            keyring.lock()
        print("Audit chain valid." if valid else "Audit chain INVALID.")
        return 0 if valid else 3
    return 2
