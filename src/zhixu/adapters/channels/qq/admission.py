"""Fail-closed inbound admission and body-free receipt storage."""

from __future__ import annotations

from dataclasses import dataclass

from zhixu.adapters.storage.sqlite.repositories import UserRepository
from zhixu.channels import ConversationKind, InboundEvent

from .contacts import QQContactStore


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    accepted: bool
    user_id: str | None
    reason_code: str


class InboundAdmission:
    def __init__(self, users: UserRepository, contacts: QQContactStore) -> None:
        self.users = users
        self.contacts = contacts

    def decide(self, event: InboundEvent) -> AdmissionDecision:
        identity = self.users.identity_by_opaque_ref(
            event.channel,
            event.channel_account,
            event.external_actor_ref,
        )
        if identity is None:
            return AdmissionDecision(False, None, "identity_unbound")
        text = (event.text or "").lstrip()
        if event.conversation_kind is ConversationKind.GROUP:
            explicitly_addressed = bool(event.metadata.get("mentioned")) or text.startswith("/")
            if not explicitly_addressed:
                return AdmissionDecision(False, identity.user_id, "group_trigger_required")
            if not self.contacts.commands_enabled(
                event.channel_account,
                event.external_conversation_ref,
            ):
                return AdmissionDecision(False, identity.user_id, "conversation_disabled")
        return AdmissionDecision(True, identity.user_id, "accepted")
