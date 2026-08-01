from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from zhixu.runtime.preflight import (
    PreflightFailure,
    PreflightPaths,
    verify_deployment_configuration,
)


def _write(path: Path, payload: str | bytes, mode: int) -> None:
    data = payload.encode() if isinstance(payload, str) else payload
    path.write_bytes(data)
    path.chmod(mode)


def _configuration(tmp_path: Path) -> PreflightPaths:
    etc = tmp_path / "etc" / "zhixu"
    credentials = etc / "credentials"
    outbound = etc / "outbound"
    credentials.mkdir(parents=True, mode=0o700)
    outbound.mkdir(mode=0o700)
    credentials.chmod(0o700)
    outbound.chmod(0o700)
    _write(
        etc / "runtime.conf",
        "\n".join(
            (
                "ZHIXU_QQ_ACCOUNT=qq-synthetic",
                "ZHIXU_ADMIN_WEB_ENABLED=true",
                "ZHIXU_PASSKEY_RP_ID=assistant.example.invalid",
                "ZHIXU_PASSKEY_ORIGIN=https://assistant.example.invalid",
            )
        ),
        0o644,
    )
    _write(etc / "outbound-accounts.json", "[]", 0o644)
    _write(
        etc / "grant_issuer_public.pem",
        Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
        0o644,
    )
    encoded_key = base64.urlsafe_b64encode(b"K" * 32)
    for name in (
        "app_field_key",
        "qq_field_key",
        "outbound_field_key",
        "app_reference_key",
        "identity_challenge_key",
        "grant_issuer_private_key",
    ):
        _write(credentials / name, encoded_key, 0o600)
    values = {
        "channel_service_token": "synthetic-channel-service-token-value",
        "qq_app_id": "synthetic-app-id",
        "qq_client_secret": "synthetic-client-secret",  # pragma: allowlist secret
        "application_backup_passphrase": "synthetic application backup phrase",
        "vault_backup_passphrase": "synthetic vault backup phrase",
        "llm_api_key": "",
    }
    for name, value in values.items():
        _write(credentials / name, value, 0o600)
    return PreflightPaths(
        runtime_config=etc / "runtime.conf",
        credentials_directory=credentials,
        grant_public_key=etc / "grant_issuer_public.pem",
        outbound_accounts=etc / "outbound-accounts.json",
        outbound_directory=outbound,
    )


def test_preflight_validates_fixed_files_without_returning_values(
    tmp_path: Path,
) -> None:
    paths = _configuration(tmp_path)
    result = verify_deployment_configuration(
        paths,
        expected_owner_uid=os.getuid(),
        expected_owner_gid=os.getgid(),
    )

    assert result.credential_files == 12
    assert result.outbound_accounts == 0
    assert "synthetic" not in repr(result)


def test_preflight_allows_headless_mode_only_without_passkey_origin(
    tmp_path: Path,
) -> None:
    paths = _configuration(tmp_path)
    _write(
        paths.runtime_config,
        "\n".join(
            (
                "ZHIXU_QQ_ACCOUNT=qq-synthetic",
                "ZHIXU_ADMIN_WEB_ENABLED=false",
            )
        ),
        0o644,
    )
    result = verify_deployment_configuration(
        paths,
        expected_owner_uid=os.getuid(),
        expected_owner_gid=os.getgid(),
    )
    assert result.credential_files == 12

    with paths.runtime_config.open("a", encoding="utf-8") as output:
        output.write("\nZHIXU_PASSKEY_ORIGIN=https://should-not-be-used.example\n")
    with pytest.raises(PreflightFailure) as configured:
        verify_deployment_configuration(
            paths,
            expected_owner_uid=os.getuid(),
            expected_owner_gid=os.getgid(),
        )
    assert configured.value.code == "passkey_disabled_configuration"


def test_preflight_accepts_http_only_for_a_localhost_ssh_tunnel(
    tmp_path: Path,
) -> None:
    paths = _configuration(tmp_path)
    original = paths.runtime_config.read_text(encoding="utf-8")
    paths.runtime_config.write_text(
        original.replace(
            "ZHIXU_PASSKEY_RP_ID=assistant.example.invalid\n"
            "ZHIXU_PASSKEY_ORIGIN=https://assistant.example.invalid",
            "ZHIXU_PASSKEY_RP_ID=localhost\n"
            "ZHIXU_PASSKEY_ORIGIN=http://localhost:8840",
        ),
        encoding="utf-8",
    )
    verify_deployment_configuration(
        paths,
        expected_owner_uid=os.getuid(),
        expected_owner_gid=os.getgid(),
    )

    for rp_id, origin in (
        ("127.0.0.1", "http://127.0.0.1:8840"),
        ("assistant.example.invalid", "http://assistant.example.invalid"),
        ("localhost", "http://localhost.example.invalid:8840"),
    ):
        paths.runtime_config.write_text(
            original.replace(
                "ZHIXU_PASSKEY_RP_ID=assistant.example.invalid\n"
                "ZHIXU_PASSKEY_ORIGIN=https://assistant.example.invalid",
                f"ZHIXU_PASSKEY_RP_ID={rp_id}\n"
                f"ZHIXU_PASSKEY_ORIGIN={origin}",
            ),
            encoding="utf-8",
        )
        with pytest.raises(PreflightFailure) as insecure:
            verify_deployment_configuration(
                paths,
                expected_owner_uid=os.getuid(),
                expected_owner_gid=os.getgid(),
            )
        assert insecure.value.code == "passkey_origin_invalid"


