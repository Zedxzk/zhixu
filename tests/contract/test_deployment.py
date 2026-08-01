from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SYSTEMD = ROOT / "deploy" / "systemd"


def _unit(name: str) -> str:
    return (SYSTEMD / name).read_text(encoding="utf-8")


def test_all_long_running_services_have_the_security_baseline() -> None:
    required = {
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "RestrictSUIDSGID=yes",
        "RestrictRealtime=yes",
        "LockPersonality=yes",
        "CapabilityBoundingSet=",
    }
    long_running = sorted(SYSTEMD.glob("*.service"))
    assert long_running
    for path in long_running:
        content = path.read_text(encoding="utf-8")
        assert required <= set(content.splitlines()), path.name


def test_runtime_units_preserve_network_and_database_boundaries() -> None:
    api = _unit("zhixu-api.service")
    qq = _unit("zhixu-qq.service")
    outbound = _unit("zhixu-outbound@.service")
    vault = _unit("zhixu-vault.service")
    executor = _unit("zhixu-pat-executor.service")
    llm_proxy = _unit("zhixu-llm-proxy.service")

    assert "--bind 127.0.0.1" in api
    assert "IPAddressDeny=any" in api and "IPAddressAllow=localhost" in api
    assert "InaccessiblePaths=/var/lib/zhixu-vault" in api

    assert "--database /var/lib/zhixu/qq/qq.sqlite3" in qq
    assert "/var/lib/zhixu/zhixu.sqlite3" in qq
    assert "/var/lib/zhixu-vault" in qq

    assert "--database /var/lib/zhixu/outbound/targets.sqlite3" in outbound
    assert "/var/lib/zhixu/zhixu.sqlite3" in outbound
    assert "/var/lib/zhixu-vault" in outbound
    assert "app_reference_key" not in outbound

    assert "PrivateNetwork=yes" in vault
    assert "RestrictAddressFamilies=AF_UNIX" in vault
    assert "--socket /run/zhixu/vault/vault.sock" in vault
    assert "--executor pat=/run/zhixu/integration/pat-executor.sock" in vault
    assert (
        "--audit-checkpoint-directory /var/backups/zhixu/vault-audit"
        in vault
    )
    assert (
        "ReadWritePaths=/var/lib/zhixu-vault /run/zhixu/vault "
        "/var/backups/zhixu/vault-audit"
    ) in vault
    assert "InaccessiblePaths=/var/lib/zhixu" in vault

    assert "User=zhixu-integration" in executor
    assert "--socket /run/zhixu/integration/pat-executor.sock" in executor
    assert "ReadWritePaths=/run/zhixu/integration" in executor
    assert "--allowed-user zhixu-vault" in executor
    assert (
        "InaccessiblePaths=/var/lib/zhixu /var/lib/zhixu-vault "
        "/etc/zhixu/credentials"
    ) in executor

    assert "User=zhixu-llm" in llm_proxy
    assert "LoadCredentialEncrypted=llm_api_key:" in llm_proxy
    assert "--model deepseek-v4-flash" in llm_proxy
    assert (
        "InaccessiblePaths=/var/lib/zhixu /var/lib/zhixu-vault "
        "/etc/zhixu/credentials"
    ) in llm_proxy


def test_runtime_socket_directories_cannot_be_replaced_by_clients() -> None:
    bootstrap = (
        ROOT / "scripts" / "deploy" / "00_bootstrap_root.sh"
    ).read_text(encoding="utf-8")
    assert "0755 /run/zhixu" in bootstrap
    assert (
        "zhixu-vault -g zhixu-vault-client -m 0750 /run/zhixu/vault"
        in bootstrap
    )
    assert (
        "zhixu-integration -g zhixu-vault-client -m 0750 "
        "/run/zhixu/integration"
    ) in bootstrap
    assert "0770 /run/zhixu" not in bootstrap


def test_release_sync_applies_the_catch_all_exclusion_last() -> None:
    sync = (
        ROOT / "scripts" / "deploy" / "05_sync.sh"
    ).read_text(encoding="utf-8")
    catch_all = sync.index("--exclude='*'")
    assert sync.index("--include='/pyproject.toml'") < catch_all
    for directory, descendant_pattern in (
        ("src", "src/***"),
        ("scripts", "scripts/deploy/***"),
        ("deploy", "deploy/***"),
    ):
        parent = sync.index(f"--include='/{directory}/'")
        descendants = sync.index(f"--include='/{descendant_pattern}'")
        assert parent < descendants < catch_all


