"""Interactive, fail-closed provisioning for encrypted deployment bundles."""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BUNDLE_V1_FORMAT = "zhixu-deployment-secrets-v1"
BUNDLE_V2_FORMAT = "zhixu-deployment-secrets-v2"
MAX_BUNDLE_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class DeploymentBundleKDF:
    time_cost: int = 4
    memory_cost_kib: int = 131_072
    parallelism: int = 2

    def validate(self) -> None:
        if not 1 <= self.time_cost <= 10:
            raise ValueError("deployment bundle KDF time cost is invalid")
        if not 8_192 <= self.memory_cost_kib <= 1_048_576:
            raise ValueError("deployment bundle KDF memory cost is invalid")
        if not 1 <= self.parallelism <= 16:
            raise ValueError("deployment bundle KDF parallelism is invalid")

    def derive(self, passphrase: str, salt: bytes) -> bytes:
        self.validate()
        if not passphrase or "\0" in passphrase:
            raise ValueError("deployment bundle passphrase is invalid")
        return hash_secret_raw(
            passphrase.encode(),
            salt,
            time_cost=self.time_cost,
            memory_cost=self.memory_cost_kib,
            parallelism=self.parallelism,
            hash_len=32,
            type=Type.ID,
        )


@dataclass(frozen=True, slots=True)
class QQDeploymentCredentials:
    app_id: str = field(repr=False)
    client_secret: str = field(repr=False)

    def validate(self) -> None:
        if (
            not self.app_id
            or len(self.app_id) > 160
            or not self.app_id.isascii()
            or not self.app_id.isdigit()
        ):
            raise ValueError("QQ application identifier is invalid")
        if (
            not self.client_secret
            or len(self.client_secret) > 4096
            or any(
                ord(character) < 0x20 or ord(character) == 0x7F
                for character in self.client_secret
            )
        ):
            raise ValueError("QQ client secret is invalid")


@dataclass(frozen=True, slots=True)
class ProvisionResult:
    credential_files: int
    recovery_bundle_created: bool


@dataclass(frozen=True, slots=True)
class DeploymentSecrets:
    qq: QQDeploymentCredentials = field(repr=False)
    app_field_key: bytes = field(repr=False)
    qq_field_key: bytes = field(repr=False)
    outbound_field_key: bytes = field(repr=False)
    app_reference_key: bytes = field(repr=False)
    identity_challenge_key: bytes = field(repr=False)
    channel_service_token: str = field(repr=False)
    grant_issuer_private_key: bytes = field(repr=False)
    application_backup_passphrase: str = field(repr=False)
    vault_backup_passphrase: str = field(repr=False)

    @classmethod
    def generate(cls, qq: QQDeploymentCredentials) -> DeploymentSecrets:
        qq.validate()
        return cls(
            qq=qq,
            app_field_key=os.urandom(32),
            qq_field_key=os.urandom(32),
            outbound_field_key=os.urandom(32),
            app_reference_key=os.urandom(32),
            identity_challenge_key=os.urandom(32),
            channel_service_token=secrets.token_urlsafe(48),
            grant_issuer_private_key=Ed25519PrivateKey.generate().private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            ),
            application_backup_passphrase=secrets.token_urlsafe(48),
            vault_backup_passphrase=secrets.token_urlsafe(48),
        )

    def validate(self) -> None:
        self.qq.validate()
        for value in (
            self.app_field_key,
            self.qq_field_key,
            self.outbound_field_key,
            self.app_reference_key,
            self.identity_challenge_key,
            self.grant_issuer_private_key,
        ):
            if len(value) != 32:
                raise ValueError("deployment key has an invalid length")
        if (
            len(self.channel_service_token) < 32
            or len(self.application_backup_passphrase) < 12
            or len(self.vault_backup_passphrase) < 12
            or any(
                "\0" in value or "\n" in value
                for value in (
                    self.channel_service_token,
                    self.application_backup_passphrase,
                    self.vault_backup_passphrase,
                )
            )
        ):
            raise ValueError("deployment text credential is invalid")


def create_deployment_bundle(
    destination: str | Path,
    credentials: QQDeploymentCredentials,
    *,
    passphrase: str,
    parameters: DeploymentBundleKDF | None = None,
) -> Path:
    credentials.validate()
    selected = parameters or DeploymentBundleKDF()
    selected.validate()
    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise FileExistsError("deployment bundle destination already exists")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return _write_complete_bundle(
        target,
        DeploymentSecrets.generate(credentials),
        passphrase=passphrase,
        parameters=selected,
    )


