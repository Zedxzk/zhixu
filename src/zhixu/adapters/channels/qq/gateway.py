"""QQ Gateway state machine with heartbeat, resume and bounded reconnect."""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from zhixu.adapters.storage.sqlite.database import Database
from zhixu.channels import ConversationKind, InboundEvent, MessageKind
from zhixu.security import FieldCipher

from .contacts import QQContactStore
from .http import API_BASE, QQHttpAdapter

logger = logging.getLogger(__name__)

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

GUILDS = 1 << 0
GUILD_MEMBERS = 1 << 1
PUBLIC_GUILD_MESSAGES = 1 << 30
DIRECT_MESSAGE = 1 << 12
GROUP_AND_C2C = 1 << 25
INTERACTION = 1 << 26
FULL_INTENTS = (
    GUILDS
    | GUILD_MEMBERS
    | PUBLIC_GUILD_MESSAGES
    | DIRECT_MESSAGE
    | GROUP_AND_C2C
    | INTERACTION
)

# Transport-level keepalive. Without it a half-open connection is only noticed
# through the QQ application heartbeat, which reacts far more slowly.
PING_INTERVAL_SECONDS = 20.0
PING_TIMEOUT_SECONDS = 20.0

# An unacknowledged heartbeat must fail fast. Checking only when the next
# heartbeat falls due leaves a blind window of up to two heartbeat intervals,
# during which the platform keeps buffering inbound events.
HEARTBEAT_ACK_TIMEOUT_SECONDS = 10.0


@dataclass(slots=True)
class QQGatewayState:
    session_id: str = field(default="", repr=False)
    sequence: int | None = None
    resume_url: str = field(default="", repr=False)
    heartbeat_acknowledged: bool = True


