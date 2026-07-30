"""Offline vault daemon exposing only an authenticated Unix socket."""

from __future__ import annotations

import argparse
import asyncio
import grp
import os
import pwd
import signal
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .crypto import VaultKeyring
from .database import VaultDatabase
from .grants import CapabilityGrantVerifier
from .service import VaultService
from .storage import VaultRepository
from .unix_api import UnixVaultServer, VaultRPCDispatcher
from .webauthn_auth import PasskeyManager


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
    return parser


def _public_key(path: str | Path) -> Ed25519PublicKey:
    value = serialization.load_pem_public_key(Path(path).read_bytes())
    if not isinstance(value, Ed25519PublicKey):
        raise ValueError("vault grant issuer key must be Ed25519")
    return value


async def run(args: argparse.Namespace) -> None:
    if not 60 <= args.idle_timeout_seconds <= 86_400:
        raise ValueError("vault idle timeout is outside the allowed range")
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
    service = VaultService(VaultRepository(database, keyring, now), verifier)
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
    try:
        await stop.wait()
    finally:
        keyring.lock()
        await server.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