def install_deployment_bundle(
    source: str | Path,
    *,
    passphrase: str,
    etc_directory: str | Path,
    expected_owner_uid: int,
    expected_owner_gid: int,
    recovery_output: str | Path | None = None,
) -> ProvisionResult:
    deployment_secrets, complete, parameters = _decrypt_bundle(
        Path(source),
        passphrase,
    )
    recovery_created = False
    if not complete:
        if recovery_output is None:
            raise ValueError("legacy bundle installation requires a recovery output")
        recovery = Path(recovery_output)
        if recovery.resolve() == Path(source).resolve():
            raise ValueError("recovery output must differ from the legacy bundle")
        recovery.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_complete_bundle(
            recovery,
            deployment_secrets,
            passphrase=passphrase,
            parameters=parameters,
        )
        recovery_created = True
    elif recovery_output is not None:
        raise ValueError("complete deployment bundle does not need a recovery output")
    etc = Path(etc_directory)
    credential_directory = etc / "credentials"
    public_key_path = etc / "grant_issuer_public.pem"
    _require_directory(
        etc,
        expected_owner_uid=expected_owner_uid,
        expected_owner_gid=expected_owner_gid,
        modes={0o700, 0o750, 0o755},
    )
    _require_directory(
        credential_directory,
        expected_owner_uid=expected_owner_uid,
        expected_owner_gid=expected_owner_gid,
        modes={0o700},
    )
    _require_bootstrap_credentials(
        credential_directory,
        expected_owner_uid=expected_owner_uid,
        expected_owner_gid=expected_owner_gid,
    )
    if public_key_path.exists() or public_key_path.is_symlink():
        raise FileExistsError("grant issuer public key already exists")

    issuer = Ed25519PrivateKey.from_private_bytes(
        deployment_secrets.grant_issuer_private_key
    )
    generated = {
        "app_field_key": _key_payload(deployment_secrets.app_field_key),
        "qq_field_key": _key_payload(deployment_secrets.qq_field_key),
        "outbound_field_key": _key_payload(
            deployment_secrets.outbound_field_key
        ),
        "app_reference_key": _key_payload(
            deployment_secrets.app_reference_key
        ),
        "identity_challenge_key": _key_payload(
            deployment_secrets.identity_challenge_key
        ),
        "channel_service_token": _text_payload(
            deployment_secrets.channel_service_token
        ),
        "grant_issuer_private_key": _key_payload(
            deployment_secrets.grant_issuer_private_key
        ),
        "qq_app_id": _text_payload(deployment_secrets.qq.app_id),
        "qq_client_secret": _text_payload(
            deployment_secrets.qq.client_secret
        ),
        "application_backup_passphrase": _text_payload(
            deployment_secrets.application_backup_passphrase
        ),
        "vault_backup_passphrase": _text_payload(
            deployment_secrets.vault_backup_passphrase
        ),
        "llm_api_key": b"",
    }
    staging = Path(
        tempfile.mkdtemp(
            prefix=".credentials.install-",
            dir=etc,
        )
    )
    os.chmod(staging, 0o700)
    os.chown(staging, expected_owner_uid, expected_owner_gid)
    public_staging = etc / f".grant-public.install-{secrets.token_hex(8)}"
    backup_directory = etc / f".credentials.bootstrap-{secrets.token_hex(8)}"
    swapped = False
    try:
        for name, payload in generated.items():
            _write_new_owned(
                staging / name,
                payload,
                mode=0o600,
                owner_uid=expected_owner_uid,
                owner_gid=expected_owner_gid,
            )
        _write_new_owned(
            public_staging,
            issuer.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ),
            mode=0o644,
            owner_uid=expected_owner_uid,
            owner_gid=expected_owner_gid,
        )
        _fsync_directory(staging)
        _fsync_directory(etc)
        os.replace(credential_directory, backup_directory)
        os.replace(staging, credential_directory)
        swapped = True
        os.replace(public_staging, public_key_path)
        _fsync_directory(etc)
    except Exception:
        if swapped:
            failed_directory = etc / f".credentials.failed-{secrets.token_hex(8)}"
            with suppress(OSError):
                os.replace(credential_directory, failed_directory)
                os.replace(backup_directory, credential_directory)
                shutil.rmtree(failed_directory)
        raise
    finally:
        with suppress(OSError):
            public_staging.unlink()
        if staging.exists():
            shutil.rmtree(staging)
    shutil.rmtree(backup_directory)
    _fsync_directory(etc)
    return ProvisionResult(
        credential_files=len(generated),
        recovery_bundle_created=recovery_created,
    )