def test_public_release_is_executable_but_not_writable_by_service_users() -> None:
    bootstrap = (
        ROOT / "scripts" / "deploy" / "00_bootstrap_root.sh"
    ).read_text(encoding="utf-8")
    sync = (
        ROOT / "scripts" / "deploy" / "05_sync.sh"
    ).read_text(encoding="utf-8")
    install = (
        ROOT / "scripts" / "deploy" / "10_install.sh"
    ).read_text(encoding="utf-8")

    assert "-m 0755 /opt/zhixu" in bootstrap
    assert "-m 0755 /opt/zhixu/releases" in bootstrap
    assert "u=rwX,g=rX,o=rX" in sync
    assert "u=rwX,g=rX,o=rX" in install
    assert "o= " not in sync
    assert "o= " not in install


def test_every_systemd_entrypoint_exists_in_the_package_manifest() -> None:
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = set(manifest["project"]["scripts"])
    expected = {
        "zhixu-api",
        "zhixu-backup",
        "zhixu-llm-proxy",
        "zhixu-outbound",
        "zhixu-pat-executor",
        "zhixu-qq",
        "zhixu-vault-backup",
        "zhixu-vault-runtime",
        "zhixu-worker",
    }
    assert expected <= scripts
    for unit in SYSTEMD.glob("*.service"):
        content = unit.read_text(encoding="utf-8")
        command = next(
            (
                line.split("/venv/bin/", 1)[1].split(" ", 1)[0]
                for line in content.splitlines()
                if line.startswith("ExecStart=") and "/venv/bin/" in line
            ),
            "",
        )
        assert command in scripts, unit.name


def test_activation_and_rollback_include_every_stateful_runtime() -> None:
    activation = (
        ROOT / "scripts" / "deploy" / "20_activate_root.sh"
    ).read_text(encoding="utf-8")
    rollback = (
        ROOT / "scripts" / "deploy" / "30_rollback_root.sh"
    ).read_text(encoding="utf-8")
    for service in (
        "zhixu-api.service",
        "zhixu-worker.service",
        "zhixu-qq.service",
        "zhixu-llm-proxy.service",
        "zhixu-pat-executor.service",
        "zhixu-vault.service",
    ):
        assert service in activation
        assert service in rollback
    assert activation.index('/venv/bin/zhixu" preflight') < activation.index(
        "ln -sfn"
    )
    assert "databases were not overwritten" in rollback


def test_root_verifier_checks_runtime_without_printing_private_state() -> None:
    verifier = (
        ROOT / "scripts" / "deploy" / "40_verify_root.sh"
    ).read_text(encoding="utf-8")
    for unit in (
        "zhixu-api.service",
        "zhixu-worker.service",
        "zhixu-qq.service",
        "zhixu-llm-proxy.service",
        "zhixu-pat-executor.service",
        "zhixu-vault.service",
        "zhixu-backup.timer",
        "zhixu-vault-backup.timer",
    ):
        assert unit in verifier
    assert "127.0.0.1:8840" in verifier
    assert "0\\.0\\.0\\.0" in verifier
    assert "zhixu-vault:zhixu-vault-client:750" in verifier
    assert "zhixu-integration:zhixu-vault-client:750" in verifier
    assert 'runuser --user zhixu-vault -- "${release}/venv/bin/zhixu-vault"' in verifier
    assert "deployment=ready" in verifier
    assert "journalctl" not in verifier


def test_backup_unit_covers_every_persistent_database_boundary() -> None:
    backup = _unit("zhixu-backup.service")
    for database, destination in (
        ("/var/lib/zhixu/zhixu.sqlite3", "/var/backups/zhixu/application"),
        ("/var/lib/zhixu/qq/qq.sqlite3", "/var/backups/zhixu/qq"),
        (
            "/var/lib/zhixu/outbound/targets.sqlite3",
            "/var/backups/zhixu/outbound",
        ),
    ):
        assert f"--database {database}" in backup
        assert f"--destination {destination}" in backup
    assert "ReadWritePaths=/var/lib/zhixu " in backup
    assert "ReadOnlyPaths=/var/lib/zhixu" not in backup
    assert "/var/lib/zhixu-vault" in backup
