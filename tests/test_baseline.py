import base64
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from zhixu import __version__
from zhixu.adapters.storage.sqlite import Database
from zhixu.channels import (
    ChannelCapabilities,
    ConversationKind,
    InboundEvent,
    MessageKind,
)
from zhixu.cli import main
from zhixu.domain import DataClassification, SecretKind


def test_cli_has_no_side_effects_by_default() -> None:
    assert main([]) == 0


def test_cli_generates_matching_grant_key_pair(
    tmp_path: Path,
) -> None:
    private_path = tmp_path / "issuer.private"
    public_path = tmp_path / "issuer.public.pem"
    arguments = [
        "generate-grant-key",
        "--private-output",
        str(private_path),
        "--public-output",
        str(public_path),
    ]
    assert main(arguments) == 0
    private_key = Ed25519PrivateKey.from_private_bytes(
        base64.urlsafe_b64decode(private_path.read_bytes()),
    )
    public_key = serialization.load_pem_public_key(public_path.read_bytes())
    assert private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    ) == public_key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def _observe_private_route(database: Database, opaque_ref: str) -> None:
    observed_at = datetime.now(UTC).isoformat()
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO channel_routes(
                channel,channel_account,opaque_ref,route_kind,
                commands_enabled,last_seen_at
            ) VALUES('qq','qq-synthetic',?,'private',0,?)
            """,
            (opaque_ref, observed_at),
        )
        connection.execute(
            """
            INSERT INTO inbound_event_receipts(
                channel,channel_account,event_id_hash,message_hash,
                actor_ref,conversation_ref,intent_kind,outcome,received_at
            ) VALUES('qq','qq-synthetic',?,? ,?,?,'','identity_unbound',?)
            """,
            (
                f"event-{opaque_ref}",
                f"message-{opaque_ref}",
                opaque_ref,
                opaque_ref,
                observed_at,
            ),
        )


def test_headless_qq_bootstrap_binds_one_recent_opaque_route_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "application.sqlite3"
    database = Database(database_path)
    database.migrate()
    _observe_private_route(database, "opaque-synthetic")
    key_path = tmp_path / "field-key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"K" * 32))
    key_path.chmod(0o600)
    arguments = [
        "bootstrap-qq-owner",
        "--database",
        str(database_path),
        "--field-key-file",
        str(key_path),
        "--user-id",
        "owner_synthetic",
        "--display-name",
        "Synthetic Owner",
    ]

    assert main(arguments) == 0
    assert "opaque-synthetic" not in capsys.readouterr().out
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM users WHERE id NOT LIKE 'service:%'"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM users WHERE id='service:registration'"
        ).fetchone()[0] == 1
        identity = connection.execute(
            "SELECT opaque_ref,external_subject_enc FROM external_identities"
        ).fetchone()
    assert identity["opaque_ref"] == "opaque-synthetic"
    assert identity["external_subject_enc"] != "opaque-synthetic"
    with pytest.raises(PermissionError, match="already closed"):
        main(arguments)


def test_headless_qq_bootstrap_rejects_ambiguous_recent_routes(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "application.sqlite3"
    database = Database(database_path)
    database.migrate()
    _observe_private_route(database, "opaque-first")
    _observe_private_route(database, "opaque-second")
    key_path = tmp_path / "field-key"
    key_path.write_bytes(base64.urlsafe_b64encode(b"K" * 32))

    with pytest.raises(PermissionError, match="exactly one"):
        main(
            [
                "bootstrap-qq-owner",
                "--database",
                str(database_path),
                "--field-key-file",
                str(key_path),
            ]
        )


def test_public_version_is_development_version() -> None:
    assert __version__ == "0.1.0.dev0"


def test_public_issue_template_blocks_private_diagnostics_and_no_actions_exist() -> None:
    root = Path(__file__).resolve().parents[1]
    template = (
        root / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml"
    ).read_text(encoding="utf-8")
    for warning in (
        "tokens",
        "raw logs",
        "databases",
        "server addresses",
        "personal data",
    ):
        assert warning in template
    assert not (root / ".github" / "workflows").exists()


def test_classifications_are_ordered_fail_closed() -> None:
    assert DataClassification.PROHIBITED > DataClassification.SECRET
    assert DataClassification.SECRET > DataClassification.CONFIDENTIAL
    assert SecretKind.MACHINE != SecretKind.HUMAN


def test_normalized_event_does_not_assume_qq() -> None:
    event = InboundEvent(
        event_id="evt_test_001",
        channel="test",
        channel_account="account_test",
        external_actor_ref="actor_opaque",
        external_conversation_ref="conversation_opaque",
        conversation_kind=ConversationKind.PRIVATE,
        message_kind=MessageKind.TEXT,
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        text="synthetic message",
    )
    capabilities = ChannelCapabilities(inbound_text=True, outbound_text=True)

    assert event.channel == "test"
    assert capabilities.inbound_text is True
    assert capabilities.proactive_push is False