class QQGatewaySessionStore:
    def __init__(self, database: Database, cipher: FieldCipher) -> None:
        self.database = database
        self.cipher = cipher

    def load(self, channel_account: str) -> QQGatewayState:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_sessions WHERE channel_account=?",
                (channel_account,),
            ).fetchone()
        if row is None:
            return QQGatewayState()
        context = f"qq-gateway:{channel_account}"
        return QQGatewayState(
            session_id=self.cipher.decrypt(
                str(row["session_id_enc"]),
                context=f"{context}:session",
            ),
            sequence=int(row["sequence"]) if row["sequence"] is not None else None,
            resume_url=self.cipher.decrypt(
                str(row["resume_url_enc"]),
                context=f"{context}:resume",
            ),
        )

    def save(
        self,
        channel_account: str,
        state: QQGatewayState,
        *,
        now: datetime,
    ) -> None:
        context = f"qq-gateway:{channel_account}"
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO gateway_sessions(
                    channel_account,session_id_enc,resume_url_enc,sequence,updated_at
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(channel_account) DO UPDATE SET
                    session_id_enc=excluded.session_id_enc,
                    resume_url_enc=excluded.resume_url_enc,
                    sequence=excluded.sequence,updated_at=excluded.updated_at
                """,
                (
                    channel_account,
                    self.cipher.encrypt(
                        state.session_id,
                        context=f"{context}:session",
                    ),
                    self.cipher.encrypt(
                        state.resume_url,
                        context=f"{context}:resume",
                    ),
                    state.sequence,
                    now.astimezone(UTC).isoformat(),
                ),
            )

    def clear(self, channel_account: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM gateway_sessions WHERE channel_account=?",
                (channel_account,),
            )


class QQEventMapper:
    def __init__(
        self,
        channel_account: str,
        contacts: QQContactStore,
        *,
        bot_identifier: str | None = None,
        display_names: Sequence[str] = (),
    ) -> None:
        self.channel_account = channel_account
        self.contacts = contacts
        self.bot_identifier = (bot_identifier or channel_account).strip()
        if not self.bot_identifier:
            raise ValueError("QQ bot mention identifier is required")
        self.display_names = tuple(
            name.strip() for name in display_names if name and name.strip()
        )
        self._display_mention = (
            re.compile(
                r"^\s*@(?:"
                + "|".join(re.escape(name) for name in self.display_names)
                + r")(?=\s|$)\s*"
            )
            if self.display_names
            else None
        )

    def map(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        received_at: datetime,
    ) -> InboundEvent | None:
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        event_id = str(data.get("id") or "")
        interaction = (
            data.get("data")
            if event_type == "INTERACTION_CREATE" and isinstance(data.get("data"), dict)
            else {}
        )
        resolved = (
            interaction.get("resolved")
            if isinstance(interaction.get("resolved"), dict)
            else {}
        )
        if event_type == "INTERACTION_CREATE":
            try:
                interaction_type = int(
                    data.get("type") or interaction.get("type") or 0
                )
            except (TypeError, ValueError):
                return None
            if interaction_type != 11:
                return None
        text = str(
            resolved.get("button_data")
            or resolved.get("button_id")
            or data.get("content")
            or ""
        ).strip()
        # QQ marks the addressed bot in a "mentions" entry carrying is_you, and
        # writes that mention into the content keyed by the bot's per-group
        # openid rather than its application id. Both are needed: the entry is
        # what proves the message is addressed, the identifiers are what strip
        # the marker back out of the text.
        mentions = data.get("mentions")
        mention_entries = (
            tuple(mention for mention in mentions if isinstance(mention, dict))
            if isinstance(mentions, list)
            else ()
        )
        addressed_by_mentions = False
        mention_identifiers = {self.bot_identifier}
        bot_mention_entries = 0
        self_mention_entries = 0
        for mention in mention_entries:
            identifiers = {
                str(mention.get(key) or "").strip()
                for key in ("id", "member_openid", "user_openid")
            } - {""}
            entry_marker = bool(
                identifiers
                and re.match(
                    r"^\s*<@!?(?:"
                    + "|".join(re.escape(value) for value in sorted(identifiers))
                    + r")>\s*",
                    text,
                )
            )
            entry_name = str(mention.get("username") or "").strip()
            entry_display_match = bool(
                entry_name and entry_name in self.display_names
            )
            entry_is_bot = _provider_flag(mention.get("bot"))
            entry_is_self = _provider_flag(mention.get("is_you"))
            bot_mention_entries += int(entry_is_bot)
            self_mention_entries += int(entry_is_self)
            # In current QQ group deliveries `is_you` is not reliable across
            # every event variant. A bot mention is still attributable to this
            # receiver when either its content marker or configured display
            # name agrees with the mention entry. Human mentions never satisfy
            # this branch.
            if entry_is_self or (
                entry_is_bot
                and (
                    entry_display_match
                    or (entry_marker and not self.display_names)
                )
            ):
                addressed_by_mentions = True
                mention_identifiers.update(identifiers)
        bot_mention_pattern = (
            r"^\s*<@!?(?:"
            + "|".join(
                re.escape(value) for value in sorted(mention_identifiers) if value
            )
            + r")>\s*"
        )
        bot_mentioned_in_content = bool(re.match(bot_mention_pattern, text))
        text = re.sub(
            bot_mention_pattern,
            "",
            text,
            count=1,
        ).strip()
        group_event = event_type in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}
        # Typing the bot's name by hand produces no mentions entry and no
        # marker, only the display name as ordinary text. Matching the
        # configured name is what separates that from a member mentioning
        # another member, which must stay unaddressed.
        addressed_by_display_name = bool(
            group_event
            and self._display_mention is not None
            and self._display_mention.match(text)
        )
        # A slash command behind any display-name mention is unambiguous intent,
        # so it still works when no display name has been configured.
        display_mentioned_command = bool(
            group_event and re.match(r"^\s*@\S+\s+/\S", text)
        )
        if addressed_by_display_name and self._display_mention is not None:
            text = self._display_mention.sub("", text, count=1).strip()
        elif event_type in {"GROUP_AT_MESSAGE_CREATE", "AT_MESSAGE_CREATE"} or (
            display_mentioned_command
        ):
            text = re.sub(r"^\s*@\S+\s+", "", text, count=1).strip()
        if group_event:
            # Which mention form a group message arrives in is not observable
            # from the receipt, and getting it wrong makes the bot look mute.
            # Only the form is recorded; the message body never is.
            logger.info(
                "qq_group_event type=%s mention_entries=%d bot_entries=%d "
                "self_entries=%d addressed=%s marker=%s display=%s command=%s",
                event_type,
                len(mention_entries),
                bot_mention_entries,
                self_mention_entries,
                addressed_by_mentions,
                bot_mentioned_in_content,
                addressed_by_display_name,
                display_mentioned_command,
            )
        if not event_id or not text:
            return None
        if event_type == "INTERACTION_CREATE":
            group = str(data.get("group_openid") or "")
            channel = str(data.get("channel_id") or "")
            actor = str(
                data.get("group_member_openid")
                or data.get("user_openid")
                or data.get("member_openid")
                or data.get("user_id")
                or resolved.get("user_id")
                or ""
            )
            if not actor:
                return None
            if group:
                conversation_kind = ConversationKind.GROUP
                actor_ref = self.contacts.record(
                    channel_account=self.channel_account,
                    kind="actor",
                    external_identifier=actor,
                    now=received_at,
                )
                conversation_ref = self.contacts.record(
                    channel_account=self.channel_account,
                    kind="group",
                    external_identifier=group,
                    now=received_at,
                )
            elif channel:
                conversation_kind = ConversationKind.CHANNEL
                actor_ref = self.contacts.record(
                    channel_account=self.channel_account,
                    kind="actor",
                    external_identifier=actor,
                    now=received_at,
                )
                conversation_ref = self.contacts.record(
                    channel_account=self.channel_account,
                    kind="channel",
                    external_identifier=channel,
                    now=received_at,
                )
            else:
                conversation_kind = ConversationKind.PRIVATE
                actor_ref = self.contacts.record(
                    channel_account=self.channel_account,
                    kind="private",
                    external_identifier=actor,
                    now=received_at,
                )
                conversation_ref = actor_ref
            mentioned = True
        elif event_type == "C2C_MESSAGE_CREATE":
            actor = str(author.get("user_openid") or data.get("user_openid") or "")
            if not actor:
                return None
            conversation_kind = ConversationKind.PRIVATE
            actor_ref = self.contacts.record(
                channel_account=self.channel_account,
                kind="private",
                external_identifier=actor,
                now=received_at,
            )
            conversation_ref = actor_ref
            mentioned = False
        elif event_type in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}:
            actor = str(author.get("member_openid") or "")
            conversation = str(data.get("group_openid") or "")
            if not actor or not conversation:
                return None
            conversation_kind = ConversationKind.GROUP
            actor_ref = self.contacts.record(
                channel_account=self.channel_account,
                kind="actor",
                external_identifier=actor,
                now=received_at,
            )
            conversation_ref = self.contacts.record(
                channel_account=self.channel_account,
                kind="group",
                external_identifier=conversation,
                now=received_at,
            )
            mentioned = (
                event_type == "GROUP_AT_MESSAGE_CREATE"
                or addressed_by_mentions
                or bot_mentioned_in_content
                or addressed_by_display_name
                or display_mentioned_command
            )
        elif event_type in {"AT_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"}:
            actor = str(author.get("id") or "")
            conversation = str(data.get("channel_id") or "")
            if not actor or not conversation:
                return None
            conversation_kind = ConversationKind.CHANNEL
            actor_ref = self.contacts.record(
                channel_account=self.channel_account,
                kind="actor",
                external_identifier=actor,
                now=received_at,
            )
            conversation_ref = self.contacts.record(
                channel_account=self.channel_account,
                kind="channel",
                external_identifier=conversation,
                now=received_at,
            )
            mentioned = event_type == "AT_MESSAGE_CREATE"
        else:
            return None
        reply_context_ref = self.contacts.record_reply_context(
            channel_account=self.channel_account,
            target_ref=conversation_ref,
            external_context=event_id,
            context_kind=(
                "event_id" if event_type == "INTERACTION_CREATE" else "msg_id"
            ),
            now=received_at,
        )
        return InboundEvent(
            event_id=event_id,
            channel="qq",
            channel_account=self.channel_account,
            external_actor_ref=actor_ref,
            external_conversation_ref=conversation_ref,
            conversation_kind=conversation_kind,
            message_kind=(
                MessageKind.BUTTON
                if event_type == "INTERACTION_CREATE"
                else MessageKind.TEXT
            ),
            received_at=received_at,
            text=text,
            metadata={
                "mentioned": mentioned,
                "reply_context_ref": reply_context_ref,
            },
        )


def _provider_flag(value: object) -> bool:
    """Accept provider booleans without treating arbitrary strings as true."""

    return value is True or value == 1 or (
        isinstance(value, str) and value.strip().lower() in {"1", "true"}
    )


class QQGatewayProtocol:
    def __init__(
        self,
        *,
        channel_account: str,
        mapper: QQEventMapper,
        session_store: QQGatewaySessionStore,
        on_event: Callable[[InboundEvent], None],
    ) -> None:
        self.channel_account = channel_account
        self.mapper = mapper
        self.session_store = session_store
        self.on_event = on_event
        self.state = session_store.load(channel_account)

    def identify_payload(self, token: str) -> dict[str, Any]:
        return {
            "op": OP_IDENTIFY,
            "d": {
                "token": f"QQBot {token}",
                "intents": FULL_INTENTS,
                "shard": [0, 1],
            },
        }

    def handshake_payload(self, token: str) -> dict[str, Any]:
        if self.state.session_id and self.state.sequence is not None:
            return {
                "op": OP_RESUME,
                "d": {
                    "token": f"QQBot {token}",
                    "session_id": self.state.session_id,
                    "seq": self.state.sequence,
                },
            }
        return self.identify_payload(token)

    def heartbeat_payload(self) -> dict[str, Any]:
        self.state.heartbeat_acknowledged = False
        return {"op": OP_HEARTBEAT, "d": self.state.sequence}

    def handle(
        self,
        payload: dict[str, Any],
        *,
        received_at: datetime,
    ) -> str:
        previous_sequence = self.state.sequence
        if isinstance(payload.get("s"), int):
            self.state.sequence = int(payload["s"])
        operation = payload.get("op")
        if operation == OP_HEARTBEAT_ACK:
            self.state.heartbeat_acknowledged = True
            return "heartbeat_ack"
        if operation == OP_RECONNECT:
            return "reconnect"
        if operation == OP_INVALID_SESSION:
            if not bool(payload.get("d")):
                self.state = QQGatewayState()
                self.session_store.clear(self.channel_account)
            return "reconnect"
        if operation != OP_DISPATCH:
            return "ignored"
        event_type = str(payload.get("t") or "")
        data = payload.get("d") if isinstance(payload.get("d"), dict) else {}
        if event_type == "READY":
            self.state.session_id = str(data.get("session_id") or "")
            self.state.resume_url = str(data.get("resume_gateway_url") or "")
            self.session_store.save(
                self.channel_account,
                self.state,
                now=received_at,
            )
            return "ready"
        if event_type == "RESUMED":
            self.session_store.save(
                self.channel_account,
                self.state,
                now=received_at,
            )
            return "resumed"
        event = self.mapper.map(event_type, data, received_at=received_at)
        if event is not None:
            try:
                self.on_event(event)
            except Exception:
                # Resume from the last durably forwarded dispatch. Advancing the
                # sequence here would acknowledge and lose the inbound command.
                self.state.sequence = previous_sequence
                raise
            self.session_store.save(
                self.channel_account,
                self.state,
                now=received_at,
            )
            return "event"
        return "ignored"


class WebSocketLike(Protocol):
    def __enter__(self) -> WebSocketLike: ...

    def __exit__(self, *args: object) -> None: ...

    def send(self, data: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str: ...


class QQGatewayRunner:
    """Blocking runner intended for the dedicated QQ service process."""

    def __init__(
        self,
        adapter: QQHttpAdapter,
        protocol: QQGatewayProtocol,
        *,
        connector: Callable[..., WebSocketLike] | None = None,
    ) -> None:
        self.adapter = adapter
        self.protocol = protocol
        self.connector = connector

    def _connect(self, url: str) -> WebSocketLike:
        if self.connector is not None:
            return self.connector(
                url,
                open_timeout=12,
                close_timeout=3,
                ping_interval=PING_INTERVAL_SECONDS,
                ping_timeout=PING_TIMEOUT_SECONDS,
                max_size=2 * 1024 * 1024,
            )
        from websockets.sync.client import connect

        return connect(
            url,
            open_timeout=12,
            close_timeout=3,
            ping_interval=PING_INTERVAL_SECONDS,
            ping_timeout=PING_TIMEOUT_SECONDS,
            max_size=2 * 1024 * 1024,
        )

    def _gateway_url(self, token: str) -> str:
        if self.protocol.state.resume_url:
            return self.protocol.state.resume_url
        status, value = self.adapter.transport.request(
            f"{API_BASE}/gateway/bot",
            headers=self.adapter._headers(token),
        )
        url = str(value.get("url") or "")
        if status != 200 or not url.startswith("wss://"):
            raise RuntimeError("QQ gateway discovery failed")
        return url

    def connect_once(self, stop_event: threading.Event) -> str:
        token = self.adapter.access_token()
        gateway_url = self._gateway_url(token)
        heartbeat_interval = 30.0
        next_heartbeat = time.monotonic() + heartbeat_interval
        heartbeat_deadline: float | None = None
        with self._connect(gateway_url) as websocket:
            while not stop_event.is_set():
                current = time.monotonic()
                if heartbeat_deadline is not None and current >= heartbeat_deadline:
                    raise RuntimeError("QQ gateway heartbeat ack timeout")
                if current >= next_heartbeat:
                    websocket.send(json.dumps(self.protocol.heartbeat_payload()))
                    heartbeat_deadline = current + HEARTBEAT_ACK_TIMEOUT_SECONDS
                    next_heartbeat = current + heartbeat_interval
                wake_at = next_heartbeat
                if heartbeat_deadline is not None:
                    wake_at = min(wake_at, heartbeat_deadline)
                try:
                    raw = websocket.recv(timeout=min(1.0, max(0.05, wake_at - current)))
                except TimeoutError:
                    continue
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    continue
                if payload.get("op") == OP_HELLO:
                    data = payload.get("d") if isinstance(payload.get("d"), dict) else {}
                    interval_ms = float(data.get("heartbeat_interval") or 30000)
                    heartbeat_interval = max(5.0, interval_ms / 1000.0)
                    next_heartbeat = time.monotonic() + random.uniform(
                        0,
                        heartbeat_interval,
                    )
                    heartbeat_deadline = None
                    websocket.send(json.dumps(self.protocol.handshake_payload(token)))
                    continue
                if (
                    payload.get("op") == OP_DISPATCH
                    and str(payload.get("t") or "") == "INTERACTION_CREATE"
                ):
                    # QQ expects interaction callbacks to be acknowledged promptly.
                    # Do this before forwarding the command to the internal API,
                    # whose deterministic processing and persistence may take longer
                    # than the client-side interaction deadline.
                    data = payload.get("d") if isinstance(payload.get("d"), dict) else {}
                    interaction_id = str(data.get("id") or "")
                    if interaction_id:
                        self.adapter.acknowledge_interaction(interaction_id)
                action = self.protocol.handle(payload, received_at=datetime.now(UTC))
                if action == "heartbeat_ack":
                    heartbeat_deadline = None
                if action == "reconnect":
                    return "reconnect_requested"
        return "stopped"

    def _resumable(self) -> bool:
        """Whether a RESUME is possible, without touching the session id itself."""
        state = getattr(self.protocol, "state", None)
        return bool(getattr(state, "session_id", ""))

    def run(self, stop_event: threading.Event) -> None:
        retry_delays = (1, 2, 5, 10, 30, 60)
        failures = 0
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                reason = self.connect_once(stop_event)
            except Exception as exc:
                delay = retry_delays[min(failures, len(retry_delays) - 1)]
                failures += 1
                # Only the exception type is logged; messages may carry payload data.
                logger.warning(
                    "qq_gateway reconnecting reason=%s uptime=%.1fs resumable=%s delay=%ss",
                    type(exc).__name__,
                    time.monotonic() - started,
                    self._resumable(),
                    delay,
                )
                stop_event.wait(delay)
                continue
            failures = 0
            if stop_event.is_set():
                return
            logger.info(
                "qq_gateway reconnecting reason=%s uptime=%.1fs resumable=%s delay=0s",
                reason,
                time.monotonic() - started,
                self._resumable(),
            )
