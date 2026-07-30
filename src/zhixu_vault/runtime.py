"""Offline vault daemon exposing only an authenticated Unix socket."""

from __future__ import annotations

import argparse
import asyncio
import grp
import logging
import os
import pwd
import signal
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .audit import VaultAuditLog
from .crypto import VaultKeyring
from .database import VaultDatabase
from .executors import UnixSocketMachineExecutor
from .grants import CapabilityGrantVerifier
from .service import VaultService
from .storage import VaultRepository
from .unix_api import UnixVaultServer, VaultRPCDispatcher
from .webauthn_auth import PasskeyManager

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zhixu-vault-runtime")
    parser.add_argument("--database", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--socket-group", required=True)
    parser.add_argument("--allowed-user", action="append", default=[])
    parser.add_argument("--issuer", default="zhixu-auth")
    parser.add_argument("--issuer-public-key-file", required=True)
    parser.add_argument("--passkey-rp-id", required=True)
    parser.add_argument("--passkey-origin", required=True)
    parser.add_argument("--idle-timeout-seconds", type=int, default=600)
    parser.add_argument("--audit-checkpoint-directory", default="")
    parser.add_argument("--audit-checkpoint-seconds", type=int, default=300)
    parser.add_argument("--audit-checkpoint-keep", type=int, default=90)
    parser.add_argument(
        "--executor",
        action="append",
        default=[],
        metavar="NAME=UNIX_SOCKET",
        help="register a fixed local machine-secret executor boundary",
    )
    return parser


def _public_key(path: str | Path) -> Ed25519PublicKey:
    value = serialization.load_pem_public_key(Path(path).read_bytes())
    if not isinstance(value, Ed25519PublicKey):
        raise ValueError("vault grant issuer key must be Ed25519")
    return value


async def run(args: argparse.Namespace) -> None:
    if not 60 <= args.idle_timeout_seconds <= 86_400:
        raise ValueError("vault idle timeout is outside the allowed range")
    if not 30 <= args.audit_checkpoint_seconds <= 86_400:
        raise ValueError("vault audit checkpoint interval is outside the allowed range")
    if not 2 <= args.audit_checkpoint_keep <= 3650:
        raise ValueError("vault audit checkpoint retention is outside the allowed range")
    database = VaultDatabase(args.database)
    database.migrate()
    def now() -> datetime:
        return datetime.now(UTC)

    keyring = VaultKeyring(
        database,
        now,
        idle_timeout=timedelta(seconds=args.idle_timeout_seconds),
    )
    verifier = CapabilityGrantVerifier(
        database,
        issuers={args.issuer: _public_key(args.issuer_public_key_file)},
        now=now,
    )
    executors = _executors(args.executor)
    service = VaultService(
        VaultRepository(database, keyring, now),
        verifier,
        executors=executors,
    )
    passkeys = PasskeyManager(
        database,
        rp_id=args.passkey_rp_id,
        rp_name="Zhixu",
        expected_origin=args.passkey_origin,
        now=now,
    )
    allowed_uids = {os.getuid()}
    allowed_uids.update(pwd.getpwnam(name).pw_uid for name in args.allowed_user)
    socket_gid = grp.getgrnam(args.socket_group).gr_gid
    server = UnixVaultServer(
        args.socket,
        VaultRPCDispatcher(service, keyring, passkeys=passkeys),
        allowed_uids=allowed_uids,
        socket_gid=socket_gid,
    )
    await server.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for selected in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(selected, stop.set)
    checkpoint_task = (
        asyncio.create_task(
            _checkpoint_loop(
                stop,
                keyring,
                VaultAuditLog(database),
                Path(args.audit_checkpoint_directory),
                interval_seconds=args.audit_checkpoint_seconds,
                keep=args.audit_checkpoint_keep,
                now=now,
            )
        )
        if args.audit_checkpoint_directory
        else None
    )
    try:
        await stop.wait()
    finally:
        if checkpoint_task is not None:
            await checkpoint_task
        keyring.lock()
        await server.close()


async def _checkpoint_loop(
    stop: asyncio.Event,
    keyring: VaultKeyring,
    audit: VaultAuditLog,
    directory: Path,
    *,
    interval_seconds: int,
    keep: int,
    now: Callable[[], datetime],
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            _write_checkpoint(keyring, audit, directory, keep=keep, now=now)
    _write_checkpoint(keyring, audit, directory, keep=keep, now=now)


def _write_checkpoint(
    keyring: VaultKeyring,
    audit: VaultAuditLog,
    directory: Path,
    *,
    keep: int,
    now: Callable[[], datetime],
) -> None:
    if keyring.sealed:
        return
    try:
        audit.write_checkpoint(
            directory,
            audit_key=keyring.audit_key(touch=False),
            now=now(),
            keep=keep,
        )
    except Exception:
        LOGGER.error("vault_audit_checkpoint_failed")


def _executors(values: Sequence[str]) -> dict[str, UnixSocketMachineExecutor]:
    result: dict[str, UnixSocketMachineExecutor] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if (
            not separator
            or not name
            or len(name) > 80
            or not all(character.isalnum() or character in "._-" for character in name)
            or name in result
        ):
            raise ValueError("machine executor registration is invalid")
        result[name] = UnixSocketMachineExecutor(Path(raw_path))
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
