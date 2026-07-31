from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from zhixu.runtime.preflight import (
    PreflightPaths,
    verify_deployment_configuration,
)
from zhixu.runtime.provision import (
    BUNDLE_V1_FORMAT,
    DeploymentBundleKDF,
    QQDeploymentCredentials,
    create_deployment_bundle,
    install_deployment_bundle,
)

_FAST_KDF = DeploymentBundleKDF(
    time_cost=1,
    memory_cost_kib=8_192,
    parallelism=1,
)
_PASSPHRASE = "synthetic deployment phrase"
_APP_ID = "1234567890"
_CLIENT_SECRET = "synthetic-qq-client-secret"  # pragma: allowlist secret


def _bootstrap_etc(tmp_path: Path) -> Path:
    etc = tmp_path / "etc" / "zhixu"
    credentials = etc / "credentials"
    credentials.mkdir(parents=True, mode=0o700)
    credentials.chmod(0o700)
    llm_key = credentials / "llm_api_key"
    llm_key.write_bytes(b"")
    llm_key.chmod(0o600)
    return etc


def _create_complete_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "deployment.zxe"
    create_deployment_bundle(
        bundle,
        QQDeploymentCredentials(_APP_ID, _CLIENT_SECRET),
        passphrase=_PASSPHRASE,
        parameters=_FAST_KDF,
    )
    return bundle


