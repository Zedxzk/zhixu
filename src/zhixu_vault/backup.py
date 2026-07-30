"""Additional authenticated encryption for backups and break-glass bundles."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
from contextlib import suppress
from pathlib import Path

from .crypto import Argon2Parameters, open_sealed, seal
from .database import VaultDatabase


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
        if target.exists():
            raise FileExistsError("backup destination already exists")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle, temporary_name = tempfile.mkstemp(
            prefix="zhixu-vault-backup-",
            suffix=".sqlite3",
            dir=target.parent,
        )
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            source = self.database.connect()
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
            target.write_text(
                json.dumps(envelope, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            with suppress(OSError):
                target.chmod(0o600)
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
        if target.exists():
            raise FileExistsError("restore destination already exists")
        envelope = json.loads(source_path.read_text(encoding="utf-8"))
        if envelope.get("format") != "zhixu-vault-backup-v1":
            raise ValueError("unsupported vault backup format")
        parameters = Argon2Parameters(**envelope["argon2"])
        salt = base64.urlsafe_b64decode(envelope["salt"])
        key = parameters.derive(backup_passphrase, salt)
        try:
            plain = open_sealed(
                key,
                str(envelope["ciphertext"]),
                context="vault-backup",
            )
        except Exception as exc:
            raise PermissionError("vault backup decryption failed") from exc
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes(plain)
        with suppress(OSError):
            target.chmod(0o600)
        restored = VaultDatabase(target)
        with restored.connect() as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if result is None or str(result[0]) != "ok":
                raise RuntimeError("restored vault failed integrity check")
        return restored

    def create_break_glass_bundle(
        self,
        destination: str | Path,
        *,
        recovery_passphrase: str,
    ) -> Path:
        """Create an offline-only recovery bundle using a separate passphrase."""

        return self.create(destination, backup_passphrase=recovery_passphrase)
