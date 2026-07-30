"""Authenticated encrypted backups for the ordinary application database."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
from contextlib import suppress
from pathlib import Path

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .database import Database


class ApplicationBackupManager:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, destination: str | Path, *, backup_passphrase: str) -> Path:
        target = Path(destination)
        if target.exists():
            raise FileExistsError("backup destination already exists")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle, temporary_name = tempfile.mkstemp(
            prefix="zhixu-app-backup-",
            suffix=".sqlite3",
        )
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            if not self.database.path.is_file():
                raise FileNotFoundError("application database does not exist")
            source = sqlite3.connect(
                f"{self.database.path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=10,
            )
            source.execute("PRAGMA busy_timeout=10000")
            copy = sqlite3.connect(temporary)
            try:
                source.backup(copy)
            finally:
                copy.close()
                source.close()
            salt = os.urandom(16)
            key = _derive(backup_passphrase, salt)
            nonce = os.urandom(12)
            ciphertext = AESGCM(key).encrypt(
                nonce,
                temporary.read_bytes(),
                b"zhixu-application-backup-v1",
            )
            envelope = {
                "format": "zhixu-application-backup-v1",
                "salt": _encode(salt),
                "nonce": _encode(nonce),
                "ciphertext": _encode(ciphertext),
            }
            _atomic_write(
                target,
                json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode(),
            )
            return target
        finally:
            with suppress(OSError):
                temporary.unlink()

    @staticmethod
    def restore(
        source: str | Path,
        destination: str | Path,
        *,
        backup_passphrase: str,
    ) -> Database:
        target = Path(destination)
        if target.exists():
            raise FileExistsError("restore destination already exists")
        envelope = json.loads(Path(source).read_text(encoding="utf-8"))
        if envelope.get("format") != "zhixu-application-backup-v1":
            raise ValueError("unsupported application backup format")
        key = _derive(backup_passphrase, _decode(str(envelope["salt"])))
        try:
            plaintext = AESGCM(key).decrypt(
                _decode(str(envelope["nonce"])),
                _decode(str(envelope["ciphertext"])),
                b"zhixu-application-backup-v1",
            )
        except Exception as exc:
            raise PermissionError("application backup decryption failed") from exc
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = target.with_name(f".{target.name}.restore-{os.getpid()}")
        try:
            temporary.write_bytes(plaintext)
            with sqlite3.connect(temporary) as connection:
                row = connection.execute("PRAGMA integrity_check").fetchone()
                if row is None or str(row[0]) != "ok":
                    raise RuntimeError("restored application database failed integrity check")
            os.replace(temporary, target)
            with suppress(OSError):
                target.chmod(0o600)
        finally:
            with suppress(OSError):
                temporary.unlink()
        return Database(target)


def _derive(passphrase: str, salt: bytes) -> bytes:
    if len(passphrase) < 12:
        raise ValueError("backup passphrase must contain at least 12 characters")
    return hash_secret_raw(
        passphrase.encode(),
        salt,
        time_cost=3,
        memory_cost=65_536,
        parallelism=4,
        hash_len=32,
        type=Type.ID,
    )


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode()


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value)


def _atomic_write(target: Path, payload: bytes) -> None:
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".partial",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        with suppress(OSError):
            os.close(handle)
        with suppress(OSError):
            temporary.unlink()
