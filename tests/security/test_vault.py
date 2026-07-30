from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import stat
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from zhixu.vault_client import CapabilityGrantIssuer
from zhixu_vault.audit import VaultAuditLog
from zhixu_vault.backup import VaultBackupManager
from zhixu_vault.crypto import Argon2Parameters, VaultKeyring
from zhixu_vault.database import VaultDatabase
from zhixu_vault.executors import PATIntegrationExecutor, UnixSocketMachineExecutor
from zhixu_vault.grants import CapabilityGrantVerifier
from zhixu_vault.policy import SecretKind, VaultAction, VaultClassification
from zhixu_vault.service import ExecutionResult, VaultService
from zhixu_vault.storage import VaultRepository
from zhixu_vault.types import SecretValue
from zhixu_vault.unix_api import UnixVaultServer, VaultRPCDispatcher
from zhixu_vault.webauthn_auth import PasskeyManager

PASSPHRASE = "correct horse synthetic battery"
NOW = datetime(2026, 6, 1, 8, tzinfo=UTC)


@dataclass
class MutableClock:
    value: datetime = NOW

    def now(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class FingerprintExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(
        self,
        secret: SecretValue,
        request: dict[str, object],
    ) -> ExecutionResult:
        self.calls += 1
        digest = hashlib.sha256(secret.bytes()).hexdigest()[:12]
        return ExecutionResult(True, "executed", {"fingerprint": digest, **request})


@pytest.fixture
def vault(
    tmp_path: Path,
) -> tuple[
    VaultDatabase,
    VaultKeyring,
    VaultRepository,
    VaultService,
    CapabilityGrantIssuer,
    MutableClock,
    FingerprintExecutor,
]:
    clock = MutableClock()
    database = VaultDatabase(tmp_path / "vault.sqlite3")
    keyring = VaultKeyring(
        database,
        clock.now,
        idle_timeout=timedelta(minutes=5),
        parameters=Argon2Parameters(
            time_cost=2,
            memory_cost_kib=32_768,
            parallelism=1,
        ),
    )
    keyring.initialize(PASSPHRASE)
    issuer = CapabilityGrantIssuer.generate("auth_test")
    verifier = CapabilityGrantVerifier(
        database,
        issuers={
            "auth_test": Ed25519PublicKey.from_public_bytes(issuer.public_bytes())
        },
        now=clock.now,
    )
    repository = VaultRepository(database, keyring, clock.now)
    executor = FingerprintExecutor()
    service = VaultService(
        repository,
        verifier,
        executors={"fingerprint": executor},
    )
    return database, keyring, repository, service, issuer, clock, executor


def issue(
    issuer: CapabilityGrantIssuer,
    clock: MutableClock,
    *,
    secret_id: str,
    action: VaultAction,
    subject: str = "user_test",
    authentication: str = "step_up",
    expires_in: timedelta = timedelta(minutes=1),
) -> str:
    return issuer.issue(
        subject=subject,
        secret_id=secret_id,
        action=action.value,
        audience="zhixu-vault",
        expires_at=clock.now() + expires_in,
        authentication=authentication,
    )


def create_human(service: VaultService, value: str = "human-secret-canary") -> None:
    service.create_secret(
        secret_id="secret_human",
        owner_user_id="user_test",
        label="Synthetic human secret",
        kind=SecretKind.HUMAN,
        value=SecretValue.from_text(value),
        authentication="step_up",
    )


def create_machine(service: VaultService, value: str = "machine-secret-canary") -> None:
    service.create_secret(
        secret_id="secret_machine",
        owner_user_id="user_test",
        label="Synthetic machine secret",
        kind=SecretKind.MACHINE,
        value=SecretValue.from_text(value),
        authentication="step_up",
    )


def database_bytes(database: VaultDatabase) -> bytes:
    value = database.path.read_bytes()
    for suffix in ("-wal", "-shm"):
        path = database.path.with_name(database.path.name + suffix)
        if path.exists():
            value += path.read_bytes()
    return value


def test_secret_and_passphrase_never_reach_database_or_repr(
    vault: tuple[
        VaultDatabase,
        VaultKeyring,
        VaultRepository,
        VaultService,
        CapabilityGrantIssuer,
        MutableClock,
        FingerprintExecutor,
    ],
) -> None:
    database, keyring, _repository, service, issuer, clock, _executor = vault
    create_human(service)
    grant = issue(
        issuer,
        clock,
        secret_id="secret_human",
        action=VaultAction.REVEAL,
    )
    secret = service.reveal(grant, "secret_human")
    assert secret.text() == "human-secret-canary"
    assert "human-secret-canary" not in repr(secret)
    secret.clear()
    keyring.lock()

    with pytest.raises(PermissionError):
        keyring.unlock("incorrect synthetic passphrase")
    keyring.unlock(PASSPHRASE)
    stored = database_bytes(database)
    assert b"human-secret-canary" not in stored
    assert PASSPHRASE.encode() not in stored


def test_capability_is_exact_expiring_and_single_use(
    vault: tuple[
        VaultDatabase,
        VaultKeyring,
        VaultRepository,
        VaultService,
        CapabilityGrantIssuer,
        MutableClock,
        FingerprintExecutor,
    ],
) -> None:
    _database, _keyring, _repository, service, issuer, clock, _executor = vault
    create_human(service)
    reveal = issue(
        issuer,
        clock,
        secret_id="secret_human",
        action=VaultAction.REVEAL,
    )
    with service.reveal(reveal, "secret_human") as secret:
        assert secret.text() == "human-secret-canary"
    with pytest.raises(PermissionError):
        service.reveal(reveal, "secret_human")

    wrong_action = issue(
        issuer,
        clock,
        secret_id="secret_human",
        action=VaultAction.LIST_METADATA,
    )
    with pytest.raises(PermissionError):
        service.reveal(wrong_action, "secret_human")
    wrong_secret = issue(
        issuer,
        clock,
        secret_id="secret_other",
        action=VaultAction.REVEAL,
    )
    with pytest.raises(PermissionError):
        service.reveal(wrong_secret, "secret_human")
    expired = issue(
        issuer,
        clock,
        secret_id="secret_human",
        action=VaultAction.REVEAL,
        expires_in=timedelta(seconds=-1),
    )
    with pytest.raises(PermissionError):
        service.reveal(expired, "secret_human")


def test_secret_creation_and_owned_listing_require_signed_grants(
    vault: tuple[
        VaultDatabase,
        VaultKeyring,
        VaultRepository,
        VaultService,
        CapabilityGrantIssuer,
        MutableClock,
        FingerprintExecutor,
    ],
) -> None:
    database, _keyring, _repository, service, issuer, clock, _executor = vault
    value = SecretValue.from_text("created-through-grant-canary")
    created = service.create_with_grant(
        issue(
            issuer,
            clock,
            secret_id="secret_granted_create",
            action=VaultAction.CREATE,
        ),
        secret_id="secret_granted_create",
        owner_user_id="user_test",
        label="Synthetic created secret",
        kind=SecretKind.HUMAN,
        value=value,
    )
    assert created.id == "secret_granted_create"
    assert value.bytes() == b"\0" * len("created-through-grant-canary")

    listed = service.list_owned_metadata(
        issue(
            issuer,
            clock,
            secret_id="*",
            action=VaultAction.LIST_METADATA,
        )
    )
    assert [item.id for item in listed] == ["secret_granted_create"]
    assert b"created-through-grant-canary" not in database_bytes(database)

    rejected = SecretValue.from_text("rejected-secret-canary")
    with pytest.raises(PermissionError):
        service.create_with_grant(
            "invalid-grant",
            secret_id="secret_rejected",
            owner_user_id="user_test",
            label="Rejected",
            kind=SecretKind.HUMAN,
            value=rejected,
        )
    assert rejected.bytes() == b"\0" * len("rejected-secret-canary")


def test_human_reveal_and_machine_use_are_separate_paths(
    vault: tuple[
        VaultDatabase,
        VaultKeyring,
        VaultRepository,
        VaultService,
        CapabilityGrantIssuer,
        MutableClock,
        FingerprintExecutor,
    ],
) -> None:
    database, _keyring, repository, service, issuer, clock, executor = vault
    create_human(service)
    create_machine(service)
    repository.grant(
        "secret_machine",
        "service:integration",
        VaultAction.USE,
        actor="user_test",
    )
    use_grant = issue(
        issuer,
        clock,
        subject="service:integration",
        secret_id="secret_machine",
        action=VaultAction.USE,
        authentication="service",
    )
    result = service.use(
        use_grant,
        "secret_machine",
        executor_name="fingerprint",
        request={"operation": "synthetic"},
    )

    assert result.ok
    assert executor.calls == 1
    assert "machine-secret-canary" not in repr(result)
    assert b"machine-secret-canary" not in database_bytes(database)

    machine_reveal = issue(
        issuer,
        clock,
        secret_id="secret_machine",
        action=VaultAction.REVEAL,
    )
    with pytest.raises(PermissionError):
        service.reveal(machine_reveal, "secret_machine")
    human_use = issue(
        issuer,
        clock,
        secret_id="secret_human",
        action=VaultAction.USE,
    )
    with pytest.raises(PermissionError):
        service.use(
            human_use,
            "secret_human",
            executor_name="fingerprint",
            request={},
        )
    with database.connect() as connection:
        decisions = [
            (str(row["action"]), str(row["secret_id"]), str(row["outcome"]))
            for row in connection.execute(
                """
                SELECT action,secret_id,outcome FROM vault_audit
                WHERE action IN ('use','reveal')
                ORDER BY sequence
                """
            )
        ]
    assert ("use", "secret_machine", "completed") in decisions
    assert ("reveal", "secret_machine", "denied") in decisions
    assert ("use", "secret_human", "denied") in decisions


def test_auto_lock_rotation_passphrase_change_and_copy_resistance(
    vault: tuple[
        VaultDatabase,
        VaultKeyring,
        VaultRepository,
        VaultService,
        CapabilityGrantIssuer,
        MutableClock,
        FingerprintExecutor,
    ],
    tmp_path: Path,
) -> None:
    database, keyring, repository, service, issuer, clock, _executor = vault
    create_human(service, "rotation-secret-canary")
    clock.advance(timedelta(minutes=6))
    assert keyring.sealed

    keyring.unlock(PASSPHRASE)
    new_version = keyring.add_key_version(PASSPHRASE)
    repository.rewrap_data_keys(new_version, actor="user_test")
    keyring.change_passphrase(PASSPHRASE, "new synthetic passphrase value")
    keyring.lock()
    with pytest.raises(PermissionError):
        keyring.unlock(PASSPHRASE)
    keyring.unlock("new synthetic passphrase value")
    token = issue(
        issuer,
        clock,
        secret_id="secret_human",
        action=VaultAction.REVEAL,
    )
    with service.reveal(token, "secret_human") as secret:
        assert secret.text() == "rotation-secret-canary"

    copied_path = tmp_path / "copied-vault.sqlite3"
    with database.connect() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copy2(database.path, copied_path)
    copied = VaultKeyring(
        VaultDatabase(copied_path),
        clock.now,
        parameters=Argon2Parameters(2, 32_768, 1),
    )
    assert copied.sealed
    with pytest.raises(PermissionError):
        copied.unlock("unrelated synthetic passphrase")


def test_audit_chain_detects_tampering_and_attach_is_denied(
    vault: tuple[
        VaultDatabase,
        VaultKeyring,
        VaultRepository,
        VaultService,
        CapabilityGrantIssuer,
        MutableClock,
        FingerprintExecutor,
    ],
) -> None:
    database, keyring, repository, service, _issuer, _clock, _executor = vault
    create_human(service)
    audit = VaultAuditLog(database)
    assert audit.verify(audit_key=keyring.audit_key())

    with database.connect() as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("ATTACH DATABASE ':memory:' AS forbidden")
        connection.execute(
            "UPDATE vault_audit SET outcome='tampered' WHERE sequence=1"
        )
    alerts: list[str] = []
    assert not audit.verify_or_alert(
        audit_key=keyring.audit_key(),
        alert=alerts.append,
    )
    assert alerts == ["vault_audit_chain_invalid"]
    assert repository.get_metadata("secret_human").label == "Synthetic human secret"


def test_external_audit_checkpoint_detects_tail_truncation_without_extending_unlock(
    vault: tuple[
        VaultDatabase,
        VaultKeyring,
        VaultRepository,
        VaultService,
        CapabilityGrantIssuer,
        MutableClock,
        FingerprintExecutor,
    ],
    tmp_path: Path,
) -> None:
    database, keyring, _repository, service, _issuer, clock, _executor = vault
    create_human(service)
    audit = VaultAuditLog(database)
    checkpoint_key = keyring.audit_key(touch=False)
    clock.advance(timedelta(minutes=4))
    checkpoint = audit.write_checkpoint(
        tmp_path / "audit-checkpoints",
        audit_key=keyring.audit_key(touch=False),
        now=clock.now(),
    )

    assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600
    assert b"secret_human" not in checkpoint.read_bytes()
    assert b"human-secret-canary" not in checkpoint.read_bytes()
    assert audit.verify_latest_checkpoint(
        checkpoint.parent,
        audit_key=checkpoint_key,
    )

    clock.advance(timedelta(minutes=2))
    assert keyring.sealed
    with database.transaction() as connection:
        connection.execute(
            "DELETE FROM vault_audit WHERE sequence=(SELECT MAX(sequence) FROM vault_audit)"
        )
    assert audit.verify(audit_key=checkpoint_key)
    assert not audit.verify_latest_checkpoint(
        checkpoint.parent,
        audit_key=checkpoint_key,
    )


def test_passkey_registration_and_step_up_consume_challenges(
    vault: tuple[
        VaultDatabase,
        VaultKeyring,
        VaultRepository,
        VaultService,
        CapabilityGrantIssuer,
        MutableClock,
        FingerprintExecutor,
    ],
) -> None:
    database, _keyring, _repository, _service, _issuer, clock, _executor = vault
    seen: dict[str, object] = {}

    def verify_registration(**kwargs):
        seen["registration"] = kwargs
        return SimpleNamespace(
            credential_id=b"credential-test",
            credential_public_key=b"public-key-test",
            sign_count=0,
        )

    def verify_authentication(**kwargs):
        seen["authentication"] = kwargs
        return SimpleNamespace(new_sign_count=1)

    manager = PasskeyManager(
        database,
        rp_id="example.invalid",
        rp_name="Synthetic RP",
        expected_origin="https://example.invalid",
        now=clock.now,
        registration_verifier=verify_registration,
        authentication_verifier=verify_authentication,
    )
    registration_options = json.loads(
        manager.begin_registration(
            user_id="user_test",
            user_name="synthetic-user",
            display_name="Synthetic User",
        )
    )
    assert registration_options["rp"]["id"] == "example.invalid"
    credential_id = manager.finish_registration(
        user_id="user_test",
        credential={
            "id": "browser-registration-test",
            "response": {"transports": ["internal"]},
        },
    )
    assert credential_id
    assert seen["registration"]

    authentication_options = json.loads(
        manager.begin_authentication(user_id="user_test")
    )
    assert authentication_options["rpId"] == "example.invalid"
    proof = manager.finish_authentication(
        user_id="user_test",
        credential={"id": credential_id},
    )
    assert proof.user_id == "user_test"
    assert proof.expires_at > proof.authenticated_at
    assert seen["authentication"]
    with pytest.raises(PermissionError):
        manager.finish_authentication(
            user_id="user_test",
            credential={"id": credential_id},
        )


def test_export_rotation_backup_restore_and_l4_rejection(
    vault: tuple[
        VaultDatabase,
        VaultKeyring,
        VaultRepository,
        VaultService,
        CapabilityGrantIssuer,
        MutableClock,
        FingerprintExecutor,
    ],
    tmp_path: Path,
) -> None:
    database, keyring, _repository, service, issuer, clock, _executor = vault
    create_human(service, "export-secret-canary")
    export_grant = issue(
        issuer,
        clock,
        secret_id="secret_human",
        action=VaultAction.EXPORT,
    )
    exported = service.export_encrypted(
        export_grant,
        "secret_human",
        export_passphrase="synthetic export passphrase",
    )
    assert b"export-secret-canary" not in exported

    rotate_grant = issue(
        issuer,
        clock,
        secret_id="secret_human",
        action=VaultAction.ROTATE,
    )
    assert service.rotate_keys(
        rotate_grant,
        "secret_human",
        passphrase=PASSPHRASE,
    ) == 2

    prohibited = SecretValue.from_text("prohibited-secret-canary")
    with pytest.raises(PermissionError):
        service.create_secret(
            secret_id="secret_prohibited",
            owner_user_id="user_test",
            label="Prohibited",
            kind=SecretKind.HUMAN,
            classification=VaultClassification.PROHIBITED,
            value=prohibited,
            authentication="step_up",
        )
    assert prohibited.bytes() == b"\0" * len("prohibited-secret-canary")

    manager = VaultBackupManager(
        database,
        parameters=Argon2Parameters(2, 32_768, 1),
    )
    backup = manager.create(
        tmp_path / "vault.backup",
        backup_passphrase="synthetic backup passphrase",
    )
    assert b"export-secret-canary" not in backup.read_bytes()
    rejected_destination = tmp_path / "wrong-passphrase.sqlite3"
    with pytest.raises(PermissionError):
        manager.restore(
            backup,
            rejected_destination,
            backup_passphrase="wrong synthetic passphrase",
        )
    assert not rejected_destination.exists()
    restored = manager.restore(
        backup,
        tmp_path / "restored.sqlite3",
        backup_passphrase="synthetic backup passphrase",
    )
    restored_keyring = VaultKeyring(restored, clock.now)
    restored_keyring.unlock(PASSPHRASE)
    restored_repository = VaultRepository(restored, restored_keyring, clock.now)
    with restored_repository.decrypt("secret_human") as value:
        assert value.text() == "export-secret-canary"


def test_unix_socket_api_uses_peer_credentials_and_bounded_json(
    vault: tuple[
        VaultDatabase,
        VaultKeyring,
        VaultRepository,
        VaultService,
        CapabilityGrantIssuer,
        MutableClock,
        FingerprintExecutor,
    ],
    tmp_path: Path,
) -> None:
    _database, keyring, _repository, service, _issuer, _clock, _executor = vault

    async def scenario() -> dict[str, object]:
        server = UnixVaultServer(
            tmp_path / "vault.sock",
            VaultRPCDispatcher(service, keyring),
            allowed_uids={os.getuid()},
        )
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(server.path)
            writer.write(b'{"method":"status","params":{}}\n')
            await writer.drain()
            response = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
            return response
        finally:
            await server.close()

    response = asyncio.run(scenario())
    assert response == {"ok": True, "result": {"sealed": False}}


def test_pat_executor_allowlists_operations_and_blocks_secret_echo(
    vault: tuple[
        VaultDatabase,
        VaultKeyring,
        VaultRepository,
        VaultService,
        CapabilityGrantIssuer,
        MutableClock,
        FingerprintExecutor,
    ],
) -> None:
    _database, _keyring, repository, _service, issuer, clock, _executor = vault
    pat_executor = PATIntegrationExecutor(
        {
            "inspect": lambda _token, request: ExecutionResult(
                True,
                "completed",
                {"resource": request.get("resource")},
            ),
            "malicious": lambda token, _request: ExecutionResult(
                True,
                "completed",
                {"credential": token},
            ),
        }
    )
    verifier = CapabilityGrantVerifier(
        repository.database,
        issuers={
            "auth_test": Ed25519PublicKey.from_public_bytes(issuer.public_bytes())
        },
        now=clock.now,
    )
    service = VaultService(
        repository,
        verifier,
        executors={"pat": pat_executor},
    )
    create_machine(service, "pat-secret-canary")
    repository.grant(
        "secret_machine",
        "service:integration",
        VaultAction.USE,
        actor="user_test",
    )
    success = issue(
        issuer,
        clock,
        secret_id="secret_machine",
        action=VaultAction.USE,
        subject="service:integration",
        authentication="service",
    )
    result = service.use(
        success,
        "secret_machine",
        executor_name="pat",
        request={"operation": "inspect", "resource": "synthetic"},
    )
    assert result.data == {"resource": "synthetic"}

    malicious = issue(
        issuer,
        clock,
        secret_id="secret_machine",
        action=VaultAction.USE,
        subject="service:integration",
        authentication="service",
    )
    with pytest.raises(PermissionError):
        service.use(
            malicious,
            "secret_machine",
            executor_name="pat",
            request={"operation": "malicious"},
        )


def test_unix_machine_executor_uses_fixed_local_boundary_without_returning_secret(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "executor.sock"
    ready = threading.Event()
    received: dict[str, object] = {}

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(1)
            ready.set()
            connection, _address = server.accept()
            with connection:
                payload = bytearray()
                while not payload.endswith(b"\n"):
                    payload.extend(connection.recv(4096))
                received.update(json.loads(payload))
                connection.sendall(
                    b'{"ok":true,"code":"completed","data":{"resource":"synthetic"}}\n'
                )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(2)
    value = SecretValue.from_text("unix-executor-secret-canary")
    try:
        result = UnixSocketMachineExecutor(socket_path).execute(
            value,
            {"operation": "inspect"},
        )
    finally:
        value.clear()
    thread.join(timeout=2)

    assert result == ExecutionResult(
        True,
        "completed",
        {"resource": "synthetic"},
    )
    assert received["request"] == {"operation": "inspect"}
    assert received["credential"] == "unix-executor-secret-canary"
    assert "unix-executor-secret-canary" not in repr(result)
