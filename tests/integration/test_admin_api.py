from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zhixu.adapters.channels import (
    ChannelRegistry,
    OutboundTargetStore,
    RegisteredChannel,
)
from zhixu.adapters.storage.sqlite import (
    AdminCredentialStore,
    AdminReadStore,
    AdminSessionStore,
    AgendaRepository,
    ChannelRouteStore,
    Database,
    GrantRepository,
    IdentityLinkStore,
    NoteRepository,
    ReminderRepository,
    TaskRepository,
    UserRepository,
)
from zhixu.adapters.web import AdminAPI, AdminResponse, HealthRegistry
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
from zhixu.runtime.api import CompositePrivateAPI
from zhixu.security import FieldCipher, OpaqueReferenceFactory
from zhixu.vault_client import CapabilityGrantIssuer

NOW = datetime(2026, 7, 30, 12, tzinfo=UTC)
SYNTHETIC_CREDENTIAL = "correct horse synthetic staple"
SYNTHETIC_INVALID_CREDENTIAL = "wrong synthetic password"


@dataclass
class AdminParts:
    api: AdminAPI
    database: Database
    outbound_database: Database
    outbound_targets: OutboundTargetStore
    reads: AdminReadStore
    sessions: AdminSessionStore
    token: str
    grants: GrantRepository
    routes: ChannelRouteStore

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@pytest.fixture
def admin(tmp_path: Path) -> AdminParts:
    database = Database(tmp_path / "zhixu.sqlite3")
    assert database.migrate() == [1, 2, 3, 4, 5, 6, 7, 8, 9]
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
    outbound_database = Database(tmp_path / "outbound-targets.sqlite3")
    assert outbound_database.migrate() == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    outbound_targets = OutboundTargetStore(
        outbound_database,
        FieldCipher(b"o" * 32),
        OpaqueReferenceFactory(b"r" * 32),
    )
    routes = ChannelRouteStore(database)
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
        channel_routes=routes,
        outbox=OutboxStore(database),
        channels=ChannelRegistry(
            declared=(
                RegisteredChannel(
                    "qq",
                    "account_synthetic",
                    "conversational",
                    {
                        "inbound_text": True,
                        "outbound_text": True,
                        "proactive_push": True,
                    },
                ),
                RegisteredChannel(
                    "email",
                    "account_synthetic",
                    "outbound-only",
                    {
                        "inbound_text": False,
                        "outbound_text": True,
                        "proactive_push": True,
                    },
                ),
            ),
        ),
        outbound_targets=outbound_targets,
        outbound_target_kinds={
            ("email", "account_synthetic"): "recipient",
        },
    )
    return AdminParts(
        api,
        database,
        outbound_database,
        outbound_targets,
        reads,
        sessions,
        token.value,
        grants,
        routes,
    )


def test_headless_composite_exposes_health_and_internal_routes_only(
    admin: AdminParts,
) -> None:
    class InternalStub:
        def dispatch(
            self,
            method: str,
            target: str,
            *,
            headers: dict[str, str],
            body: bytes,
        ) -> AdminResponse:
            assert method == "POST"
            assert target == "/internal/synthetic"
            assert headers == {"authorization": "Bearer synthetic"}
            assert body == b"{}"
            return AdminResponse(202, {"accepted": True})

    composite = CompositePrivateAPI(
        admin.api,
        InternalStub(),  # type: ignore[arg-type]
        admin_enabled=False,
    )
    assert composite.dispatch("GET", "/health/live").status == 200
    assert composite.dispatch("GET", "/admin/status").status == 404
    internal = composite.dispatch(
        "POST",
        "/internal/synthetic",
        headers={"Authorization": "Bearer synthetic"},
        body=b"{}",
    )
    assert internal.status == 202


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


