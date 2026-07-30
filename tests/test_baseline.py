import base64
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from zhixu import __version__
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


def test_public_version_is_development_version() -> None:
    assert __version__ == "0.1.0.dev0"


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
