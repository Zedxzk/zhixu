"""Inbound screening for credentials, before storage and before any model.

Two separate jobs live here. Financial credentials are refused outright: the
project forbids storing a bank or payment secret in ordinary storage, and a
chat channel cannot satisfy the vault's step-up requirement. Everything else
is allowed through, but its value is replaced by a placeholder so the value
itself never reaches a model prompt.

Nothing in this module logs, echoes, or raises with matched material.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .patterns import CREDENTIAL_LABELS, SEPARATOR, TOKEN_PATTERN

FINANCIAL_REFUSAL_CODE = "financial_credential_blocked"

# Mirrors the cap on link placeholders.
MAX_REDACTED_SECRETS = 8

# Beyond this the text is not a note anyone typed; skip rather than scan.
_MAX_SCANNED_CHARS = 20_000

_PLACEHOLDER = re.compile(r"<SECRET_(\d+)>")
_FORGED_PLACEHOLDER = re.compile(r"<SECRET_", re.IGNORECASE)

# A credential-shaped value: visible ASCII, no whitespace, and either long or
# containing a digit. This is what keeps "支付密码是多少" from matching.
_VALUE = r"(?P<value>(?=[!-~]{3,64}(?![!-~]))(?=[!-~]*(?:\d|[!-~]{6}))[!-~]{3,64})"

# Financial nouns that already name a credential on their own.
_FINANCIAL_PHRASES = (
    r"支付密码|付款密码|银行卡密码|银行密码|卡密码|信用卡密码|储蓄卡密码"
    r"|借记卡密码|取款密码|提款密码|取现密码|交易密码|查询密码|网银密码"
    r"|网上银行密码|手机银行密码|U盾密码|社保卡密码|医保卡密码|安全码"
    r"|payment\s+password|pay\s+password|payment\s+pin|bank\s+pin"
    r"|card\s+pin|atm\s+pin|transaction\s+password|withdrawal\s+password"
    r"|online\s+banking\s+password|cvv2?|cvc2?"
)

# A financial context noun close to a credential noun, so an unlisted bank
# name such as 招商银行卡的密码 still resolves without enumerating banks.
_FINANCIAL_WINDOW = (
    r"(?:银行卡|信用卡|储蓄卡|借记卡|社保卡|医保卡|网银|网上银行|手机银行"
    r"|支付宝|微信支付|云闪付|U盾|ATM|POS|银行)[^\n]{0,8}?(?:密码|口令|密钥|PIN)"
)

_FINANCIAL_NOUN = re.compile(
    rf"(?:{_FINANCIAL_PHRASES}|{_FINANCIAL_WINDOW})",
    re.IGNORECASE,
)
_FINANCIAL_ASSIGNED = re.compile(
    rf"(?:{_FINANCIAL_PHRASES}|{_FINANCIAL_WINDOW}){SEPARATOR}{_VALUE}",
    re.IGNORECASE,
)

# A labelled credential worth hiding from the model. The floor of six matches
# the web gate, so the two screens agree on what counts as a value.
_LABELLED_VALUE = re.compile(
    rf"(?:{CREDENTIAL_LABELS}){SEPARATOR}"
    + r"(?P<value>(?=[!-~]{6,64}(?![!-~]))[!-~]{6,64})",
    re.IGNORECASE,
)


def contains_financial_credential(text: str) -> bool:
    """Whether the text states a banking or payment secret.

    Three signals must coincide: a financial noun, an assignment, and a
    credential-shaped value. Asking how to change a payment password, or
    saying the password is six ones, does not qualify.
    """

    if not text or len(text) > _MAX_SCANNED_CHARS:
        return False
    return _FINANCIAL_ASSIGNED.search(text) is not None


def mentions_financial_credential(text: str) -> bool:
    """Whether a financial credential is named at all, value or not."""

    if not text or len(text) > _MAX_SCANNED_CHARS:
        return False
    return _FINANCIAL_NOUN.search(text) is not None


class SecretRedactor:
    """Placeholder map for one request; mirrors the link mechanism.

    Stateful on purpose. A staged plan is replayed to the model when the user
    revises it, and that text must reuse the same numbering as the message it
    came from, or restoration would pair a placeholder with the wrong value.
    """

    def __init__(self) -> None:
        self._values: list[str] = []

    def __bool__(self) -> bool:
        return bool(self._values)

    def redact(self, text: str) -> str:
        if not text or len(text) > _MAX_SCANNED_CHARS:
            return text
        # A placeholder the user typed themselves must never be restorable.
        cleaned = _FORGED_PLACEHOLDER.sub("<SECRET_?", text)

        def take(value: str) -> str:
            if len(self._values) >= MAX_REDACTED_SECRETS:
                return value
            self._values.append(value)
            return f"<SECRET_{len(self._values)}>"

        def hide_labelled(match: re.Match[str]) -> str:
            value = match.group("value")
            return match.group(0).replace(value, take(value), 1)

        cleaned = _LABELLED_VALUE.sub(hide_labelled, cleaned)
        return TOKEN_PATTERN.sub(lambda match: take(match.group(0)), cleaned)

    def restore(self, text: str | None) -> str | None:
        if not text or not self._values:
            return text

        def put(match: re.Match[str]) -> str:
            index = int(match.group(1))
            if 1 <= index <= len(self._values):
                return self._values[index - 1]
            return match.group(0)

        return _PLACEHOLDER.sub(put, text)

    def blank(self, text: str | None) -> str | None:
        """Drop placeholders instead of restoring them.

        Used where the text is scheduled for later delivery: restoring there
        would broadcast the value on a timer.
        """

        if not text:
            return text
        return _PLACEHOLDER.sub("（已隐藏）", text)


def hide_credential_values(text: str, marker: str) -> str:
    """Replace credential values with a fixed marker, with no way back.

    For text that is shown to the model but never restored, such as a staged
    plan replayed during a revision. Apply it per field: once fields are joined
    into JSON a value runs into the next key with no separator and its end
    becomes unfindable.
    """

    if not text or len(text) > _MAX_SCANNED_CHARS:
        return text
    hidden = _LABELLED_VALUE.sub(
        lambda match: match.group(0).replace(match.group("value"), marker, 1),
        text,
    )
    return TOKEN_PATTERN.sub(marker, hidden)


def redact_all(texts: Sequence[str]) -> tuple[tuple[str, ...], SecretRedactor]:
    """Redact several texts through one shared placeholder map."""

    redactor = SecretRedactor()
    return tuple(redactor.redact(value) for value in texts), redactor
