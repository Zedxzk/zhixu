"""Fail-closed deployment configuration checks that never print secret values."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from zhixu.adapters.llm import OpenAICompatibleLLM

from .api import _outbound_accounts
from .common import read_key_file, read_text_credential
from .outbound import configured_outbound_account

_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}")
_REQUIRED_RUNTIME = {
    "ZHIXU_ADMIN_WEB_ENABLED",
    "ZHIXU_QQ_ACCOUNT",
}
_OPTIONAL_RUNTIME = {
    "ZHIXU_PASSKEY_RP_ID",
    "ZHIXU_PASSKEY_ORIGIN",
    "ZHIXU_LLM_BASE_URL",
    "ZHIXU_LLM_MODEL",
    "ZHIXU_LLM_LOCAL",
    "ZHIXU_LLM_HEALTH_URL",
    "ZHIXU_LLM_WEB_SEARCH",
    "ZHIXU_ALLOW_PERSONAL_LLM_EGRESS",
    "ZHIXU_ALLOW_CONFIDENTIAL_LOCAL_LLM",
    "ZHIXU_QQ_BOT_DISPLAY_NAME",
}
_BOOLEAN_VALUES = {"0", "1", "false", "no", "off", "on", "true", "yes"}


class PreflightFailure(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PreflightPaths:
    runtime_config: Path
    credentials_directory: Path
    grant_public_key: Path
    outbound_accounts: Path
    outbound_directory: Path


@dataclass(frozen=True, slots=True)
class PreflightResult:
    credential_files: int
    outbound_accounts: int


def verify_deployment_configuration(
    paths: PreflightPaths,
    *,
    expected_owner_uid: int = 0,
    expected_owner_gid: int = 0,
) -> PreflightResult:
    _secure_directory(
        paths.credentials_directory,
        expected_owner_uid=expected_owner_uid,
        expected_owner_gid=expected_owner_gid,
        allowed_modes={0o700},
        code="credentials_directory_insecure",
    )
    _secure_directory(
        paths.outbound_directory,
        expected_owner_uid=expected_owner_uid,
        expected_owner_gid=expected_owner_gid,
        allowed_modes={0o700},
        code="outbound_directory_insecure",
    )
    _secure_file(
        paths.runtime_config,
        expected_owner_uid=expected_owner_uid,
        expected_owner_gid=expected_owner_gid,
        allowed_modes={0o640, 0o644},
        maximum_bytes=64 * 1024,
        code="runtime_config_insecure",
    )
    _secure_file(
        paths.grant_public_key,
        expected_owner_uid=expected_owner_uid,
        expected_owner_gid=expected_owner_gid,
        allowed_modes={0o644},
        maximum_bytes=16 * 1024,
        code="grant_public_key_insecure",
    )
    _secure_file(
        paths.outbound_accounts,
        expected_owner_uid=expected_owner_uid,
        expected_owner_gid=expected_owner_gid,
        allowed_modes={0o640, 0o644},
        maximum_bytes=64 * 1024,
        code="outbound_accounts_insecure",
    )

    runtime = _runtime_configuration(paths.runtime_config)
    _validate_passkey(runtime)
    _validate_llm(runtime)
    credentials = _credentials(
        paths.credentials_directory,
        expected_owner_uid=expected_owner_uid,
        expected_owner_gid=expected_owner_gid,
    )
    _grant_public_key(paths.grant_public_key)

    try:
        declared = {
            (descriptor.channel, descriptor.channel_account)
            for descriptor, _target_kind in _outbound_accounts(
                str(paths.outbound_accounts)
            )
        }
    except Exception as exc:
        raise PreflightFailure("outbound_accounts_invalid") from exc
    configured: set[tuple[str, str]] = set()
    try:
        entries = sorted(paths.outbound_directory.iterdir())
    except OSError as exc:
        raise PreflightFailure("outbound_directory_unreadable") from exc
    for entry in entries:
        if entry.suffix != ".json":
            raise PreflightFailure("outbound_directory_contains_unknown_file")
        _secure_file(
            entry,
            expected_owner_uid=expected_owner_uid,
            expected_owner_gid=expected_owner_gid,
            allowed_modes={0o600},
            maximum_bytes=64 * 1024,
            code="outbound_credential_insecure",
        )
        try:
            account = configured_outbound_account(entry)
        except Exception as exc:
            raise PreflightFailure("outbound_credential_invalid") from exc
        if account in configured:
            raise PreflightFailure("outbound_credential_duplicated")
        configured.add(account)
    if configured != declared:
        raise PreflightFailure("outbound_account_mismatch")
    return PreflightResult(credentials, len(declared))


def _credentials(
    directory: Path,
    *,
    expected_owner_uid: int,
    expected_owner_gid: int,
) -> int:
    key_specs = {
        "app_field_key": 32,
        "qq_field_key": 32,
        "outbound_field_key": 32,
        "grant_issuer_private_key": 32,
    }
    minimum_key_specs = {
        "app_reference_key",
        "identity_challenge_key",
    }
    text_specs = {
        "channel_service_token": 32,
        "qq_app_id": 1,
        "qq_client_secret": 1,
        "application_backup_passphrase": 12,
        "vault_backup_passphrase": 12,
    }
    count = 0
    for name, length in key_specs.items():
        path = directory / name
        _credential_file(
            path,
            name,
            expected_owner_uid,
            expected_owner_gid,
        )
        try:
            read_key_file(path, exact_bytes=length)
        except Exception as exc:
            raise PreflightFailure(f"credential_{name}_invalid") from exc
        count += 1
    for name in minimum_key_specs:
        path = directory / name
        _credential_file(
            path,
            name,
            expected_owner_uid,
            expected_owner_gid,
        )
        try:
            read_key_file(path)
        except Exception as exc:
            raise PreflightFailure(f"credential_{name}_invalid") from exc
        count += 1
    for name, minimum in text_specs.items():
        path = directory / name
        _credential_file(
            path,
            name,
            expected_owner_uid,
            expected_owner_gid,
        )
        try:
            value = read_text_credential(path)
            if len(value) < minimum:
                raise ValueError("credential is too short")
        except Exception as exc:
            raise PreflightFailure(f"credential_{name}_invalid") from exc
        count += 1

    llm_key = directory / "llm_api_key"
    _credential_file(
        llm_key,
        "llm_api_key",
        expected_owner_uid,
        expected_owner_gid,
    )
    try:
        value = llm_key.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise PreflightFailure("credential_llm_api_key_invalid") from exc
    if len(value) > 4096 or "\0" in value:
        raise PreflightFailure("credential_llm_api_key_invalid")
    return count + 1


def _credential_file(
    path: Path,
    name: str,
    expected_owner_uid: int,
    expected_owner_gid: int,
) -> None:
    _secure_file(
        path,
        expected_owner_uid=expected_owner_uid,
        expected_owner_gid=expected_owner_gid,
        allowed_modes={0o600},
        maximum_bytes=16 * 1024,
        code=f"credential_{name}_insecure",
    )


def _runtime_configuration(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PreflightFailure("runtime_config_invalid") from exc
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (
            not separator
            or key not in _REQUIRED_RUNTIME | _OPTIONAL_RUNTIME
            or key in result
            or "\0" in value
            or "\n" in value
        ):
            raise PreflightFailure("runtime_config_invalid")
        result[key] = value
    if not result.keys() >= _REQUIRED_RUNTIME:
        raise PreflightFailure("runtime_config_incomplete")
    account = result["ZHIXU_QQ_ACCOUNT"]
    if _ACCOUNT.fullmatch(account) is None:
        raise PreflightFailure("runtime_qq_account_invalid")
    for key in {
        "ZHIXU_ADMIN_WEB_ENABLED",
        "ZHIXU_LLM_LOCAL",
        "ZHIXU_LLM_WEB_SEARCH",
        "ZHIXU_ALLOW_PERSONAL_LLM_EGRESS",
        "ZHIXU_ALLOW_CONFIDENTIAL_LOCAL_LLM",
    } & result.keys():
        if result[key].lower() not in _BOOLEAN_VALUES:
            raise PreflightFailure("runtime_boolean_invalid")
    display_name = result.get("ZHIXU_QQ_BOT_DISPLAY_NAME", "")
    if display_name and (len(display_name) > 160 or display_name.startswith("@")):
        # The name is matched behind a literal @, so it must not carry its own.
        raise PreflightFailure("runtime_qq_display_name_invalid")
    return result


def _validate_passkey(runtime: dict[str, str]) -> None:
    enabled = runtime["ZHIXU_ADMIN_WEB_ENABLED"].lower() in {
        "1",
        "on",
        "true",
        "yes",
    }
    rp_id = runtime.get("ZHIXU_PASSKEY_RP_ID", "").strip().rstrip(".").lower()
    configured_origin = runtime.get("ZHIXU_PASSKEY_ORIGIN", "").strip()
    if not enabled:
        if rp_id or configured_origin:
            raise PreflightFailure("passkey_disabled_configuration")
        return
    if not rp_id or not configured_origin:
        raise PreflightFailure("passkey_configuration_incomplete")
    try:
        ascii_rp_id = rp_id.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise PreflightFailure("passkey_origin_invalid") from exc
    origin = urlsplit(configured_origin)
    hostname = (origin.hostname or "").rstrip(".").lower()
    loopback_tunnel_origin = (
        origin.scheme == "http"
        and hostname == "localhost"
        and ascii_rp_id == "localhost"
    )
    if (
        not ascii_rp_id
        or hostname != ascii_rp_id
        or (origin.scheme != "https" and not loopback_tunnel_origin)
        or origin.username is not None
        or origin.password is not None
        or origin.path not in {"", "/"}
        or origin.query
        or origin.fragment
    ):
        raise PreflightFailure("passkey_origin_invalid")


def _validate_llm(runtime: dict[str, str]) -> None:
    base_url = runtime.get("ZHIXU_LLM_BASE_URL", "")
    model = runtime.get("ZHIXU_LLM_MODEL", "")
    if bool(base_url) != bool(model):
        raise PreflightFailure("llm_configuration_incomplete")
    web_search = runtime.get("ZHIXU_LLM_WEB_SEARCH", "").lower() in {
        "1",
        "on",
        "true",
        "yes",
    }
    if web_search and not base_url:
        raise PreflightFailure("llm_web_search_without_llm")
    if not base_url:
        return
    local = runtime.get("ZHIXU_LLM_LOCAL", "").lower() in {"1", "on", "true", "yes"}
    try:
        OpenAICompatibleLLM(
            provider_ref="preflight",
            base_url=base_url,
            api_key="",
            is_local=local,
        )
    except Exception as exc:
        raise PreflightFailure("llm_endpoint_invalid") from exc
    health_url = runtime.get("ZHIXU_LLM_HEALTH_URL", "")
    if health_url:
        parsed = urlsplit(health_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise PreflightFailure("llm_health_endpoint_invalid")


def _grant_public_key(path: Path) -> None:
    try:
        value = serialization.load_pem_public_key(path.read_bytes())
    except Exception as exc:
        raise PreflightFailure("grant_public_key_invalid") from exc
    if not isinstance(value, Ed25519PublicKey):
        raise PreflightFailure("grant_public_key_invalid")


def _secure_directory(
    path: Path,
    *,
    expected_owner_uid: int,
    expected_owner_gid: int,
    allowed_modes: set[int],
    code: str,
) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PreflightFailure(code) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_owner_uid
        or metadata.st_gid != expected_owner_gid
        or stat.S_IMODE(metadata.st_mode) not in allowed_modes
    ):
        raise PreflightFailure(code)


def _secure_file(
    path: Path,
    *,
    expected_owner_uid: int,
    expected_owner_gid: int,
    allowed_modes: set[int],
    maximum_bytes: int,
    code: str,
) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PreflightFailure(code) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_owner_uid
        or metadata.st_gid != expected_owner_gid
        or stat.S_IMODE(metadata.st_mode) not in allowed_modes
        or metadata.st_size > maximum_bytes
    ):
        raise PreflightFailure(code)


def root_owned_paths() -> PreflightPaths:
    return PreflightPaths(
        runtime_config=Path("/etc/zhixu/runtime.conf"),
        credentials_directory=Path("/etc/zhixu/credentials"),
        grant_public_key=Path("/etc/zhixu/grant_issuer_public.pem"),
        outbound_accounts=Path("/etc/zhixu/outbound-accounts.json"),
        outbound_directory=Path("/etc/zhixu/outbound"),
    )


def require_root() -> None:
    if os.geteuid() != 0:
        raise PreflightFailure("root_required")
