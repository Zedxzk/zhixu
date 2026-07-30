from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zhixu.adapters.storage.sqlite import (
    AdminCredentialStore,
    AdminReadStore,
    AdminSessionStore,
    AgendaRepository,
    Database,
    GrantRepository,
    IdentityLinkStore,
    NoteRepository,
    ReminderRepository,
    TaskRepository,
    UserRepository,
)
from zhixu.adapters.web import AdminAPI, HealthRegistry
from zhixu.application import ZhixuServices
from zhixu.delivery import OutboxStore
from zhixu.domain import (
    Action,
    AuthenticationStrength,
    CommandContext,
    PolicyEngine,
    ResourceRef,
    User,
    UserStatus,
)
from zhixu.ports import FrozenClock
from zhixu.security import FieldCipher, OpaqueReferenceFactory
from zhixu.vault_client import CapabilityGrantIssuer

NOW = datetime(2026, 7, 30, 12, tzinfo=UTC)
SYNTHETIC_CREDENTIAL = "correct horse synthetic staple"
SYNTHETIC_INVALID_CREDENTIAL = "wrong synthetic password"


@dataclass
class AdminParts:
    api: AdminAPI
    database: Database
    reads: AdminReadStore
    sessions: AdminSessionStore
    token: str
    grants: GrantRepository

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@pytest.fixture
def admin(tmp_path: Path) -> AdminParts:
    database = Database(tmp_path / "zhixu.sqlite3")
    assert database.migrate() == [1, 2, 3, 4, 5, 6]
    clock = FrozenClock(NOW)
    grants = GrantRepository(database)
    policy = PolicyEngine(grants.has_grant)
    users = UserRepository(database)
    for user_id in ("user_owner", "user_other"):
        authorization = policy.require(
            CommandContext(actor_user_id=user_id, now=NOW),
            Action.CREATE,
            ResourceRef("user", user_id, user_id),
        )
        users.create(
            User(user_id, f"Synthetic {user_id}", UserStatus.ACTIVE, NOW),
            authorization,
        )
    sessions = AdminSessionStore(database)
    credentials = AdminCredentialStore(database)
    credentials.set_password(
        "user_owner",
        SYNTHETIC_CREDENTIAL,
        now=NOW,
    )
    token = sessions.create(
        user_id="user_owner",
        authentication=AuthenticationStrength.STEP_UP,
        now=NOW,
    )
    reads = AdminReadStore(database)
    api = AdminAPI(
        services=ZhixuServices(
            agenda=AgendaRepository(database),
            tasks=TaskRepository(database),
            notes=NoteRepository(database),
            reminders=ReminderRepository(database),
            policy=policy,
            clock=clock,
        ),
        policy=policy,
        users=users,
        grants=grants,
        sessions=sessions,
        identity_links=IdentityLinkStore(database, challenge_key=b"c" * 32),
        reads=reads,
        clock=clock,
        field_cipher=FieldCipher(b"e" * 32),
        references=OpaqueReferenceFactory(b"r" * 32),
        health=HealthRegistry(
            storage_probe=lambda: True,
            optional_probes={"llm": lambda: False, "vault": lambda: True},
        ),
        credentials=credentials,
        outbox=OutboxStore(database),
    )
    return AdminParts(api, database, reads, sessions, token.value, grants)


def _json(data: dict[str, object]) -> bytes:
    return json.dumps(data).encode()


def _challenge_code(admin: AdminParts, challenge_id: str) -> str:
    with admin.database.connect() as connection:
        row = connection.execute(
            "SELECT payload_json FROM outbox_deliveries WHERE idempotency_key=?",
            (challenge_id,),
        ).fetchone()
    assert row is not None
    text = str(json.loads(str(row["payload_json"]))["text"])
    return text.split("：", 1)[1].split("。", 1)[0]


