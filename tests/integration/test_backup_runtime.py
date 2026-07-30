from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zhixu.adapters.storage.sqlite import (
    ApplicationBackupManager,
    Database,
    UserRepository,
)
from zhixu.domain import Action, CommandContext, PolicyEngine, ResourceRef, User, UserStatus
from zhixu.runtime.backup import main as backup_main
from zhixu_vault.backup_runtime import main as vault_backup_main
from zhixu_vault.crypto import Argon2Parameters, VaultKeyring
from zhixu_vault.database import VaultDatabase

NOW = datetime(2026, 7, 30, 12, tzinfo=UTC)
SYNTHETIC_BACKUP_PHRASE = "synthetic application backup phrase"
SYNTHETIC_VAULT_BACKUP_PHRASE = "synthetic vault backup phrase"
SYNTHETIC_VAULT_UNLOCK_PHRASE = "synthetic vault unlock phrase"


def test_application_backup_runtime_encrypts_and_performs_restore_drill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "application.sqlite3")
    database.migrate()
    policy = PolicyEngine()
    UserRepository(database).create(
        User("user_backup_test", "Backup Canary", UserStatus.ACTIVE, NOW),
        policy.require(
            CommandContext(actor_user_id="user_backup_test", now=NOW),
            Action.CREATE,
            ResourceRef("user", "user_backup_test", "user_backup_test"),
        ),
    )
    credential_file = tmp_path / "backup-credential"
    credential_file.write_text(SYNTHETIC_BACKUP_PHRASE, encoding="utf-8")
    destination = tmp_path / "encrypted"

    assert (
        backup_main(
            [
                "--database",
                str(database.path),
                "--destination",
                str(destination),
                "--passphrase-file",
                str(credential_file),
                "--keep",
                "2",
            ]
        )
        == 0
    )
    artifacts = list(destination.glob("application-*.zxb"))
    assert len(artifacts) == 1
    envelope = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert envelope["format"] == "zhixu-application-backup-v1"
    raw = artifacts[0].read_bytes()
    assert b"SQLite format 3" not in raw
    assert b"Backup Canary" not in raw

    restored_path = tmp_path / "restored.sqlite3"
    ApplicationBackupManager.restore(
        artifacts[0],
        restored_path,
        backup_passphrase=SYNTHETIC_BACKUP_PHRASE,
    )
    assert UserRepository(Database(restored_path)).get("user_backup_test") is not None

    rejected_path = tmp_path / "wrong-passphrase.sqlite3"
    with pytest.raises(PermissionError):
        ApplicationBackupManager.restore(
            artifacts[0],
            rejected_path,
            backup_passphrase="synthetic but incorrect phrase",
        )
    assert not rejected_path.exists()

    failed_target = tmp_path / "atomic-failure.zxb"

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("synthetic storage failure")

    monkeypatch.setattr(
        "zhixu.adapters.storage.sqlite.backup.os.replace",
        fail_replace,
    )
    with pytest.raises(OSError, match="synthetic storage failure"):
        ApplicationBackupManager(database).create(
            failed_target,
            backup_passphrase=SYNTHETIC_BACKUP_PHRASE,
        )
    assert not failed_target.exists()
    assert not list(tmp_path.glob(".atomic-failure.zxb.*.partial"))


def test_vault_backup_runtime_encrypts_and_performs_restore_drill(
    tmp_path: Path,
) -> None:
    database = VaultDatabase(tmp_path / "vault.sqlite3")
    database.migrate()
    keyring = VaultKeyring(
        database,
        lambda: NOW,
        parameters=Argon2Parameters(
            time_cost=2,
            memory_cost_kib=32_768,
            parallelism=1,
        ),
    )
    keyring.initialize(SYNTHETIC_VAULT_UNLOCK_PHRASE)
    keyring.lock()
    credential_file = tmp_path / "vault-backup-credential"
    credential_file.write_text(SYNTHETIC_VAULT_BACKUP_PHRASE, encoding="utf-8")
    destination = tmp_path / "vault-encrypted"

    assert (
        vault_backup_main(
            [
                "--database",
                str(database.path),
                "--destination",
                str(destination),
                "--passphrase-file",
                str(credential_file),
                "--keep",
                "2",
            ]
        )
        == 0
    )
    artifacts = list(destination.glob("vault-*.zxb"))
    assert len(artifacts) == 1
    envelope = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert envelope["format"] == "zhixu-vault-backup-v1"
    raw = artifacts[0].read_bytes()
    assert b"SQLite format 3" not in raw
    assert SYNTHETIC_VAULT_UNLOCK_PHRASE.encode() not in raw
