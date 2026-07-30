"""Hashed admin sessions and one-time opaque identity linking."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type

from zhixu.domain import (
    Action,
    AuthenticationStrength,
    AuthorizedAction,
    EncryptedIdentifier,
    ExternalIdentity,
)
from zhixu.domain.errors import PermissionDenied, ValidationError

from .database import Database


def _dump(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValidationError("time must be timezone-aware")
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class AdminPrincipal:
    user_id: str
    authentication: AuthenticationStrength
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AdminSessionToken:
    value: str = field(repr=False)
    expires_at: datetime


class AdminSessionStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        user_id: str,
        authentication: AuthenticationStrength,
        now: datetime,
        lifetime: timedelta = timedelta(hours=8),
    ) -> AdminSessionToken:
        if authentication < AuthenticationStrength.PASSWORD:
            raise PermissionDenied("admin sessions require password authentication")
        raw = secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        expires_at = now + lifetime
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO admin_sessions(
                    token_hash,user_id,authentication,created_at,expires_at,revoked_at
                ) VALUES(?,?,?,?,?,NULL)
                """,
                (
                    digest,
                    user_id,
                    authentication.name.lower(),
                    _dump(now),
                    _dump(expires_at),
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_events(
                    occurred_at,actor_user_id,action,resource_kind,resource_id,
                    outcome,reason_code
                ) VALUES(?,?,'create','admin_session',?,'completed',?)
                """,
                (
                    _dump(now),
                    user_id,
                    digest[:24],
                    f"authentication_{authentication.name.lower()}",
                ),
            )
        return AdminSessionToken(raw, expires_at)

    def authenticate(self, raw_token: str, *, now: datetime) -> AdminPrincipal | None:
        if not raw_token:
            return None
        digest = hashlib.sha256(raw_token.encode()).hexdigest()
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT user_id,authentication,expires_at FROM admin_sessions
                WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?
                """,
                (digest, _dump(now)),
            ).fetchone()
        if row is None:
            return None
        return AdminPrincipal(
            str(row["user_id"]),
            AuthenticationStrength[str(row["authentication"]).upper()],
            datetime.fromisoformat(str(row["expires_at"])),
        )

    def revoke(self, raw_token: str, *, now: datetime) -> bool:
        digest = hashlib.sha256(raw_token.encode()).hexdigest()
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE admin_sessions SET revoked_at=?
                WHERE token_hash=? AND revoked_at IS NULL
                """,
                (_dump(now), digest),
            ).rowcount
        return changed == 1

    def revoke_user(self, user_id: str, *, now: datetime) -> int:
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE admin_sessions SET revoked_at=?
                WHERE user_id=? AND revoked_at IS NULL
                """,
                (_dump(now), user_id),
            ).rowcount
        return max(0, changed)