def test_health_and_admin_status_are_minimal_and_redacted(admin: AdminParts) -> None:
    assert admin.api.dispatch("GET", "/health/live").body == {"status": "live"}
    ready = admin.api.dispatch("GET", "/health/ready")
    assert ready.status == 200
    assert ready.body["degraded"] is True

    denied = admin.api.dispatch("GET", "/admin/status")
    assert denied.status == 403

    status = admin.api.dispatch(
        "GET",
        "/admin/status",
        headers=admin.headers,
    )
    serialized = json.dumps(status.body)
    assert status.status == 200
    assert status.body["health"]["core"] == "ready"
    assert "sqlite3" not in serialized
    assert "127.0.0.1" not in serialized
    assert "enc:" not in serialized


def test_password_login_is_rate_limited_and_session_can_be_revoked(
    admin: AdminParts,
) -> None:
    successful = admin.api.dispatch(
        "POST",
        "/admin/session",
        body=_json(
            {
                "user_id": "user_owner",
                "password": SYNTHETIC_CREDENTIAL,
            }
        ),
    )
    assert successful.status == 201
    access_token = str(successful.body["access_token"])
    assert SYNTHETIC_CREDENTIAL not in json.dumps(successful.body)
    assert admin.sessions.authenticate(access_token, now=NOW) is not None

    revoked = admin.api.dispatch(
        "DELETE",
        "/admin/session",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert revoked.body == {"revoked": True}
    assert admin.sessions.authenticate(access_token, now=NOW) is None

    for _ in range(5):
        denied = admin.api.dispatch(
            "POST",
            "/admin/session",
            body=_json(
                {
                    "user_id": "user_owner",
                    "password": SYNTHETIC_INVALID_CREDENTIAL,
                }
            ),
        )
        assert denied.status == 403
    locked = admin.api.dispatch(
        "POST",
        "/admin/session",
        body=_json(
            {
                "user_id": "user_owner",
                "password": SYNTHETIC_CREDENTIAL,
            }
        ),
    )
    assert locked.status == 403


def test_passkey_ceremony_upgrades_only_the_authenticated_user(
    admin: AdminParts,
) -> None:
    class FakeVaultClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def call(
            self,
            method: str,
            params: dict[str, object],
        ) -> dict[str, object]:
            self.calls.append((method, params))
            if method.endswith("_options"):
                raise AssertionError("unexpected method")
            if method.startswith("passkey_begin"):
                return {"options": {"challenge": "synthetic-challenge"}}
            if method == "passkey_finish_registration":
                return {"credential_id": "synthetic-credential"}
            if method == "passkey_finish_authentication":
                return {
                    "user_id": "user_owner",
                    "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
                }
            raise AssertionError(f"unexpected method {method}")

    fake = FakeVaultClient()
    admin.api.vault_client = fake  # type: ignore[assignment]
    password_session = admin.sessions.create(
        user_id="user_owner",
        authentication=AuthenticationStrength.PASSWORD,
        now=NOW,
    )
    headers = {"Authorization": f"Bearer {password_session.value}"}

    options = admin.api.dispatch(
        "POST",
        "/admin/passkeys/registration/options",
        headers=headers,
    )
    assert options.body == {"publicKey": {"challenge": "synthetic-challenge"}}
    registered = admin.api.dispatch(
        "POST",
        "/admin/passkeys/registration/verify",
        headers=headers,
        body=_json({"credential": {"id": "synthetic-credential"}}),
    )
    assert registered.body == {"registered": True}
    admin.api.dispatch(
        "POST",
        "/admin/passkeys/authentication/options",
        headers=headers,
    )
    verified = admin.api.dispatch(
        "POST",
        "/admin/passkeys/authentication/verify",
        headers=headers,
        body=_json({"credential": {"id": "synthetic-credential"}}),
    )

    assert verified.status == 201
    upgraded = admin.sessions.authenticate(
        str(verified.body["access_token"]),
        now=NOW,
    )
    assert upgraded is not None
    assert upgraded.authentication is AuthenticationStrength.STEP_UP
    assert all(
        call_params.get("user_id") == "user_owner"
        for _method, call_params in fake.calls
    )


def test_vault_admin_requires_step_up_and_never_returns_created_value(
    admin: AdminParts,
) -> None:
    class FakeVaultClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def call(
            self,
            method: str,
            params: dict[str, object],
        ) -> dict[str, object]:
            self.calls.append((method, params))
            if method == "create":
                return {
                    "item": {
                        "id": params["secret_id"],
                        "label": params["label"],
                        "kind": params["kind"],
                        "classification": "l3_human",
                        "version": 1,
                    }
                }
            if method == "reveal":
                return {"value": "synthetic-secret-value"}
            raise AssertionError(f"unexpected method {method}")

    fake = FakeVaultClient()
    admin.api.vault_client = fake  # type: ignore[assignment]
    admin.api.grant_issuer = CapabilityGrantIssuer.generate("zhixu-auth")
    ordinary = admin.sessions.create(
        user_id="user_owner",
        authentication=AuthenticationStrength.PASSWORD,
        now=NOW,
    )
    body = _json(
        {
            "label": "Synthetic secret",
            "kind": "human",
            "value": "synthetic-secret-value",
        }
    )
    denied = admin.api.dispatch(
        "POST",
        "/admin/vault/secrets",
        headers={"Authorization": f"Bearer {ordinary.value}"},
        body=body,
    )
    assert denied.status == 403
    assert fake.calls == []

    created = admin.api.dispatch(
        "POST",
        "/admin/vault/secrets",
        headers=admin.headers,
        body=body,
    )
    assert created.status == 201
    assert "synthetic-secret-value" not in json.dumps(created.body)
    secret_id = str(created.body["id"])
    revealed = admin.api.dispatch(
        "POST",
        f"/admin/vault/secrets/{secret_id}/reveal",
        headers=admin.headers,
        body=b"{}",
    )
    assert revealed.body["value"] == "synthetic-secret-value"
    assert all("grant" in params for _method, params in fake.calls)


def test_identity_otp_is_one_time_encrypted_and_unbind_revokes_session(
    admin: AdminParts,
    tmp_path: Path,
) -> None:
    external_subject = "synthetic-external-actor"
    issued = admin.api.dispatch(
        "POST",
        "/admin/identity-challenges",
        headers=admin.headers,
        body=_json(
            {
                "channel": "qq",
                "channel_account": "account_synthetic",
                "external_subject": external_subject,
            }
        ),
    )
    assert issued.status == 201
    assert external_subject not in json.dumps(issued.body)
    assert "verification_code" not in issued.body
    challenge_id = str(issued.body["challenge_id"])
    code = _challenge_code(admin, challenge_id)

    linked = admin.api.dispatch(
        "POST",
        "/admin/identities",
        headers=admin.headers,
        body=_json(
            {
                "challenge_id": challenge_id,
                "verification_code": code,
            }
        ),
    )
    assert linked.status == 201
    identity_id = str(linked.body["id"])
    replay = admin.api.dispatch(
        "POST",
        "/admin/identities",
        headers=admin.headers,
        body=_json(
            {
                "challenge_id": challenge_id,
                "verification_code": code,
            }
        ),
    )
    assert replay.status == 403

    session_id = "channel_session_synthetic"
    admin.reads.create_channel_session(
        session_id=session_id,
        identity_id=identity_id,
        user_id="user_owner",
        channel="qq",
        channel_account="account_synthetic",
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    assert admin.reads.channel_session_active(session_id, now=NOW)

    note = admin.api.dispatch(
        "POST",
        "/admin/notes",
        headers=admin.headers,
        body=_json({"title": "Synthetic note", "body": "Domain data remains."}),
    )
    assert note.status == 201

    unbound = admin.api.dispatch(
        "DELETE",
        f"/admin/identities/{identity_id}",
        headers={**admin.headers, "X-Zhixu-Confirm": "true"},
    )
    assert unbound.status == 200
    assert not admin.reads.channel_session_active(session_id, now=NOW)
    notes = admin.api.dispatch("GET", "/admin/notes", headers=admin.headers)
    assert len(notes.body) == 1
    assert any(
        event["action"] == "delete" and event["resource_kind"] == "external_identity"
        for event in admin.reads.audit("user_owner")
    )

    database_bytes = b"".join(
        path.read_bytes()
        for path in tmp_path.iterdir()
        if path.name.startswith("zhixu.sqlite3")
    )
    assert external_subject.encode() not in database_bytes


def test_wrong_identity_code_attempts_persist_and_lock_challenge(admin: AdminParts) -> None:
    issued = admin.api.dispatch(
        "POST",
        "/admin/identity-challenges",
        headers=admin.headers,
        body=_json(
            {
                "channel": "email",
                "channel_account": "account_synthetic",
                "external_subject": "synthetic@example.invalid",
            }
        ),
    )
    challenge_id = str(issued.body["challenge_id"])
    correct_code = _challenge_code(admin, challenge_id)
    for _ in range(5):
        response = admin.api.dispatch(
            "POST",
            "/admin/identities",
            headers=admin.headers,
            body=_json(
                {
                    "challenge_id": challenge_id,
                    "verification_code": "00000000",
                }
            ),
        )
        assert response.status == 403
    locked = admin.api.dispatch(
        "POST",
        "/admin/identities",
        headers=admin.headers,
        body=_json(
            {
                "challenge_id": challenge_id,
                "verification_code": correct_code,
            }
        ),
    )
    assert locked.status == 403
    with admin.database.connect() as connection:
        attempts = connection.execute(
            "SELECT attempts FROM identity_link_challenges WHERE id=?",
            (challenge_id,),
        ).fetchone()["attempts"]
    assert attempts == 5


def test_admin_domain_management_and_acl_use_internal_user_ids(
    admin: AdminParts,
) -> None:
    agenda = admin.api.dispatch(
        "POST",
        "/admin/agenda",
        headers=admin.headers,
        body=_json(
            {
                "title": "Synthetic agenda",
                "start_at": (NOW + timedelta(hours=1)).isoformat(),
                "end_at": (NOW + timedelta(hours=2)).isoformat(),
                "timezone": "UTC",
            }
        ),
    )
    task = admin.api.dispatch(
        "POST",
        "/admin/tasks",
        headers=admin.headers,
        body=_json({"title": "Synthetic task", "priority": 2}),
    )
    note = admin.api.dispatch(
        "POST",
        "/admin/notes",
        headers=admin.headers,
        body=_json(
            {
                "title": "Synthetic note",
                "body": "Synthetic body",
                "tags": ["synthetic"],
            }
        ),
    )
    assert (agenda.status, task.status, note.status) == (201, 201, 201)
    note_id = str(note.body["id"])

    granted = admin.api.dispatch(
        "POST",
        "/admin/acl",
        headers=admin.headers,
        body=_json(
            {
                "resource_kind": "note",
                "resource_id": note_id,
                "subject_user_id": "user_other",
                "action": "read",
            }
        ),
    )
    assert granted.status == 201
    assert admin.grants.has_grant(
        "user_other",
        Action.READ,
        ResourceRef("note", note_id, "user_owner"),
    )
    acl = admin.api.dispatch(
        "GET",
        f"/admin/acl?resource_kind=note&resource_id={note_id}",
        headers=admin.headers,
    )
    assert acl.body == [
        {
            "subject_user_id": "user_other",
            "action": "read",
            "created_at": NOW.isoformat(),
        }
    ]

    assert len(admin.api.dispatch("GET", "/admin/agenda", headers=admin.headers).body) == 1
    assert len(admin.api.dispatch("GET", "/admin/tasks", headers=admin.headers).body) == 1
    assert len(admin.api.dispatch("GET", "/admin/notes", headers=admin.headers).body) == 1