def _decrypt_bundle(
    path: Path,
    passphrase: str,
) -> tuple[DeploymentSecrets, bool, DeploymentBundleKDF]:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > MAX_BUNDLE_BYTES
    ):
        raise ValueError("deployment bundle file is invalid")
    envelope = _strict_json(path.read_bytes())
    if not isinstance(envelope, dict) or set(envelope) != {
        "format",
        "kdf",
        "salt",
        "nonce",
        "ciphertext",
    }:
        raise ValueError("deployment bundle envelope is invalid")
    bundle_format = envelope["format"]
    if bundle_format not in {BUNDLE_V1_FORMAT, BUNDLE_V2_FORMAT}:
        raise ValueError("deployment bundle format is unsupported")
    kdf = envelope["kdf"]
    if not isinstance(kdf, dict) or set(kdf) != {
        "name",
        "time_cost",
        "memory_cost_kib",
        "parallelism",
    }:
        raise ValueError("deployment bundle KDF is invalid")
    if kdf["name"] != "argon2id":
        raise ValueError("deployment bundle KDF is unsupported")
    if any(
        isinstance(kdf[name], bool) or not isinstance(kdf[name], int)
        for name in ("time_cost", "memory_cost_kib", "parallelism")
    ):
        raise ValueError("deployment bundle KDF parameters are invalid")
    parameters = DeploymentBundleKDF(
        time_cost=kdf["time_cost"],
        memory_cost_kib=kdf["memory_cost_kib"],
        parallelism=kdf["parallelism"],
    )
    parameters.validate()
    salt = _decode(envelope["salt"])
    nonce = _decode(envelope["nonce"])
    ciphertext = _decode(envelope["ciphertext"])
    if len(salt) != 16 or len(nonce) != 12 or not 16 <= len(ciphertext) <= 32 * 1024:
        raise ValueError("deployment bundle cryptographic fields are invalid")
    try:
        plaintext = AESGCM(parameters.derive(passphrase, salt)).decrypt(
            nonce,
            ciphertext,
            str(bundle_format).encode(),
        )
    except InvalidTag as exc:
        raise PermissionError("deployment bundle decryption failed") from exc
    value = _strict_json(plaintext)
    if not isinstance(value, dict):
        raise ValueError("deployment bundle credential schema is invalid")
    if not all(isinstance(item, str) for item in value.values()):
        raise ValueError("deployment bundle credential values are invalid")
    qq_fields = {"qq_app_id", "qq_client_secret"}
    if bundle_format == BUNDLE_V1_FORMAT:
        if set(value) != qq_fields:
            raise ValueError("legacy deployment bundle credential schema is invalid")
        qq = QQDeploymentCredentials(
            app_id=value["qq_app_id"],
            client_secret=value["qq_client_secret"],
        )
        return DeploymentSecrets.generate(qq), False, parameters
    expected = qq_fields | {
        "app_field_key",
        "qq_field_key",
        "outbound_field_key",
        "app_reference_key",
        "identity_challenge_key",
        "channel_service_token",
        "grant_issuer_private_key",
        "application_backup_passphrase",
        "vault_backup_passphrase",
    }
    if set(value) != expected:
        raise ValueError("deployment bundle credential schema is invalid")
    result = DeploymentSecrets(
        qq=QQDeploymentCredentials(
            app_id=value["qq_app_id"],
            client_secret=value["qq_client_secret"],
        ),
        app_field_key=_decode_key(value["app_field_key"]),
        qq_field_key=_decode_key(value["qq_field_key"]),
        outbound_field_key=_decode_key(value["outbound_field_key"]),
        app_reference_key=_decode_key(value["app_reference_key"]),
        identity_challenge_key=_decode_key(value["identity_challenge_key"]),
        channel_service_token=value["channel_service_token"],
        grant_issuer_private_key=_decode_key(value["grant_issuer_private_key"]),
        application_backup_passphrase=value["application_backup_passphrase"],
        vault_backup_passphrase=value["vault_backup_passphrase"],
    )
    result.validate()
    return result, True, parameters