def _legacy_bundle(path: Path) -> None:
    salt = b"S" * 16
    nonce = b"N" * 12
    plaintext = json.dumps(
        {
            "qq_app_id": _APP_ID,
            "qq_client_secret": _CLIENT_SECRET,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    ciphertext = AESGCM(_FAST_KDF.derive(_PASSPHRASE, salt)).encrypt(
        nonce,
        plaintext,
        BUNDLE_V1_FORMAT.encode(),
    )
    path.write_text(
        json.dumps(
            {
                "format": BUNDLE_V1_FORMAT,
                "kdf": {
                    "name": "argon2id",
                    "time_cost": _FAST_KDF.time_cost,
                    "memory_cost_kib": _FAST_KDF.memory_cost_kib,
                    "parallelism": _FAST_KDF.parallelism,
                },
                "salt": base64.urlsafe_b64encode(salt).decode(),
                "nonce": base64.urlsafe_b64encode(nonce).decode(),
                "ciphertext": base64.urlsafe_b64encode(ciphertext).decode(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _install(bundle: Path, etc: Path, **kwargs: object) -> object:
    return install_deployment_bundle(
        bundle,
        passphrase=_PASSPHRASE,
        etc_directory=etc,
        expected_owner_uid=os.getuid(),
        expected_owner_gid=os.getgid(),
        **kwargs,
    )


def test_complete_bundle_is_encrypted_and_installs_preflight_ready_credentials(
    tmp_path: Path,
) -> None:
    bundle = _create_complete_bundle(tmp_path)
    raw_bundle = bundle.read_bytes()

    assert stat.S_IMODE(bundle.stat().st_mode) == 0o600
    assert _APP_ID.encode() not in raw_bundle
    assert _CLIENT_SECRET.encode() not in raw_bundle
    assert _PASSPHRASE.encode() not in raw_bundle

    etc = _bootstrap_etc(tmp_path)
    result = _install(bundle, etc)

    assert result.credential_files == 12
    assert result.recovery_bundle_created is False
    credentials = etc / "credentials"
    assert {entry.name for entry in credentials.iterdir()} == {
        "app_field_key",
        "qq_field_key",
        "outbound_field_key",
        "app_reference_key",
        "identity_challenge_key",
        "channel_service_token",
        "grant_issuer_private_key",
        "qq_app_id",
        "qq_client_secret",
        "application_backup_passphrase",
        "vault_backup_passphrase",
        "llm_api_key",
    }
    assert all(
        stat.S_IMODE(entry.stat().st_mode) == 0o600
        for entry in credentials.iterdir()
    )
    private_key = Ed25519PrivateKey.from_private_bytes(
        base64.urlsafe_b64decode(
            (credentials / "grant_issuer_private_key").read_bytes()
        )
    )
    assert (
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        == (etc / "grant_issuer_public.pem").read_bytes()
    )

    outbound = etc / "outbound"
    outbound.mkdir(mode=0o700)
    outbound.chmod(0o700)
    (etc / "runtime.conf").write_text(
        "\n".join(
            (
                "ZHIXU_QQ_ACCOUNT=qq-synthetic",
                "ZHIXU_ADMIN_WEB_ENABLED=true",
                "ZHIXU_PASSKEY_RP_ID=assistant.example.invalid",
                "ZHIXU_PASSKEY_ORIGIN=https://assistant.example.invalid",
            )
        ),
        encoding="utf-8",
    )
    (etc / "runtime.conf").chmod(0o644)
    (etc / "outbound-accounts.json").write_text("[]", encoding="utf-8")
    (etc / "outbound-accounts.json").chmod(0o644)
    preflight = verify_deployment_configuration(
        PreflightPaths(
            runtime_config=etc / "runtime.conf",
            credentials_directory=credentials,
            grant_public_key=etc / "grant_issuer_public.pem",
            outbound_accounts=etc / "outbound-accounts.json",
            outbound_directory=outbound,
        ),
        expected_owner_uid=os.getuid(),
        expected_owner_gid=os.getgid(),
    )
    assert preflight.credential_files == 12


def test_wrong_passphrase_and_existing_credentials_do_not_overwrite(
    tmp_path: Path,
) -> None:
    bundle = _create_complete_bundle(tmp_path)
    etc = _bootstrap_etc(tmp_path)
    bootstrap = (etc / "credentials" / "llm_api_key").read_bytes()

    with pytest.raises(PermissionError, match="decryption failed"):
        install_deployment_bundle(
            bundle,
            passphrase="wrong synthetic phrase",
            etc_directory=etc,
            expected_owner_uid=os.getuid(),
            expected_owner_gid=os.getgid(),
        )
    assert (etc / "credentials" / "llm_api_key").read_bytes() == bootstrap
    assert not (etc / "grant_issuer_public.pem").exists()

    extra = etc / "credentials" / "already_present"
    extra.write_text("preserve me", encoding="utf-8")
    extra.chmod(0o600)
    with pytest.raises(FileExistsError, match="already provisioned"):
        _install(bundle, etc)
    assert extra.read_text(encoding="utf-8") == "preserve me"


def test_bundle_symlink_and_insecure_bootstrap_are_rejected(
    tmp_path: Path,
) -> None:
    bundle = _create_complete_bundle(tmp_path)
    link = tmp_path / "bundle-link.zxe"
    link.symlink_to(bundle)
    etc = _bootstrap_etc(tmp_path)

    with pytest.raises(ValueError, match="file is invalid"):
        _install(link, etc)

    llm_key = etc / "credentials" / "llm_api_key"
    llm_key.chmod(0o644)
    with pytest.raises(FileExistsError, match="not empty and secure"):
        _install(bundle, etc)


def test_legacy_bundle_requires_and_creates_complete_recovery_bundle(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy.zxe"
    _legacy_bundle(legacy)
    etc = _bootstrap_etc(tmp_path / "first")

    with pytest.raises(ValueError, match="requires a recovery output"):
        _install(legacy, etc)

    recovery = tmp_path / "offline" / "complete.zxe"
    result = _install(legacy, etc, recovery_output=recovery)
    assert result.recovery_bundle_created is True
    assert stat.S_IMODE(recovery.stat().st_mode) == 0o600
    raw_recovery = recovery.read_bytes()
    assert _APP_ID.encode() not in raw_recovery
    assert _CLIENT_SECRET.encode() not in raw_recovery
    assert _PASSPHRASE.encode() not in raw_recovery

    second_etc = _bootstrap_etc(tmp_path / "second")
    second_result = _install(recovery, second_etc)
    assert second_result.recovery_bundle_created is False
    first_credentials = etc / "credentials"
    second_credentials = second_etc / "credentials"
    for first in first_credentials.iterdir():
        assert first.read_bytes() == (second_credentials / first.name).read_bytes()
    assert (etc / "grant_issuer_public.pem").read_bytes() == (
        second_etc / "grant_issuer_public.pem"
    ).read_bytes()


def test_complete_bundle_refuses_unnecessary_recovery_output(
    tmp_path: Path,
) -> None:
    bundle = _create_complete_bundle(tmp_path)
    etc = _bootstrap_etc(tmp_path)

    with pytest.raises(ValueError, match="does not need"):
        _install(
            bundle,
            etc,
            recovery_output=tmp_path / "unexpected-recovery.zxe",
        )
