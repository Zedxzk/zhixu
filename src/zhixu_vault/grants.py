"""Capability verification with exact binding and one-time nonces."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .database import VaultDatabase


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True, slots=True)
class VerifiedGrant:
    issuer: str
    subject: str
    secret_id: str
    action: str
    audience: str
    expires_at: datetime
    authentication: str


class CapabilityGrantVerifier:
    def __init__(
        self,
        database: VaultDatabase,
        *,
        issuers: dict[str, Ed25519PublicKey],
        now: Callable[[], datetime],
        audience: str = "zhixu-vault",
    ) -> None:
        self.database = database
        self.issuers = issuers
        self.now = now
        self.audience = audience

    def verify_and_consume(
        self,
        token: str,
        *,
        secret_id: str,
        action: str,
    ) -> VerifiedGrant:
        try:
            payload_part, signature_part = token.split(".", 1)
            payload_bytes = _decode(payload_part)
            signature = _decode(signature_part)
            payload = json.loads(payload_bytes)
            issuer = str(payload["issuer"])
            public_key = self.issuers[issuer]
            public_key.verify(signature, payload_bytes)
            expires_at = datetime.fromisoformat(str(payload["expires_at"]))
            if expires_at.tzinfo is None:
                raise ValueError("naive expiry")
        except (ValueError, KeyError, TypeError, InvalidSignature) as exc:
            raise PermissionError("capability signature is invalid") from exc
        if str(payload.get("audience")) != self.audience:
            raise PermissionError("capability audience mismatch")
        if str(payload.get("secret_id")) != secret_id:
            raise PermissionError("capability secret mismatch")
        if str(payload.get("action")) != action:
            raise PermissionError("capability action mismatch")
        if expires_at.astimezone(UTC) <= self.now().astimezone(UTC):
            raise PermissionError("capability expired")
        nonce = str(payload.get("nonce") or "")
        if not nonce:
            raise PermissionError("capability nonce is missing")
        nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO grant_nonces(
                    nonce_hash,subject,secret_id,action,expires_at,consumed_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    nonce_hash,
                    str(payload["subject"]),
                    secret_id,
                    action,
                    expires_at.astimezone(UTC).isoformat(),
                    self.now().astimezone(UTC).isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                raise PermissionError("capability nonce was already consumed")
        return VerifiedGrant(
            issuer=issuer,
            subject=str(payload["subject"]),
            secret_id=secret_id,
            action=action,
            audience=self.audience,
            expires_at=expires_at,
            authentication=str(payload.get("authentication") or ""),
        )
