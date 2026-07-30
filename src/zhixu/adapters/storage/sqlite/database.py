"""SQLite connection and immutable, checksummed SQL migrations."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path


class MigrationDriftError(RuntimeError):
    """An applied migration no longer matches its recorded checksum."""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        with suppress(OSError):
            self.path.chmod(0o600)
        return connection

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
        migration_dir = Path(__file__).with_name("migrations")
        migrations = sorted(migration_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"))
        applied_now: list[int] = []
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
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
            for path in migrations:
                version_text, _, name = path.stem.partition("_")
                version = int(version_text)
                script = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(script.encode("utf-8")).hexdigest()
                if version in applied:
                    applied_name, applied_checksum = applied[version]
                    if applied_name != name or applied_checksum != checksum:
                        raise MigrationDriftError(
                            f"applied migration {version} does not match source"
                        )
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
