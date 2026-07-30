from datetime import UTC, datetime

import pytest

from zhixu.domain import (
    Action,
    AuthenticationStrength,
    CommandContext,
    DataClassification,
    PolicyEngine,
    RequestChannel,
    ResourceRef,
)
from zhixu.domain.errors import ConfirmationRequired, PermissionDenied

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def context(
    *,
    actor: str = "user_owner",
    authentication: AuthenticationStrength = AuthenticationStrength.CHANNEL,
    channel: RequestChannel = RequestChannel.PRIVATE_CHAT,
    confirmed: bool = False,
) -> CommandContext:
    return CommandContext(
        actor_user_id=actor,
        authentication=authentication,
        request_channel=channel,
        confirmed=confirmed,
        now=NOW,
    )


def test_owner_can_read_personal_data_but_other_user_cannot() -> None:
    policy = PolicyEngine()
    resource = ResourceRef(
        "note",
        "note_test",
        "user_owner",
        DataClassification.PERSONAL,
    )

    assert policy.require(context(), Action.READ, resource).actor_user_id == "user_owner"
    with pytest.raises(PermissionDenied):
        policy.require(context(actor="user_other"), Action.READ, resource)


def test_confidential_data_requires_step_up_and_never_enters_group_chat() -> None:
    policy = PolicyEngine()
    resource = ResourceRef(
        "note",
        "note_confidential",
        "user_owner",
        DataClassification.CONFIDENTIAL,
    )

    with pytest.raises(PermissionDenied):
        policy.require(context(), Action.READ, resource)
    with pytest.raises(PermissionDenied):
        policy.require(
            context(
                authentication=AuthenticationStrength.STEP_UP,
                channel=RequestChannel.GROUP_CHAT,
            ),
            Action.READ,
            resource,
        )
    assert (
        policy.require(
            context(
                authentication=AuthenticationStrength.STEP_UP,
                channel=RequestChannel.ADMIN_WEB,
            ),
            Action.READ,
            resource,
        ).resource
        == resource
    )


def test_delete_requires_explicit_confirmation() -> None:
    policy = PolicyEngine()
    resource = ResourceRef(
        "agenda",
        "agenda_test",
        "user_owner",
        DataClassification.PERSONAL,
    )

    with pytest.raises(ConfirmationRequired):
        policy.require(context(), Action.DELETE, resource)
    assert policy.require(context(confirmed=True), Action.DELETE, resource).action is Action.DELETE
