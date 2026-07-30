"""Standalone SQLite database; attaching another database is prohibited."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path


class VaultDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.set_authorizer(self._authorizer)
        with suppress(OSError):
            self.path.chmod(0o600)
        return connection

    @staticmethod
    def _authorizer(
        action: int,
        _arg1: str | None,
        _arg2: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        if action in {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def migrate(self) -> list[int]:
        directory = Path(__file__).with_name("migrations")
        paths = sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql"))
        applied_now: list[int] = []
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations(
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                int(row["version"]): (str(row["name"]), str(row["checksum"]))
                for row in connection.execute(
                    "SELECT version,name,checksum FROM schema_migrations"
                )
            }
            for path in paths:
                version_text, _, name = path.stem.partition("_")
                version = int(version_text)
                script = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(script.encode()).hexdigest()
                if version in applied:
                    if applied[version] != (name, checksum):
                        raise RuntimeError(f"vault migration drift at version {version}")
                    continue
                connection.executescript(script)
                connection.execute(
                    """
                    INSERT INTO schema_migrations(version,name,checksum,applied_at)
                    VALUES(?,?,?,?)
                    """,
                    (version, name, checksum, datetime.now(UTC).isoformat()),
                )
                applied_now.append(version)
        return applied_now
