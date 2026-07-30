"""Additional authenticated encryption for backups and break-glass bundles."""

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

from .crypto import Argon2Parameters, open_sealed, seal
from .database import VaultDatabase

MAX_BACKUP_ENVELOPE_BYTES = 2 * 1024 * 1024 * 1024


class VaultBackupManager:
    def __init__(
        self,
        database: VaultDatabase,
        *,
        parameters: Argon2Parameters | None = None,
    ) -> None:
        self.database = database
        self.parameters = parameters or Argon2Parameters()

    def create(self, destination: str | Path, *, backup_passphrase: str) -> Path:
        target = Path(destination)
        if target.exists() or target.is_symlink():
            raise FileExistsError("backup destination already exists")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _require_real_directory(target.parent)
        handle, temporary_name = tempfile.mkstemp(
            prefix="zhixu-vault-backup-",
            suffix=".sqlite3",
        )
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            if not self.database.path.is_file():
                raise FileNotFoundError("vault database does not exist")
            source = sqlite3.connect(
                f"{self.database.path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=10,
            )
            source.execute("PRAGMA busy_timeout=10000")
            backup = sqlite3.connect(temporary)
            try:
                source.backup(backup)
            finally:
                backup.close()
                source.close()
            plain = temporary.read_bytes()
            salt = os.urandom(16)
            key = self.parameters.derive(backup_passphrase, salt)
            envelope = {
                "format": "zhixu-vault-backup-v1",
                "salt": base64.urlsafe_b64encode(salt).decode(),
                "argon2": {
                    "time_cost": self.parameters.time_cost,
                    "memory_cost_kib": self.parameters.memory_cost_kib,
                    "parallelism": self.parameters.parallelism,
                },
                "ciphertext": seal(key, plain, context="vault-backup"),
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
    ) -> VaultDatabase:
        source_path = Path(source)
        target = Path(destination)
        if target.exists() or target.is_symlink():
            raise FileExistsError("restore destination already exists")
        envelope = _read_envelope(source_path)
        if set(envelope) != {"format", "salt", "argon2", "ciphertext"}:
            raise ValueError("vault backup envelope is invalid")
        if envelope.get("format") != "zhixu-vault-backup-v1":
            raise ValueError("unsupported vault backup format")
        settings = envelope["argon2"]
        if (
            not isinstance(settings, dict)
            or set(settings) != {
                "time_cost",
                "memory_cost_kib",
                "parallelism",
            }
            or any(
                isinstance(settings[name], bool)
                or not isinstance(settings[name], int)
                for name in (
                    "time_cost",
                    "memory_cost_kib",
                    "parallelism",
                )
            )
        ):
            raise ValueError("vault backup Argon2id parameters are invalid")
        parameters = Argon2Parameters(**settings)
        salt = _decode(envelope["salt"])
        if len(salt) != 16:
            raise ValueError("vault backup salt is invalid")
        ciphertext = envelope["ciphertext"]
        if (
            not isinstance(ciphertext, str)
            or not ciphertext.startswith("enc:v1:")
            or len(ciphertext) > MAX_BACKUP_ENVELOPE_BYTES
        ):
            raise ValueError("vault backup ciphertext is invalid")
        key = parameters.derive(backup_passphrase, salt)
        try:
            plain = open_sealed(
                key,
                ciphertext,
                context="vault-backup",
            )
        except Exception as exc:
            raise PermissionError("vault backup decryption failed") from exc
        if not plain.startswith(b"SQLite format 3\0"):
            raise ValueError("vault backup plaintext is not a SQLite database")
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
                output.write(plain)
                output.flush()
                os.fsync(descriptor)
            with sqlite3.connect(
                f"{temporary.resolve().as_uri()}?mode=ro",
                uri=True,
            ) as connection:
                connection.execute("PRAGMA query_only=ON")
                result = connection.execute("PRAGMA integrity_check").fetchone()
                if result is None or str(result[0]) != "ok":
                    raise RuntimeError("restored vault failed integrity check")
            os.link(temporary, target)
            _fsync_directory(target.parent)
        finally:
            with suppress(OSError):
                os.close(descriptor)
            with suppress(OSError):
                temporary.unlink()
        return VaultDatabase(target)

    def create_break_glass_bundle(
        self,
        destination: str | Path,
        *,
        recovery_passphrase: str,
    ) -> Path:
        """Create an offline-only recovery bundle using a separate passphrase."""

        return self.create(destination, backup_passphrase=recovery_passphrase)


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


def _decode(value: Any) -> bytes:
    if not isinstance(value, str) or len(value) > MAX_BACKUP_ENVELOPE_BYTES:
        raise ValueError("vault backup Base64 field is invalid")
    try:
        return base64.b64decode(value, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("vault backup Base64 field is invalid") from exc


def _read_envelope(path: Path) -> dict[str, Any]:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > MAX_BACKUP_ENVELOPE_BYTES
    ):
        raise ValueError("vault backup file is invalid")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("vault backup contains a duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("vault backup JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("vault backup envelope is invalid")
    return value


def _require_real_directory(path: Path) -> None:
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise PermissionError("backup directory must not be a symlink")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
