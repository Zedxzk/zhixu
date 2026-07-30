"""Authenticated field encryption and keyed opaque references."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from zhixu.domain.errors import ValidationError

_PREFIX = "enc:v1:"


@dataclass(frozen=True, slots=True)
class FieldCipher:
    """AES-GCM field encryption using a caller-supplied 256-bit application key."""

    key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.key) != 32:
            raise ValidationError("field encryption key must contain exactly 32 bytes")

    def encrypt(self, plaintext: str, *, context: str) -> str:
        if not context:
            raise ValidationError("encryption context is required")
        nonce = os.urandom(12)
        encrypted = AESGCM(self.key).encrypt(
            nonce,
            plaintext.encode("utf-8"),
            context.encode("utf-8"),
        )
        token = base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")
        return _PREFIX + token

    def decrypt(self, ciphertext: str, *, context: str) -> str:
        if not ciphertext.startswith(_PREFIX):
            raise ValidationError("encrypted field has an unsupported format")
        try:
            raw = base64.urlsafe_b64decode(ciphertext.removeprefix(_PREFIX))
            plaintext = AESGCM(self.key).decrypt(
                raw[:12],
                raw[12:],
                context.encode("utf-8"),
            )
        except Exception as exc:
            raise ValidationError("encrypted field authentication failed") from exc
        return plaintext.decode("utf-8")


@dataclass(frozen=True, slots=True)
class OpaqueReferenceFactory:
    """Creates non-reversible, account-scoped references for external identifiers."""

    key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.key) < 32:
            raise ValidationError("opaque reference key must contain at least 32 bytes")

    def create(self, prefix: str, *parts: str) -> str:
        if not prefix or any(not part for part in parts):
            raise ValidationError("opaque reference inputs must not be empty")
        payload = "\0".join(parts).encode("utf-8")
        digest = hmac.new(self.key, payload, hashlib.sha256).hexdigest()[:32]
        return f"{prefix}_{digest}"
