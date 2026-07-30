"""Local operator CLI for setup, restore, and runtime diagnostics."""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .adapters.storage.sqlite import (
    AdminCredentialStore,
    ApplicationBackupManager,
    Database,
    UserRepository,
)
from .domain import Action, CommandContext, PolicyEngine, ResourceRef, User, UserStatus
from .runtime.probes import vault_available
from .vault_client import CapabilityGrantIssuer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zhixu",
        description="Privacy-first self-hosted personal assistant",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    commands = parser.add_subparsers(dest="command")

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--api-url", default="http://127.0.0.1:8840")
    doctor.add_argument("--database", default="/var/lib/zhixu/zhixu.sqlite3")
    doctor.add_argument("--vault-socket", default="/run/zhixu/vault.sock")

    bootstrap = commands.add_parser("bootstrap-admin")
    bootstrap.add_argument("--database", required=True)

    backup = commands.add_parser("backup")
    backup.add_argument("--database", required=True)
    backup.add_argument("--output", required=True)

    restore = commands.add_parser("restore")
    restore.add_argument("--input", required=True)
    restore.add_argument("--database", required=True)

    grant_key = commands.add_parser("generate-grant-key")
    grant_key.add_argument("--private-output", required=True)
    grant_key.add_argument("--public-output", required=True)
    return parser


def _require_tty() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise PermissionError("this command requires an interactive TTY")


def _doctor(args: argparse.Namespace) -> int:
    parsed = urlsplit(args.api_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
        print("api=invalid storage=unknown vault=unknown")
        return 2
    api = "unavailable"
    degraded = True
    try:
        with urllib.request.urlopen(
            args.api_url.rstrip("/") + "/health/ready",
            timeout=2,
        ) as response:
            value = json.loads(response.read(64 * 1024))
            if int(response.status) == 200 and value.get("core") == "ready":
                api = "ready"
                degraded = bool(value.get("degraded"))
    except (OSError, ValueError, urllib.error.URLError):
        pass
    storage = _database_status(Path(args.database))
    vault = "available" if vault_available(args.vault_socket, timeout=2) else "unavailable"
    overall = (
        "ready"
        if api == "ready" and storage == "ok"
        else "unavailable"
    )
    if overall == "ready" and (degraded or vault != "available"):
        overall = "degraded"
    print(f"status={overall} api={api} storage={storage} vault={vault}")
    return 0 if api == "ready" and storage == "ok" else 3


def _database_status(path: Path) -> str:
    if not path.is_file():
        return "missing"
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=2) as connection:
            row = connection.execute("PRAGMA quick_check").fetchone()
        return "ok" if row and str(row[0]) == "ok" else "invalid"
    except sqlite3.Error:
        return "unavailable"


def _bootstrap_admin(args: argparse.Namespace) -> int:
    _require_tty()
    user_id = input("Internal admin user id: ").strip()
    display_name = input("Display name: ").strip()
    first = getpass.getpass("Admin password: ")
    second = getpass.getpass("Repeat admin password: ")
    if first != second:
        print("Passwords do not match.")
        return 2
    database = Database(args.database)
    database.migrate()
    users = UserRepository(database)
    if users.get(user_id) is None:
        now = datetime.now(UTC)
        authorization = PolicyEngine().require(
            CommandContext(actor_user_id=user_id, now=now),
            Action.CREATE,
            ResourceRef("user", user_id, user_id),
        )
        users.create(
            User(user_id, display_name, UserStatus.ACTIVE, now),
            authorization,
        )
    AdminCredentialStore(database).set_password(
        user_id,
        first,
        now=datetime.now(UTC),
    )
    print("Admin principal initialized.")
    return 0


def _backup(args: argparse.Namespace) -> int:
    _require_tty()
    first = getpass.getpass("Backup passphrase: ")
    second = getpass.getpass("Repeat backup passphrase: ")
    if first != second:
        print("Passphrases do not match.")
        return 2
    ApplicationBackupManager(Database(args.database)).create(
        args.output,
        backup_passphrase=first,
    )
    print("Encrypted application backup created.")
    return 0


def _restore(args: argparse.Namespace) -> int:
    _require_tty()
    ApplicationBackupManager.restore(
        args.input,
        args.database,
        backup_passphrase=getpass.getpass("Backup passphrase: "),
    )
    print("Application backup restored and verified.")
    return 0


def _generate_grant_key(args: argparse.Namespace) -> int:
    private_path = Path(args.private_output)
    public_path = Path(args.public_output)
    if private_path == public_path:
        raise ValueError("grant key output paths must be different")
    issuer = CapabilityGrantIssuer.generate("zhixu-auth")
    private_payload = base64.urlsafe_b64encode(issuer.private_bytes()) + b"\n"
    private_fd = os.open(
        private_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    public_created = False
    try:
        with os.fdopen(private_fd, "wb") as private_file:
            private_file.write(private_payload)
        os.chmod(private_path, 0o600)
        public_fd = os.open(
            public_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o644,
        )
        public_created = True
        with os.fdopen(public_fd, "wb") as public_file:
            public_file.write(issuer.public_pem())
        os.chmod(public_path, 0o644)
    except Exception:
        private_path.unlink(missing_ok=True)
        if public_created:
            public_path.unlink(missing_ok=True)
        raise
    print("Grant issuer key pair created.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        build_parser().print_help()
        return 0
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "bootstrap-admin":
        return _bootstrap_admin(args)
    if args.command == "backup":
        return _backup(args)
    if args.command == "restore":
        return _restore(args)
    if args.command == "generate-grant-key":
        return _generate_grant_key(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
