"""Ed25519 capability grants issued by the authenticated application boundary."""

from __future__ import annotations

import base64
import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


@dataclass(slots=True)
class CapabilityGrantIssuer:
    private_key: Ed25519PrivateKey = field(repr=False)
    issuer: str

    @classmethod
    def generate(cls, issuer: str) -> CapabilityGrantIssuer:
        return cls(Ed25519PrivateKey.generate(), issuer)

    @classmethod
    def from_private_bytes(
        cls,
        value: bytes,
        *,
        issuer: str,
    ) -> CapabilityGrantIssuer:
        return cls(Ed25519PrivateKey.from_private_bytes(value), issuer)

    def private_bytes(self) -> bytes:
        return self.private_key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )

    def public_bytes(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    def issue(
        self,
        *,
        subject: str,
        secret_id: str,
        action: str,
        audience: str,
        expires_at: datetime,
        authentication: str,
    ) -> str:
        if expires_at.tzinfo is None:
            raise ValueError("grant expiry must be timezone-aware")
        payload = {
            "issuer": self.issuer,
            "subject": subject,
            "secret_id": secret_id,
            "action": action,
            "audience": audience,
            "expires_at": expires_at.astimezone(UTC).isoformat(),
            "authentication": authentication,
            "nonce": secrets.token_urlsafe(24),
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        signature = self.private_key.sign(encoded)
        return f"{_b64(encoded)}.{_b64(signature)}"


def load_public_key(value: bytes) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(value)
