"""Passkey registration and user-verified step-up ceremonies."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .database import VaultDatabase


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True, slots=True)
class StepUpProof:
    user_id: str
    credential_id: str
    authenticated_at: datetime
    expires_at: datetime


class PasskeyManager:
    def __init__(
        self,
        database: VaultDatabase,
        *,
        rp_id: str,
        rp_name: str,
        expected_origin: str,
        now: Callable[[], datetime],
        challenge_ttl: timedelta = timedelta(minutes=5),
        registration_verifier=verify_registration_response,
        authentication_verifier=verify_authentication_response,
    ) -> None:
        if not rp_id or not rp_name or not expected_origin.startswith("https://"):
            raise ValueError("Passkey RP and HTTPS origin are required")
        self.database = database
        self.rp_id = rp_id
        self.rp_name = rp_name
        self.expected_origin = expected_origin
        self.now = now
        self.challenge_ttl = challenge_ttl
        self.registration_verifier = registration_verifier
        self.authentication_verifier = authentication_verifier

    def begin_registration(
        self,
        *,
        user_id: str,
        user_name: str,
        display_name: str,
    ) -> str:
        existing = self._descriptors(user_id)
        options = generate_registration_options(
            rp_id=self.rp_id,
            rp_name=self.rp_name,
            user_id=hashlib.sha256(user_id.encode()).digest(),
            user_name=user_name,
            user_display_name=display_name,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=existing,
        )
        self._store_challenge(user_id, "registration", options.challenge)
        return options_to_json(options)

    def finish_registration(
        self,
        *,
        user_id: str,
        credential: str | dict[str, Any],
    ) -> str:
        challenge_id, challenge = self._pending_challenge(user_id, "registration")
        verification = self.registration_verifier(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=self.rp_id,
            expected_origin=self.expected_origin,
            require_user_verification=True,
        )
        credential_id = _b64(verification.credential_id)
        transports: list[str] = []
        if isinstance(credential, dict):
            response = credential.get("response")
            if isinstance(response, dict) and isinstance(response.get("transports"), list):
                transports = [str(item) for item in response["transports"]]
        now = self.now().astimezone(UTC).isoformat()
        with self.database.transaction() as connection:
            consumed = connection.execute(
                """
                UPDATE webauthn_challenges SET consumed_at=?
                WHERE id=? AND consumed_at IS NULL
                """,
                (now, challenge_id),
            ).rowcount
            if consumed != 1:
                raise PermissionError("Passkey challenge was already consumed")
            connection.execute(
                """
                INSERT INTO webauthn_credentials(
                    credential_id,user_id,public_key,sign_count,transports_json,
                    created_at,last_used_at
                ) VALUES(?,?,?,?,?,?,NULL)
                """,
                (
                    credential_id,
                    user_id,
                    _b64(verification.credential_public_key),
                    int(verification.sign_count),
                    json.dumps(transports, separators=(",", ":")),
                    now,
                ),
            )
        return credential_id

    def begin_authentication(self, *, user_id: str) -> str:
        descriptors = self._descriptors(user_id)
        if not descriptors:
            raise PermissionError("user has no registered passkey")
        options = generate_authentication_options(
            rp_id=self.rp_id,
            allow_credentials=descriptors,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        self._store_challenge(user_id, "authentication", options.challenge)
        return options_to_json(options)

    def finish_authentication(
        self,
        *,
        user_id: str,
        credential: str | dict[str, Any],
    ) -> StepUpProof:
        value = json.loads(credential) if isinstance(credential, str) else credential
        credential_id = str(value.get("id") or "")
        challenge_id, challenge = self._pending_challenge(user_id, "authentication")
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM webauthn_credentials
                WHERE credential_id=? AND user_id=?
                """,
                (credential_id, user_id),
            ).fetchone()
        if row is None:
            raise PermissionError("Passkey credential is not registered for this user")
        verification = self.authentication_verifier(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=self.rp_id,
            expected_origin=self.expected_origin,
            credential_public_key=_unb64(str(row["public_key"])),
            credential_current_sign_count=int(row["sign_count"]),
            require_user_verification=True,
        )
        authenticated_at = self.now().astimezone(UTC)
        with self.database.transaction() as connection:
            consumed = connection.execute(
                """
                UPDATE webauthn_challenges SET consumed_at=?
                WHERE id=? AND consumed_at IS NULL
                """,
                (authenticated_at.isoformat(), challenge_id),
            ).rowcount
            if consumed != 1:
                raise PermissionError("Passkey challenge was already consumed")
            connection.execute(
                """
                UPDATE webauthn_credentials
                SET sign_count=?,last_used_at=?
                WHERE credential_id=? AND user_id=?
                """,
                (
                    int(verification.new_sign_count),
                    authenticated_at.isoformat(),
                    credential_id,
                    user_id,
                ),
            )
        return StepUpProof(
            user_id,
            credential_id,
            authenticated_at,
            authenticated_at + timedelta(minutes=5),
        )

    def _descriptors(self, user_id: str) -> list[PublicKeyCredentialDescriptor]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT credential_id FROM webauthn_credentials WHERE user_id=?",
                (user_id,),
            ).fetchall()
        return [
            PublicKeyCredentialDescriptor(id=_unb64(str(row["credential_id"])))
            for row in rows
        ]

    def _store_challenge(self, user_id: str, purpose: str, challenge: bytes) -> None:
        now = self.now().astimezone(UTC)
        challenge_id = f"challenge_{secrets.token_urlsafe(18)}"
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO webauthn_challenges(
                    id,user_id,purpose,challenge,expires_at,consumed_at
                ) VALUES(?,?,?,?,?,NULL)
                """,
                (
                    challenge_id,
                    user_id,
                    purpose,
                    _b64(challenge),
                    (now + self.challenge_ttl).isoformat(),
                ),
            )

    def _pending_challenge(self, user_id: str, purpose: str) -> tuple[str, bytes]:
        now = self.now().astimezone(UTC)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT id,challenge,expires_at FROM webauthn_challenges
                WHERE user_id=? AND purpose=? AND consumed_at IS NULL
                ORDER BY expires_at DESC LIMIT 1
                """,
                (user_id, purpose),
            ).fetchone()
        if row is None or datetime.fromisoformat(str(row["expires_at"])) <= now:
            raise PermissionError("Passkey challenge is missing or expired")
        return str(row["id"]), _unb64(str(row["challenge"]))
