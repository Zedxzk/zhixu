from datetime import UTC, datetime

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