def test_preflight_rejects_loose_permissions_symlinks_and_unknown_config(
    tmp_path: Path,
) -> None:
    paths = _configuration(tmp_path)
    target = paths.credentials_directory / "qq_client_secret"
    target.chmod(0o644)
    with pytest.raises(PreflightFailure) as insecure:
        verify_deployment_configuration(
            paths,
            expected_owner_uid=os.getuid(),
            expected_owner_gid=os.getgid(),
        )
    assert insecure.value.code == "credential_qq_client_secret_insecure"
    target.chmod(0o600)

    target.unlink()
    target.symlink_to(paths.credentials_directory / "qq_app_id")
    with pytest.raises(PreflightFailure) as symlink:
        verify_deployment_configuration(
            paths,
            expected_owner_uid=os.getuid(),
            expected_owner_gid=os.getgid(),
        )
    assert symlink.value.code == "credential_qq_client_secret_insecure"

    target.unlink()
    _write(target, "synthetic-client-secret", 0o600)
    with paths.runtime_config.open("a", encoding="utf-8") as output:
        output.write("\nPRIVATE_TOKEN=must-not-be-accepted\n")  # pragma: allowlist secret
    with pytest.raises(PreflightFailure) as unknown:
        verify_deployment_configuration(
            paths,
            expected_owner_uid=os.getuid(),
            expected_owner_gid=os.getgid(),
        )
    assert unknown.value.code == "runtime_config_invalid"


def test_preflight_accepts_a_qq_bot_display_name_but_rejects_a_leading_at(
    tmp_path: Path,
) -> None:
    paths = _configuration(tmp_path)
    original = paths.runtime_config.read_text(encoding="utf-8")

    with paths.runtime_config.open("a", encoding="utf-8") as output:
        output.write("\nZHIXU_QQ_BOT_DISPLAY_NAME=SyntheticBotName\n")  # pragma: allowlist secret
    verify_deployment_configuration(
        paths,
        expected_owner_uid=os.getuid(),
        expected_owner_gid=os.getgid(),
    )

    paths.runtime_config.write_text(
        original + "\nZHIXU_QQ_BOT_DISPLAY_NAME=@SyntheticBotName\n",
        encoding="utf-8",
    )
    with pytest.raises(PreflightFailure) as leading_at:
        verify_deployment_configuration(
            paths,
            expected_owner_uid=os.getuid(),
            expected_owner_gid=os.getgid(),
        )
    assert leading_at.value.code == "runtime_qq_display_name_invalid"


def test_preflight_requires_declared_outbound_credentials_to_match(
    tmp_path: Path,
) -> None:
    paths = _configuration(tmp_path)
    declaration = [
        {
            "channel": "email",
            "channel_account": "email-synthetic",
        }
    ]
    _write(paths.outbound_accounts, json.dumps(declaration), 0o644)
    credential = {
        "channel": "email",
        "channel_account": "email-synthetic",
        "host": "smtp.example.invalid",
        "port": 465,
        "sender": "sender@example.invalid",
        "username": "synthetic-user",
        "password": "synthetic-password",  # pragma: allowlist secret
        "implicit_tls": True,
    }
    _write(
        paths.outbound_directory / "email-synthetic.json",
        json.dumps(credential),
        0o600,
    )

    result = verify_deployment_configuration(
        paths,
        expected_owner_uid=os.getuid(),
        expected_owner_gid=os.getgid(),
    )
    assert result.outbound_accounts == 1

    declaration[0]["channel_account"] = "different-synthetic"
    _write(paths.outbound_accounts, json.dumps(declaration), 0o644)
    with pytest.raises(PreflightFailure) as mismatch:
        verify_deployment_configuration(
            paths,
            expected_owner_uid=os.getuid(),
            expected_owner_gid=os.getgid(),
        )
    assert mismatch.value.code == "outbound_account_mismatch"