def _bind_private_qq_target(admin: AdminParts, opaque_ref: str) -> str:
    admin.routes.observe(
        channel="qq",
        channel_account="account_synthetic",
        opaque_ref=opaque_ref,
        kind="private",
        now=NOW,
    )
    issued = admin.api.dispatch(
        "POST",
        "/admin/identity-challenges",
        headers=admin.headers,
        body=_json(
            {
                "channel": "qq",
                "channel_account": "account_synthetic",
                "opaque_ref": opaque_ref,
            }
        ),
    )
    assert issued.status == 201
    challenge_id = str(issued.body["challenge_id"])
    linked = admin.api.dispatch(
        "POST",
        "/admin/identities",
        headers=admin.headers,
        body=_json(
            {
                "challenge_id": challenge_id,
                "verification_code": _challenge_code(admin, challenge_id),
            }
        ),
    )
    assert linked.status == 201
    return str(linked.body["id"])


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
    llm_usage = admin.api.dispatch(
        "GET",
        "/admin/llm-usage",
        headers=admin.headers,
    )
    assert llm_usage.status == 200
    assert llm_usage.body == []


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
                classification = (
                    "l4_human_override"
                    if params.get("classification") == "l4_prohibited"
                    else "l3_human"
                )
                return {
                    "item": {
                        "id": params["secret_id"],
                        "label": params["label"],
                        "kind": params["kind"],
                        "classification": classification,
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

    l4_body = _json(
        {
            "label": "Synthetic L4 owner override",
            "kind": "human",
            "classification": "l4_prohibited",
            "policy_override": "owner_explicit_human_storage",
            "value": "synthetic-l4-value",
        }
    )
    calls_before = len(fake.calls)
    unconfirmed = admin.api.dispatch(
        "POST",
        "/admin/vault/secrets",
        headers=admin.headers,
        body=l4_body,
    )
    assert unconfirmed.status == 428
    assert len(fake.calls) == calls_before

    confirmed_headers = {**admin.headers, "X-Zhixu-Confirm": "true"}
    overridden = admin.api.dispatch(
        "POST",
        "/admin/vault/secrets",
        headers=confirmed_headers,
        body=l4_body,
    )
    assert overridden.status == 201
    assert overridden.body["classification"] == "l4_human_override"
    assert "synthetic-l4-value" not in json.dumps(overridden.body)
    override_call = fake.calls[-1]
    assert override_call[0] == "create"
    assert override_call[1]["classification"] == "l4_prohibited"
    assert (
        override_call[1]["policy_override"]
        == "owner_explicit_human_storage"
    )


def test_identity_otp_is_one_time_encrypted_and_unbind_revokes_session(
    admin: AdminParts,
    tmp_path: Path,
) -> None:
    opaque_ref = "qqc_synthetic_observed_private"
    admin.routes.observe(
        channel="qq",
        channel_account="account_synthetic",
        opaque_ref=opaque_ref,
        kind="private",
        now=NOW,
    )
    issued = admin.api.dispatch(
        "POST",
        "/admin/identity-challenges",
        headers=admin.headers,
        body=_json(
            {
                "channel": "qq",
                "channel_account": "account_synthetic",
                "opaque_ref": opaque_ref,
            }
        ),
    )
    assert issued.status == 201
    assert issued.body["opaque_ref"] == opaque_ref
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
    with admin.database.connect() as connection:
        encrypted_subject = str(
            connection.execute(
                "SELECT external_subject_enc FROM external_identities WHERE id=?",
                (identity_id,),
            ).fetchone()["external_subject_enc"]
        )
    assert (
        FieldCipher(b"e" * 32).decrypt(
            encrypted_subject,
            context=(
                "external-identity:qq:account_synthetic:"
                "qqc_synthetic_observed_private"
            ),
        )
        == opaque_ref
    )
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
    assert opaque_ref.encode() in database_bytes


def test_qq_identity_challenge_rejects_raw_or_unobserved_routes(
    admin: AdminParts,
) -> None:
    raw = admin.api.dispatch(
        "POST",
        "/admin/identity-challenges",
        headers=admin.headers,
        body=_json(
            {
                "channel": "qq",
                "channel_account": "account_synthetic",
                "external_subject": "raw-openid-must-not-cross-boundary",
            }
        ),
    )
    assert raw.status == 422
    unobserved = admin.api.dispatch(
        "POST",
        "/admin/identity-challenges",
        headers=admin.headers,
        body=_json(
            {
                "channel": "qq",
                "channel_account": "account_synthetic",
                "opaque_ref": "qqc_unobserved",
            }
        ),
    )
    assert unobserved.status == 422
    admin.routes.observe(
        channel="qq",
        channel_account="account_synthetic",
        opaque_ref="qqc_group_actor",
        kind="actor",
        now=NOW,
    )
    group_actor = admin.api.dispatch(
        "POST",
        "/admin/identity-challenges",
        headers=admin.headers,
        body=_json(
            {
                "channel": "qq",
                "channel_account": "account_synthetic",
                "opaque_ref": "qqc_group_actor",
            }
        ),
    )
    assert group_actor.status == 422


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
    assert issued.status == 201
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


def test_outbound_identity_target_is_encrypted_in_an_isolated_database(
    admin: AdminParts,
    tmp_path: Path,
) -> None:
    recipient = "identity-recipient@example.invalid"
    issued = admin.api.dispatch(
        "POST",
        "/admin/identity-challenges",
        headers=admin.headers,
        body=_json(
            {
                "channel": "email",
                "channel_account": "account_synthetic",
                "external_subject": recipient,
            }
        ),
    )
    assert issued.status == 201
    opaque_ref = str(issued.body["opaque_ref"])
    resolved = admin.outbound_targets.resolve(
        channel="email",
        channel_account="account_synthetic",
        opaque_ref=opaque_ref,
    )
    assert resolved.kind == "recipient"
    assert resolved.value == recipient

    with admin.database.connect() as connection:
        delivery = connection.execute(
            """
            SELECT target_ref,payload_json
            FROM outbox_deliveries
            WHERE idempotency_key=?
            """,
            (str(issued.body["challenge_id"]),),
        ).fetchone()
    assert delivery is not None
    assert str(delivery["target_ref"]) == opaque_ref
    assert recipient not in str(delivery["payload_json"])

    database_bytes = b"".join(
        path.read_bytes()
        for path in tmp_path.iterdir()
        if path.name.startswith(("zhixu.sqlite3", "outbound-targets.sqlite3"))
    )
    assert recipient.encode() not in database_bytes


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


def test_admin_manages_recurrence_exceptions_and_note_attachment_metadata(
    admin: AdminParts,
) -> None:
    start = NOW + timedelta(days=1)
    agenda = admin.api.dispatch(
        "POST",
        "/admin/agenda",
        headers=admin.headers,
        body=_json(
            {
                "title": "Synthetic recurring agenda",
                "start_at": start.isoformat(),
                "end_at": (start + timedelta(hours=1)).isoformat(),
                "timezone": "UTC",
                "recurrence_rule": "FREQ=DAILY;COUNT=3",
            }
        ),
    )
    assert agenda.status == 201
    agenda_id = str(agenda.body["id"])
    cancelled_at = start + timedelta(days=1)
    exception = admin.api.dispatch(
        "POST",
        f"/admin/agenda/{agenda_id}/exceptions",
        headers=admin.headers,
        body=_json(
            {
                "occurrence_at": cancelled_at.isoformat(),
                "action": "cancel",
            }
        ),
    )
    assert exception.status == 201
    occurrences = AgendaRepository(admin.database).occurrences(
        "user_owner",
        start - timedelta(minutes=1),
        start + timedelta(days=4),
    )
    assert [item.start_at for item in occurrences] == [
        start,
        start + timedelta(days=2),
    ]

    note = admin.api.dispatch(
        "POST",
        "/admin/notes",
        headers=admin.headers,
        body=_json(
            {
                "title": "Synthetic attachment note",
                "body": "Metadata only.",
                "attachments": [
                    {
                        "id": "attachment_synthetic",
                        "filename": "synthetic.pdf",
                        "media_type": "application/pdf",
                        "size_bytes": 2048,
                        "content_ref": "attachment_ref_synthetic",
                    }
                ],
            }
        ),
    )
    assert note.status == 201
    assert note.body["attachments"] == [
        {
            "id": "attachment_synthetic",
            "filename": "synthetic.pdf",
            "media_type": "application/pdf",
            "size_bytes": 2048,
            "content_ref": "attachment_ref_synthetic",
        }
    ]
    stored = NoteRepository(admin.database).get(str(note.body["id"]))
    assert stored is not None
    assert stored.attachments[0].content_ref == "attachment_ref_synthetic"

    rejected_binary = admin.api.dispatch(
        "POST",
        "/admin/notes",
        headers=admin.headers,
        body=_json(
            {
                "title": "Synthetic rejected note",
                "body": "No binary payloads.",
                "attachments": [
                    {
                        "id": "attachment_rejected",
                        "filename": "synthetic.bin",
                        "media_type": "application/octet-stream",
                        "size_bytes": 4,
                        "content_ref": "attachment_ref_rejected",
                        "content": "AAAA",
                    }
                ],
            }
        ),
    )
    assert rejected_binary.status == 422


def test_admin_reminders_require_owned_target_and_confirmed_cancellation(
    admin: AdminParts,
) -> None:
    target_ref = "qqc_synthetic_reminder_target"
    _bind_private_qq_target(admin, target_ref)

    unbound = admin.api.dispatch(
        "POST",
        "/admin/reminders",
        headers=admin.headers,
        body=_json(
            {
                "title": "Synthetic rejected reminder",
                "fire_at": (NOW + timedelta(hours=1)).isoformat(),
                "target_ref": "qqc_synthetic_not_bound",
            }
        ),
    )
    assert unbound.status == 403

    created = admin.api.dispatch(
        "POST",
        "/admin/reminders",
        headers=admin.headers,
        body=_json(
            {
                "title": "Synthetic managed reminder",
                "fire_at": (NOW + timedelta(hours=2)).isoformat(),
                "target_ref": target_ref,
                "missed_policy": "skip",
                "related_kind": "task",
                "related_id": "task_synthetic_related",
            }
        ),
    )
    assert created.status == 201
    assert created.body["status"] == "pending"
    assert created.body["missed_policy"] == "skip"
    reminder_id = str(created.body["id"])

    listed = admin.api.dispatch(
        "GET",
        "/admin/reminders",
        headers=admin.headers,
    )
    assert listed.status == 200
    assert [item["id"] for item in listed.body] == [reminder_id]

    confirmation_required = admin.api.dispatch(
        "DELETE",
        f"/admin/reminders/{reminder_id}",
        headers=admin.headers,
    )
    assert confirmation_required.status == 428

    cancelled = admin.api.dispatch(
        "DELETE",
        f"/admin/reminders/{reminder_id}",
        headers={**admin.headers, "X-Zhixu-Confirm": "true"},
    )
    assert cancelled.status == 200
    assert cancelled.body["status"] == "cancelled"

    listed_after = admin.api.dispatch(
        "GET",
        "/admin/reminders",
        headers=admin.headers,
    )
    assert listed_after.body[0]["status"] == "cancelled"
