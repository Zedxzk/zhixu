"""Interactive vault administration over the Unix socket."""

from __future__ import annotations

import argparse
import getpass
import json
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

from .audit import AuditEvent, VaultAuditLog
from .backup import VaultBackupManager
from .crypto import VaultKeyring
from .database import VaultDatabase
from .storage import VaultRepository

MAX_FRAME_BYTES = 64 * 1024


def _now() -> datetime:
    return datetime.now(UTC)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zhixu-vault")
    commands = parser.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser("initialize")
    initialize.add_argument("--database", required=True)

    verify = commands.add_parser("verify-audit")
    verify.add_argument("--database", required=True)
    verify.add_argument("--checkpoint-directory")

    backup = commands.add_parser("backup")
    backup.add_argument("--database", required=True)
    backup.add_argument("--output", required=True)

    restore = commands.add_parser("restore")
    restore.add_argument("--input", required=True)
    restore.add_argument("--database", required=True)

    rotate = commands.add_parser("rotate-keys")
    rotate.add_argument("--database", required=True)

    change_passphrase = commands.add_parser("change-passphrase")
    change_passphrase.add_argument("--database", required=True)

    for name in ("status", "unlock", "lock"):
        command = commands.add_parser(name)
        command.add_argument("--socket", default="/run/zhixu/vault/vault.sock")
    return parser


def _require_tty() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise PermissionError("this vault command requires an interactive TTY")


def _rpc(path: str | Path, method: str, params: dict[str, object]) -> dict[str, object]:
    request = json.dumps(
        {"method": method, "params": params},
        separators=(",", ":"),
    ).encode() + b"\n"
    if len(request) > MAX_FRAME_BYTES:
        raise ValueError("vault request exceeds the frame limit")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    try:
        client.connect(str(path))
        client.sendall(request)
        raw = bytearray()
        while len(raw) <= MAX_FRAME_BYTES:
            chunk = client.recv(4096)
            if not chunk:
                break
            raw.extend(chunk)
            if b"\n" in chunk:
                break
    finally:
        client.close()
    if not raw or len(raw) > MAX_FRAME_BYTES:
        raise RuntimeError("vault returned an invalid response")
    response = json.loads(bytes(raw).split(b"\n", 1)[0])
    if not isinstance(response, dict) or not response.get("ok"):
        raise RuntimeError("vault command was rejected")
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("vault returned an invalid result")
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {
        "initialize",
        "verify-audit",
        "backup",
        "restore",
        "rotate-keys",
        "change-passphrase",
    }:
        _require_tty()
    if args.command == "initialize":
        database = VaultDatabase(args.database)
        keyring = VaultKeyring(database, _now)
        first = getpass.getpass("New vault passphrase: ")
        second = getpass.getpass("Repeat vault passphrase: ")
        if first != second:
            print("Passphrases do not match.")
            return 2
        keyring.initialize(first)
        keyring.lock()
        print("Vault initialized and sealed.")
        return 0
    if args.command == "verify-audit":
        database = VaultDatabase(args.database)
        keyring = VaultKeyring(database, _now)
        keyring.unlock(getpass.getpass("Vault passphrase: "))
        try:
            audit = VaultAuditLog(database)
            audit_key = keyring.audit_key()
            valid = audit.verify(audit_key=audit_key)
            if valid and args.checkpoint_directory:
                valid = audit.verify_latest_checkpoint(
                    args.checkpoint_directory,
                    audit_key=audit_key,
                )
        finally:
            keyring.lock()
        print(
            "Audit chain and checkpoint valid."
            if valid and args.checkpoint_directory
            else "Audit chain valid."
            if valid
            else "Audit chain INVALID."
        )
        return 0 if valid else 3
    if args.command == "backup":
        first = getpass.getpass("Backup passphrase: ")
        second = getpass.getpass("Repeat backup passphrase: ")
        if first != second:
            print("Passphrases do not match.")
            return 2
        VaultBackupManager(VaultDatabase(args.database)).create(
            args.output,
            backup_passphrase=first,
        )
        print("Encrypted vault backup created.")
        return 0
    if args.command == "restore":
        passphrase = getpass.getpass("Backup passphrase: ")
        VaultBackupManager.restore(
            args.input,
            args.database,
            backup_passphrase=passphrase,
        )
        print("Vault backup restored and verified.")
        return 0
    if args.command == "rotate-keys":
        database = VaultDatabase(args.database)
        keyring = VaultKeyring(database, _now)
        passphrase = getpass.getpass("Vault passphrase: ")
        keyring.unlock(passphrase)
        try:
            version = keyring.add_key_version(passphrase)
            VaultRepository(database, keyring, _now).rewrap_data_keys(
                version,
                actor="operator:local",
            )
        finally:
            keyring.lock()
        print(f"Vault data keys rewrapped to key version {version}.")
        return 0
    if args.command == "change-passphrase":
        database = VaultDatabase(args.database)
        keyring = VaultKeyring(database, _now)
        old = getpass.getpass("Current vault passphrase: ")
        new = getpass.getpass("New vault passphrase: ")
        repeated = getpass.getpass("Repeat new vault passphrase: ")
        if new != repeated:
            print("Passphrases do not match.")
            return 2
        keyring.unlock(old)
        try:
            keyring.change_passphrase(old, new)
            with database.transaction() as connection:
                VaultAuditLog(database).append(
                    connection,
                    AuditEvent(
                        _now(),
                        "operator:local",
                        "change_passphrase",
                        "*",
                        "completed",
                    ),
                    audit_key=keyring.audit_key(),
                )
        finally:
            keyring.lock()
        print("Vault passphrase changed; vault remains sealed.")
        return 0
    if args.command == "status":
        result = _rpc(args.socket, "status", {})
        print(f"sealed={str(bool(result.get('sealed'))).lower()}")
        return 0
    if args.command == "unlock":
        _require_tty()
        result = _rpc(
            args.socket,
            "unlock",
            {"passphrase": getpass.getpass("Vault passphrase: ")},
        )
        print(f"sealed={str(bool(result.get('sealed'))).lower()}")
        return 0
    if args.command == "lock":
        _require_tty()
        result = _rpc(args.socket, "lock", {})
        print(f"sealed={str(bool(result.get('sealed'))).lower()}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