class AdminCredentialStore:
    """Argon2id password fallback with persistent per-principal lockout."""

    def __init__(
        self,
        database: Database,
        *,
        max_attempts: int = 5,
        window: timedelta = timedelta(minutes=15),
        lockout: timedelta = timedelta(minutes=15),
    ) -> None:
        self.database = database
        self.max_attempts = max_attempts
        self.window = window
        self.lockout = lockout
        self.hasher = PasswordHasher(
            time_cost=3,
            memory_cost=65_536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._dummy_hash = self.hasher.hash("synthetic-dummy-password-never-accepted")

    def set_password(self, user_id: str, password: str, *, now: datetime) -> None:
        if not user_id.strip():
            raise ValidationError("admin user id is required")
        if not 12 <= len(password) <= 1024:
            raise ValidationError("admin password must contain 12 to 1024 characters")
        password_hash = self.hasher.hash(password)
        with self.database.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM users WHERE id=? AND status='active'",
                (user_id,),
            ).fetchone()
            if exists is None:
                raise PermissionDenied("admin principal is unavailable")
            connection.execute(
                """
                INSERT INTO admin_credentials(user_id,password_hash,updated_at)
                VALUES(?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    password_hash=excluded.password_hash,
                    updated_at=excluded.updated_at
                """,
                (user_id, password_hash, _dump(now)),
            )
            connection.execute("DELETE FROM admin_login_state WHERE user_id=?", (user_id,))

    def verify(self, user_id: str, password: str, *, now: datetime) -> bool:
        if not user_id or len(password) > 1024:
            return False
        with self.database.connect() as connection:
            credential = connection.execute(
                """
                SELECT c.password_hash FROM admin_credentials c
                JOIN users u ON u.id=c.user_id
                WHERE c.user_id=? AND u.status='active'
                """,
                (user_id,),
            ).fetchone()
            state = connection.execute(
                "SELECT * FROM admin_login_state WHERE user_id=?",
                (user_id,),
            ).fetchone()
        if state is not None and state["locked_until"]:
            locked_until = datetime.fromisoformat(str(state["locked_until"]))
            if locked_until > now:
                self._audit_login(user_id, now=now, outcome="rejected", reason="locked")
                return False
        password_hash = (
            str(credential["password_hash"]) if credential is not None else self._dummy_hash
        )
        try:
            valid = self.hasher.verify(password_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            valid = False
        if not valid or credential is None:
            self._record_failure(user_id, now=now, state=state)
            return False
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM admin_login_state WHERE user_id=?", (user_id,))
            if self.hasher.check_needs_rehash(password_hash):
                connection.execute(
                    "UPDATE admin_credentials SET password_hash=?,updated_at=? WHERE user_id=?",
                    (self.hasher.hash(password), _dump(now), user_id),
                )
        self._audit_login(user_id, now=now, outcome="completed", reason="password")
        return True

    def _record_failure(
        self,
        user_id: str,
        *,
        now: datetime,
        state: sqlite3.Row | None,
    ) -> None:
        window_started = now
        attempts = 1
        if state is not None:
            prior_start = datetime.fromisoformat(str(state["window_started_at"]))
            if now - prior_start < self.window:
                window_started = prior_start
                attempts = int(state["attempts"]) + 1
        locked_until = now + self.lockout if attempts >= self.max_attempts else None
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO admin_login_state(
                    user_id,window_started_at,attempts,locked_until
                ) VALUES(?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    window_started_at=excluded.window_started_at,
                    attempts=excluded.attempts,
                    locked_until=excluded.locked_until
                """,
                (
                    user_id,
                    _dump(window_started),
                    attempts,
                    _dump(locked_until) if locked_until else None,
                ),
            )
        self._audit_login(
            user_id,
            now=now,
            outcome="rejected",
            reason="invalid_credentials",
        )

    def _audit_login(
        self,
        user_id: str,
        *,
        now: datetime,
        outcome: str,
        reason: str,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO audit_events(
                    occurred_at,actor_user_id,action,resource_kind,resource_id,
                    outcome,reason_code
                ) VALUES(?,?,'authenticate','admin_session','login',?,?)
                """,
                (_dump(now), user_id or "unknown", outcome, reason),
            )


@dataclass(frozen=True, slots=True)
class LinkChallenge:
    id: str
    code: str = field(repr=False)
    expires_at: datetime


class IdentityLinkStore:
    def __init__(
        self,
        database: Database,
        *,
        challenge_key: bytes,
        max_attempts: int = 5,
    ) -> None:
        if len(challenge_key) < 32:
            raise ValueError("identity challenge key must contain at least 32 bytes")
        self.database = database
        self.challenge_key = challenge_key
        self.max_attempts = max_attempts

    def issue(
        self,
        *,
        user_id: str,
        channel: str,
        channel_account: str,
        opaque_ref: str,
        encrypted_subject: EncryptedIdentifier,
        now: datetime,
        lifetime: timedelta = timedelta(minutes=10),
    ) -> LinkChallenge:
        challenge_id = f"link_{secrets.token_urlsafe(18)}"
        code = f"{secrets.randbelow(100_000_000):08d}"
        expires_at = now + lifetime
        digest = self._digest(challenge_id, code)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO identity_link_challenges(
                    id,user_id,channel,challenge_hash,expires_at,consumed_at,
                    channel_account,opaque_ref,external_subject_enc,attempts
                ) VALUES(?,?,?,?,?,NULL,?,?,?,0)
                """,
                (
                    challenge_id,
                    user_id,
                    channel,
                    digest,
                    _dump(expires_at),
                    channel_account,
                    opaque_ref,
                    encrypted_subject.value,
                ),
            )
        return LinkChallenge(challenge_id, code, expires_at)

    def consume(
        self,
        *,
        challenge_id: str,
        code: str,
        identity_id: str,
        authorization: AuthorizedAction,
    ) -> ExternalIdentity:
        if (
            authorization.action is not Action.CREATE
            or authorization.resource.kind != "external_identity"
            or authorization.resource.id != identity_id
        ):
            raise PermissionDenied("identity authorization does not match link")
        now = authorization.authorized_at
        invalid_code = False
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM identity_link_challenges
                WHERE id=? AND consumed_at IS NULL
                """,
                (challenge_id,),
            ).fetchone()
            if row is None:
                raise PermissionDenied("identity challenge is unavailable")
            if datetime.fromisoformat(str(row["expires_at"])) <= now:
                raise PermissionDenied("identity challenge expired")
            if int(row["attempts"]) >= self.max_attempts:
                raise PermissionDenied("identity challenge attempt limit reached")
            expected = str(row["challenge_hash"])
            actual = self._digest(challenge_id, code)
            if not hmac.compare_digest(expected, actual):
                connection.execute(
                    "UPDATE identity_link_challenges SET attempts=attempts+1 WHERE id=?",
                    (challenge_id,),
                )
                invalid_code = True
            else:
                user_id = str(row["user_id"])
                if authorization.resource.owner_user_id != user_id:
                    raise PermissionDenied("identity challenge user mismatch")
                connection.execute(
                    """
                    INSERT INTO external_identities(
                        id,user_id,channel,channel_account,external_subject_enc,
                        opaque_ref,created_at
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        identity_id,
                        user_id,
                        str(row["channel"]),
                        str(row["channel_account"]),
                        str(row["external_subject_enc"]),
                        str(row["opaque_ref"]),
                        _dump(now),
                    ),
                )
                connection.execute(
                    "UPDATE identity_link_challenges SET consumed_at=? WHERE id=?",
                    (_dump(now), challenge_id),
                )
                connection.execute(
                    """
                    INSERT INTO audit_events(
                        occurred_at,actor_user_id,action,resource_kind,resource_id,
                        outcome,reason_code
                    ) VALUES(?,?,?,?,?,'completed','otp_verified')
                    """,
                    (
                        _dump(now),
                        authorization.actor_user_id,
                        "create",
                        "external_identity",
                        identity_id,
                    ),
                )
        if invalid_code:
            raise PermissionDenied("identity challenge code is invalid")
        return ExternalIdentity(
            identity_id,
            user_id,
            str(row["channel"]),
            str(row["channel_account"]),
            EncryptedIdentifier(str(row["external_subject_enc"])),
            str(row["opaque_ref"]),
            now,
        )

    def _digest(self, challenge_id: str, code: str) -> str:
        return hmac.new(
            self.challenge_key,
            f"{challenge_id}\0{code}".encode(),
            hashlib.sha256,
        ).hexdigest()