def _write_complete_bundle(
    target: Path,
    deployment_secrets: DeploymentSecrets,
    *,
    passphrase: str,
    parameters: DeploymentBundleKDF,
) -> Path:
    deployment_secrets.validate()
    if target.exists() or target.is_symlink():
        raise FileExistsError("deployment bundle destination already exists")
    plaintext = json.dumps(
        {
            "qq_app_id": deployment_secrets.qq.app_id,
            "qq_client_secret": deployment_secrets.qq.client_secret,
            "app_field_key": _encode(deployment_secrets.app_field_key),
            "qq_field_key": _encode(deployment_secrets.qq_field_key),
            "outbound_field_key": _encode(
                deployment_secrets.outbound_field_key
            ),
            "app_reference_key": _encode(
                deployment_secrets.app_reference_key
            ),
            "identity_challenge_key": _encode(
                deployment_secrets.identity_challenge_key
            ),
            "channel_service_token": deployment_secrets.channel_service_token,
            "grant_issuer_private_key": _encode(
                deployment_secrets.grant_issuer_private_key
            ),
            "application_backup_passphrase": (
                deployment_secrets.application_backup_passphrase
            ),
            "vault_backup_passphrase": (
                deployment_secrets.vault_backup_passphrase
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    salt = os.urandom(16)
    nonce = os.urandom(12)
    ciphertext = AESGCM(parameters.derive(passphrase, salt)).encrypt(
        nonce,
        plaintext,
        BUNDLE_V2_FORMAT.encode(),
    )
    envelope = {
        "format": BUNDLE_V2_FORMAT,
        "kdf": {
            "name": "argon2id",
            "time_cost": parameters.time_cost,
            "memory_cost_kib": parameters.memory_cost_kib,
            "parallelism": parameters.parallelism,
        },
        "salt": _encode(salt),
        "nonce": _encode(nonce),
        "ciphertext": _encode(ciphertext),
    }
    _atomic_create(
        target,
        json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode(),
        mode=0o600,
    )
    return target


def _strict_json(payload: bytes) -> Any:
    if len(payload) > MAX_BUNDLE_BYTES:
        raise ValueError("deployment bundle JSON is too large")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("deployment bundle JSON contains a duplicate key")
            result[key] = value
        return result

    try:
        return json.loads(payload, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("deployment bundle JSON is invalid") from exc


def _require_directory(
    path: Path,
    *,
    expected_owner_uid: int,
    expected_owner_gid: int,
    modes: set[int],
) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) not in modes
        or metadata.st_uid != expected_owner_uid
        or metadata.st_gid != expected_owner_gid
    ):
        raise PermissionError(f"deployment directory {path.name} is insecure")


def _require_bootstrap_credentials(
    path: Path,
    *,
    expected_owner_uid: int,
    expected_owner_gid: int,
) -> None:
    entries = list(path.iterdir())
    if not entries:
        return
    if len(entries) != 1 or entries[0].name != "llm_api_key":
        raise FileExistsError("deployment credentials are already provisioned")
    metadata = entries[0].lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size != 0
        or metadata.st_uid != expected_owner_uid
        or metadata.st_gid != expected_owner_gid
    ):
        raise FileExistsError("bootstrap LLM credential is not empty and secure")


def _key_payload(value: bytes | None = None) -> bytes:
    return base64.urlsafe_b64encode(value or os.urandom(32)) + b"\n"


def _text_payload(value: str) -> bytes:
    return value.encode() + b"\n"


def _write_new_owned(
    path: Path,
    payload: bytes,
    *,
    mode: int,
    owner_uid: int,
    owner_gid: int,
) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, owner_uid, owner_gid)
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(payload)
            output.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_create(path: Path, payload: bytes, *, mode: int) -> None:
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".partial",
        dir=path.parent,
    )
    temporary = Path(raw_temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(payload)
            output.flush()
            os.fsync(descriptor)
        os.link(temporary, path)
        _fsync_directory(path.parent)
    finally:
        os.close(descriptor)
        with suppress(OSError):
            temporary.unlink()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode()


def _decode(value: Any) -> bytes:
    if not isinstance(value, str) or len(value) > MAX_BUNDLE_BYTES:
        raise ValueError("deployment bundle Base64 field is invalid")
    try:
        return base64.b64decode(value, altchars=b"-_", validate=True)
    except Exception as exc:
        raise ValueError("deployment bundle Base64 field is invalid") from exc


def _decode_key(value: str) -> bytes:
    decoded = _decode(value)
    if len(decoded) != 32:
        raise ValueError("deployment bundle key has an invalid length")
    return decoded
