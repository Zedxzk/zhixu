"""Authenticated encrypted backups for the ordinary application database."""

from __future__ import annotations

import base64
import binascii
import json
import os
import sqlite3
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .database import Database

MAX_BACKUP_ENVELOPE_BYTES = 2 * 1024 * 1024 * 1024


class ApplicationBackupManager:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, destination: str | Path, *, backup_passphrase: str) -> Path:
        target = Path(destination)
        if target.exists() or target.is_symlink():
            raise FileExistsError("backup destination already exists")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _require_real_directory(target.parent)
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
        if target.exists() or target.is_symlink():
            raise FileExistsError("restore destination already exists")
        envelope = _read_envelope(Path(source))
        if (
            set(envelope) != {"format", "salt", "nonce", "ciphertext"}
            or envelope.get("format") != "zhixu-application-backup-v1"
        ):
            raise ValueError("unsupported application backup format")
        salt = _decode(envelope["salt"])
        nonce = _decode(envelope["nonce"])
        ciphertext = _decode(envelope["ciphertext"])
        if len(salt) != 16 or len(nonce) != 12 or len(ciphertext) < 16:
            raise ValueError("application backup cryptographic fields are invalid")
        key = _derive(backup_passphrase, salt)
        try:
            plaintext = AESGCM(key).decrypt(
                nonce,
                ciphertext,
                b"zhixu-application-backup-v1",
            )
        except Exception as exc:
            raise PermissionError("application backup decryption failed") from exc
        if not plaintext.startswith(b"SQLite format 3\0"):
            raise ValueError("application backup plaintext is not a SQLite database")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _require_real_directory(target.parent)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.restore-",
            suffix=".partial",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                output.write(plaintext)
                output.flush()
                os.fsync(descriptor)
            with sqlite3.connect(
                f"{temporary.resolve().as_uri()}?mode=ro",
                uri=True,
            ) as connection:
                connection.execute("PRAGMA query_only=ON")
                row = connection.execute("PRAGMA integrity_check").fetchone()
                if row is None or str(row[0]) != "ok":
                    raise RuntimeError("restored application database failed integrity check")
            os.link(temporary, target)
            _fsync_directory(target.parent)
        finally:
            with suppress(OSError):
                os.close(descriptor)
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


def _decode(value: Any) -> bytes:
    if not isinstance(value, str) or len(value) > MAX_BACKUP_ENVELOPE_BYTES:
        raise ValueError("application backup Base64 field is invalid")
    try:
        return base64.b64decode(value, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("application backup Base64 field is invalid") from exc


def _read_envelope(path: Path) -> dict[str, Any]:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > MAX_BACKUP_ENVELOPE_BYTES
    ):
        raise ValueError("application backup file is invalid")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("application backup contains a duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("application backup JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("application backup envelope is invalid")
    return value


def _require_real_directory(path: Path) -> None:
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise PermissionError("backup directory must not be a symlink")


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
        os.link(temporary, target)
        _fsync_directory(target.parent)
    finally:
        with suppress(OSError):
            os.close(handle)
        with suppress(OSError):
            temporary.unlink()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
