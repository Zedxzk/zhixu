"""Argon2id unlock and AES-GCM envelope-key lifecycle."""

from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .database import VaultDatabase

_PREFIX = "enc:v1:"


def seal(key: bytes, plaintext: bytes, *, context: str) -> str:
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, context.encode())
    return _PREFIX + base64.urlsafe_b64encode(nonce + ciphertext).decode()


def open_sealed(key: bytes, value: str, *, context: str) -> bytes:
    if not value.startswith(_PREFIX):
        raise ValueError("unsupported encrypted value")
    try:
        raw = base64.b64decode(
            value.removeprefix(_PREFIX),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid encrypted value") from exc
    if len(raw) < 29:
        raise ValueError("invalid encrypted value")
    return AESGCM(key).decrypt(raw[:12], raw[12:], context.encode())


@dataclass(frozen=True, slots=True)
class Argon2Parameters:
    time_cost: int = 3
    memory_cost_kib: int = 65_536
    parallelism: int = 4

    def __post_init__(self) -> None:
        if (
            not 2 <= self.time_cost <= 10
            or not 32_768 <= self.memory_cost_kib <= 1_048_576
            or not 1 <= self.parallelism <= 16
        ):
            raise ValueError("Argon2id parameters are outside the supported bounds")

    def derive(self, passphrase: str, salt: bytes) -> bytes:
        if len(passphrase) < 12:
            raise ValueError("vault passphrase must contain at least 12 characters")
        return hash_secret_raw(
            passphrase.encode(),
            salt,
            time_cost=self.time_cost,
            memory_cost=self.memory_cost_kib,
            parallelism=self.parallelism,
            hash_len=32,
            type=Type.ID,
        )


@dataclass(slots=True)
class VaultKeyring:
    database: VaultDatabase
    now: Callable[[], datetime]
    idle_timeout: timedelta = timedelta(minutes=10)
    parameters: Argon2Parameters = field(default_factory=Argon2Parameters)
    _keys: dict[int, bytearray] = field(default_factory=dict, init=False, repr=False)
    _last_used_at: datetime | None = field(default=None, init=False, repr=False)

    @property
    def sealed(self) -> bool:
        if self._last_used_at is not None and self.now() - self._last_used_at >= self.idle_timeout:
            self.lock()
        return not self._keys

    def initialize(self, passphrase: str) -> None:
        self.database.migrate()
        with self.database.transaction() as connection:
            exists = connection.execute("SELECT 1 FROM key_versions LIMIT 1").fetchone()
            if exists is not None:
                raise RuntimeError("vault is already initialized")
            salt = os.urandom(16)
            unlock_key = self.parameters.derive(passphrase, salt)
            master_key = os.urandom(32)
            created = self.now().astimezone(UTC).isoformat()
            connection.execute(
                "INSERT INTO vault_meta(key,value) VALUES('argon2_salt',?)",
                (base64.urlsafe_b64encode(salt).decode(),),
            )
            connection.execute(
                "INSERT INTO vault_meta(key,value) VALUES('argon2_parameters',?)",
                (
                    json.dumps(
                        {
                            "time_cost": self.parameters.time_cost,
                            "memory_cost_kib": self.parameters.memory_cost_kib,
                            "parallelism": self.parameters.parallelism,
                        },
                        separators=(",", ":"),
                    ),
                ),
            )
            connection.execute(
                """
                INSERT INTO key_versions(version,wrapped_master_key,status,created_at)
                VALUES(1,?,'active',?)
                """,
                (
                    seal(unlock_key, master_key, context="vault-master-key:1"),
                    created,
                ),
            )
        self._install_keys({1: master_key})

    def unlock(self, passphrase: str) -> None:
        with self.database.connect() as connection:
            meta = {
                str(row["key"]): str(row["value"])
                for row in connection.execute(
                    "SELECT key,value FROM vault_meta WHERE key LIKE 'argon2_%'"
                )
            }
            rows = connection.execute(
                "SELECT version,wrapped_master_key FROM key_versions ORDER BY version"
            ).fetchall()
        if not rows:
            raise RuntimeError("vault is not initialized")
        settings = json.loads(meta["argon2_parameters"])
        parameters = Argon2Parameters(**settings)
        salt = base64.urlsafe_b64decode(meta["argon2_salt"])
        unlock_key = parameters.derive(passphrase, salt)
        try:
            keys = {
                int(row["version"]): open_sealed(
                    unlock_key,
                    str(row["wrapped_master_key"]),
                    context=f"vault-master-key:{int(row['version'])}",
                )
                for row in rows
            }
        except Exception as exc:
            raise PermissionError("vault unlock failed") from exc
        self.parameters = parameters
        self._install_keys(keys)

    def lock(self) -> None:
        for key in self._keys.values():
            for index in range(len(key)):
                key[index] = 0
        self._keys.clear()
        self._last_used_at = None

    def key(self, version: int) -> bytes:
        if self.sealed:
            raise PermissionError("vault is sealed")
        value = self._keys.get(version)
        if value is None:
            raise KeyError("vault key version is unavailable")
        self._last_used_at = self.now()
        return bytes(value)

    def active(self) -> tuple[int, bytes]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT version FROM key_versions WHERE status='active'"
            ).fetchone()
        if row is None:
            raise RuntimeError("vault has no active key")
        version = int(row["version"])
        return version, self.key(version)

    def audit_key(self) -> bytes:
        version = min(self._keys) if not self.sealed else 1
        master = self.key(version)
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=f"zhixu-vault-audit:{version}".encode(),
        ).derive(master)

    def add_key_version(self, passphrase: str) -> int:
        if self.sealed:
            raise PermissionError("vault is sealed")
        salt, parameters = self._load_derivation()
        unlock_key = parameters.derive(passphrase, salt)
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT version,wrapped_master_key FROM key_versions ORDER BY version"
            ).fetchall()
            try:
                for row in rows:
                    open_sealed(
                        unlock_key,
                        str(row["wrapped_master_key"]),
                        context=f"vault-master-key:{int(row['version'])}",
                    )
            except Exception as exc:
                raise PermissionError("vault passphrase verification failed") from exc
            version = max(int(row["version"]) for row in rows) + 1
            master = os.urandom(32)
            connection.execute("UPDATE key_versions SET status='retired'")
            connection.execute(
                """
                INSERT INTO key_versions(version,wrapped_master_key,status,created_at)
                VALUES(?,?,'active',?)
                """,
                (
                    version,
                    seal(unlock_key, master, context=f"vault-master-key:{version}"),
                    self.now().astimezone(UTC).isoformat(),
                ),
            )
        self._keys[version] = bytearray(master)
        self._last_used_at = self.now()
        return version

    def change_passphrase(self, old_passphrase: str, new_passphrase: str) -> None:
        if self.sealed:
            raise PermissionError("vault is sealed")
        old_salt, old_parameters = self._load_derivation()
        old_key = old_parameters.derive(old_passphrase, old_salt)
        new_salt = os.urandom(16)
        new_key = self.parameters.derive(new_passphrase, new_salt)
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT version,wrapped_master_key FROM key_versions"
            ).fetchall()
            plain: dict[int, bytes] = {}
            try:
                for row in rows:
                    version = int(row["version"])
                    plain[version] = open_sealed(
                        old_key,
                        str(row["wrapped_master_key"]),
                        context=f"vault-master-key:{version}",
                    )
            except Exception as exc:
                raise PermissionError("old vault passphrase is invalid") from exc
            for version, master in plain.items():
                connection.execute(
                    "UPDATE key_versions SET wrapped_master_key=? WHERE version=?",
                    (
                        seal(new_key, master, context=f"vault-master-key:{version}"),
                        version,
                    ),
                )
            connection.execute(
                "UPDATE vault_meta SET value=? WHERE key='argon2_salt'",
                (base64.urlsafe_b64encode(new_salt).decode(),),
            )

    def _load_derivation(self) -> tuple[bytes, Argon2Parameters]:
        with self.database.connect() as connection:
            values = {
                str(row["key"]): str(row["value"])
                for row in connection.execute(
                    "SELECT key,value FROM vault_meta WHERE key LIKE 'argon2_%'"
                )
            }
        return (
            base64.urlsafe_b64decode(values["argon2_salt"]),
            Argon2Parameters(**json.loads(values["argon2_parameters"])),
        )

    def _install_keys(self, values: dict[int, bytes]) -> None:
        self.lock()
        self._keys = {version: bytearray(value) for version, value in values.items()}
        self._last_used_at = self.now()
