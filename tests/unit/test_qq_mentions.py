from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from zhixu.adapters.channels.qq.contacts import QQContactStore
from zhixu.adapters.channels.qq.gateway import QQEventMapper

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


class _Contacts:
    def record(
        self,
        *,
        channel_account: str,
        kind: str,
        external_identifier: str,
        now: datetime,
    ) -> str:
        del channel_account, external_identifier, now
        return f"opaque-{kind}"

    def record_reply_context(
        self,
        *,
        channel_account: str,
        target_ref: str,
        external_context: str,
        context_kind: str,
        now: datetime,
    ) -> str:
        del channel_account, target_ref, external_context, context_kind, now
        return "opaque-reply-context"


def _mapper(*, display_names: tuple[str, ...] = ()) -> QQEventMapper:
    return QQEventMapper(
        "logical-account",
        cast(QQContactStore, _Contacts()),
        bot_identifier="application-id",
        display_names=display_names,
    )


def _group_payload(*, content: str, mention: dict[str, object]) -> dict[str, object]:
    return {
        "id": "synthetic-event",
        "group_openid": "synthetic-group",
        "content": content,
        "author": {"member_openid": "synthetic-member"},
        "mentions": [mention],
    }


def test_bot_mention_uses_display_identity_when_is_you_is_false() -> None:
    event = _mapper(display_names=("SyntheticBot",)).map(
        "GROUP_MESSAGE_CREATE",
        _group_payload(
            content="<@per-group-bot-id> 今天几号",
            mention={
                "bot": True,
                "is_you": False,
                "id": "per-group-bot-id",
                "member_openid": "per-group-bot-id",
                "username": "SyntheticBot",
            },
        ),
        received_at=NOW,
    )

    assert event is not None
    assert event.metadata["mentioned"] is True
    assert event.text == "今天几号"


def test_bot_mention_survives_when_qq_strips_marker_from_content() -> None:
    event = _mapper(display_names=("SyntheticBot",)).map(
        "GROUP_MESSAGE_CREATE",
        _group_payload(
            content="今天几号",
            mention={
                "bot": "true",
                "is_you": "false",
                "member_openid": "per-group-bot-id",
                "username": "SyntheticBot",
            },
        ),
        received_at=NOW,
    )

    assert event is not None
    assert event.metadata["mentioned"] is True
    assert event.text == "今天几号"


def test_mention_of_another_bot_is_not_addressed_to_this_bot() -> None:
    event = _mapper(display_names=("SyntheticBot",)).map(
        "GROUP_MESSAGE_CREATE",
        _group_payload(
            content="<@other-bot-id> 今天几号",
            mention={
                "bot": True,
                "is_you": False,
                "id": "other-bot-id",
                "username": "OtherBot",
            },
        ),
        received_at=NOW,
    )

    assert event is not None
    assert event.metadata["mentioned"] is False
    assert event.text == "<@other-bot-id> 今天几号"
