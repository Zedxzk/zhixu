"""Local operator CLI for setup, restore, and runtime diagnostics."""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .adapters.storage.sqlite import (
    AdminCredentialStore,
    ApplicationBackupManager,
    Database,
    UserRepository,
)
from .application.services import random_id
from .domain import (
    Action,
    CommandContext,
    EncryptedIdentifier,
    ExternalIdentity,
    PolicyEngine,
    ResourceRef,
    User,
    UserStatus,
)
from .runtime.common import read_key_file
from .runtime.preflight import (
    PreflightFailure,
    require_root,
    root_owned_paths,
    verify_deployment_configuration,
)
from .runtime.probes import vault_available
from .runtime.provision import (
    QQDeploymentCredentials,
    create_deployment_bundle,
    install_deployment_bundle,
)
from .security import FieldCipher
from .vault_client import CapabilityGrantIssuer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zhixu",
        description="Privacy-first self-hosted personal assistant",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    commands = parser.add_subparsers(dest="command")

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--api-url", default="http://127.0.0.1:8840")
    doctor.add_argument("--database", default="/var/lib/zhixu/zhixu.sqlite3")
    doctor.add_argument("--vault-socket", default="/run/zhixu/vault/vault.sock")

    commands.add_parser("preflight")

    bootstrap = commands.add_parser("bootstrap-admin")
    bootstrap.add_argument("--database", required=True)

    bootstrap_qq = commands.add_parser("bootstrap-qq-owner")
    bootstrap_qq.add_argument("--database", required=True)
    bootstrap_qq.add_argument("--field-key-file", default="")
    bootstrap_qq.add_argument("--user-id", default="owner")
    bootstrap_qq.add_argument("--display-name", default="Owner")
    bootstrap_qq.add_argument("--max-route-age-seconds", type=int, default=600)

    backup = commands.add_parser("backup")
    backup.add_argument("--database", required=True)
    backup.add_argument("--output", required=True)

    restore = commands.add_parser("restore")
    restore.add_argument("--input", required=True)
    restore.add_argument("--database", required=True)

    grant_key = commands.add_parser("generate-grant-key")
    grant_key.add_argument("--private-output", required=True)
    grant_key.add_argument("--public-output", required=True)

    create_bundle = commands.add_parser("create-deployment-bundle")
    create_bundle.add_argument("--output", required=True)

    install_bundle = commands.add_parser("install-deployment-bundle")
    install_bundle.add_argument("--bundle", required=True)
    install_bundle.add_argument("--recovery-output")
    return parser


def _require_tty() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise PermissionError("this command requires an interactive TTY")


def _doctor(args: argparse.Namespace) -> int:
    parsed = urlsplit(args.api_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        print("api=invalid storage=unknown vault=unknown")
        return 2
    api = "unavailable"
    degraded = True
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RejectRedirects(),
    )
    try:
        with opener.open(
            args.api_url.rstrip("/") + "/health/ready",
            timeout=2,
        ) as response:
            raw = response.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise ValueError("health response is too large")
            value = json.loads(raw)
            if int(response.status) == 200 and value.get("core") == "ready":
                api = "ready"
                degraded = bool(value.get("degraded"))
    except (OSError, ValueError, urllib.error.URLError):
        pass
    storage = _database_status(Path(args.database))
    vault = "available" if vault_available(args.vault_socket, timeout=2) else "unavailable"
    overall = (
        "ready"
        if api == "ready" and storage == "ok"
        else "unavailable"
    )
    if overall == "ready" and (degraded or vault != "available"):
        overall = "degraded"
    print(f"status={overall} api={api} storage={storage} vault={vault}")
    return 0 if api == "ready" and storage == "ok" else 3


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        _request: urllib.request.Request,
        _file_pointer: object,
        _code: int,
        _message: str,
        _headers: object,
        _new_url: str,
    ) -> None:
        return None


def _preflight() -> int:
    try:
        require_root()
        result = verify_deployment_configuration(root_owned_paths())
    except PreflightFailure as exc:
        print(f"preflight=failed code={exc.code}")
        return 4
    print(
        "preflight=ready "
        f"credential_files={result.credential_files} "
        f"outbound_accounts={result.outbound_accounts}"
    )
    return 0


def _database_status(path: Path) -> str:
    if not path.is_file():
        return "missing"
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=2) as connection:
            row = connection.execute("PRAGMA quick_check").fetchone()
        return "ok" if row and str(row[0]) == "ok" else "invalid"
    except sqlite3.Error:
        return "unavailable"


def _bootstrap_admin(args: argparse.Namespace) -> int:
    _require_tty()
    user_id = input("Internal admin user id: ").strip()
    display_name = input("Display name: ").strip()
    first = getpass.getpass("Admin password: ")
    second = getpass.getpass("Repeat admin password: ")
    if first != second:
        print("Passwords do not match.")
        return 2
    database = Database(args.database)
    database.migrate()
    users = UserRepository(database)
    if users.get(user_id) is None:
        now = datetime.now(UTC)
        authorization = PolicyEngine().require(
            CommandContext(actor_user_id=user_id, now=now),
            Action.CREATE,
            ResourceRef("user", user_id, user_id),
        )
        users.create(
            User(user_id, display_name, UserStatus.ACTIVE, now),
            authorization,
        )
    if not users.assign_project_admin_if_vacant(user_id, now=datetime.now(UTC)):
        raise PermissionError("a different project administrator already exists")
    AdminCredentialStore(database).set_password(
        user_id,
        first,
        now=datetime.now(UTC),
    )
    print("Admin principal initialized.")
    return 0


def _bootstrap_qq_owner(args: argparse.Namespace) -> int:
    if not 60 <= args.max_route_age_seconds <= 3600:
        raise ValueError("QQ bootstrap route age is outside the allowed range")
    database = Database(args.database)
    database.migrate()
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=args.max_route_age_seconds)
    with database.connect() as connection:
        user_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM users WHERE id NOT LIKE 'service:%'"
            ).fetchone()[0]
        )
        identity_count = int(
            connection.execute("SELECT COUNT(*) FROM external_identities").fetchone()[0]
        )
        candidates = connection.execute(
            """
            SELECT routes.channel_account,routes.opaque_ref,routes.last_seen_at
            FROM channel_routes AS routes
            WHERE routes.channel='qq'
              AND routes.route_kind='private'
              AND NOT EXISTS (
                  SELECT 1 FROM external_identities AS identities
                  WHERE identities.channel=routes.channel
                    AND identities.channel_account=routes.channel_account
                    AND identities.opaque_ref=routes.opaque_ref
              )
              AND EXISTS (
                  SELECT 1 FROM inbound_event_receipts AS receipts
                  WHERE receipts.channel=routes.channel
                    AND receipts.channel_account=routes.channel_account
                    AND receipts.actor_ref=routes.opaque_ref
                    AND receipts.outcome='identity_unbound'
              )
            ORDER BY routes.last_seen_at DESC
            """
        ).fetchall()
    recent = [
        row
        for row in candidates
        if datetime.fromisoformat(str(row["last_seen_at"])) >= cutoff
    ]
    if user_count or identity_count:
        raise PermissionError("headless QQ bootstrap is already closed")
    if len(recent) != 1:
        raise PermissionError("headless QQ bootstrap requires exactly one recent private route")

    credential_directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
    field_key_file = args.field_key_file or (
        str(Path(credential_directory) / "app_field_key")
        if credential_directory
        else ""
    )
    if not field_key_file:
        raise ValueError("application field key credential is unavailable")
    route = recent[0]
    account = str(route["channel_account"])
    opaque_ref = str(route["opaque_ref"])
    identity_id = random_id("identity")
    context = CommandContext(actor_user_id=args.user_id, now=now)
    policy = PolicyEngine()
    user = User(args.user_id, args.display_name, UserStatus.ACTIVE, now)
    user_authorization = policy.require(
        context,
        Action.CREATE,
        ResourceRef("user", args.user_id, args.user_id),
    )
    identity = ExternalIdentity(
        identity_id,
        args.user_id,
        "qq",
        account,
        EncryptedIdentifier(
            FieldCipher(
                read_key_file(field_key_file, exact_bytes=32)
            ).encrypt(
                opaque_ref,
                context=f"external-identity:qq:{account}:{opaque_ref}",
            )
        ),
        opaque_ref,
        now,
    )
    identity_authorization = policy.require(
        context,
        Action.CREATE,
        ResourceRef(
            "external_identity",
            identity_id,
            args.user_id,
        ),
    )
    users = UserRepository(database)
    users.create(user, user_authorization)
    users.bind_identity(identity, identity_authorization)
    if not users.assign_project_admin_if_vacant(args.user_id, now=now):
        raise PermissionError("a different project administrator already exists")
    print("Headless QQ owner initialized.")
    return 0


def _backup(args: argparse.Namespace) -> int:
    _require_tty()
    first = getpass.getpass("Backup passphrase: ")
    second = getpass.getpass("Repeat backup passphrase: ")
    if first != second:
        print("Passphrases do not match.")
        return 2
    ApplicationBackupManager(Database(args.database)).create(
        args.output,
        backup_passphrase=first,
    )
    print("Encrypted application backup created.")
    return 0


def _restore(args: argparse.Namespace) -> int:
    _require_tty()
    ApplicationBackupManager.restore(
        args.input,
        args.database,
        backup_passphrase=getpass.getpass("Backup passphrase: "),
    )
    print("Application backup restored and verified.")
    return 0


def _generate_grant_key(args: argparse.Namespace) -> int:
    private_path = Path(args.private_output)
    public_path = Path(args.public_output)
    if private_path == public_path:
        raise ValueError("grant key output paths must be different")
    issuer = CapabilityGrantIssuer.generate("zhixu-auth")
    private_payload = base64.urlsafe_b64encode(issuer.private_bytes()) + b"\n"
    private_fd = os.open(
        private_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    public_created = False
    try:
        with os.fdopen(private_fd, "wb") as private_file:
            private_file.write(private_payload)
        os.chmod(private_path, 0o600)
        public_fd = os.open(
            public_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o644,
        )
        public_created = True
        with os.fdopen(public_fd, "wb") as public_file:
            public_file.write(issuer.public_pem())
        os.chmod(public_path, 0o644)
    except Exception:
        private_path.unlink(missing_ok=True)
        if public_created:
            public_path.unlink(missing_ok=True)
        raise
    print("Grant issuer key pair created.")
    return 0


def _create_deployment_bundle(args: argparse.Namespace) -> int:
    _require_tty()
    app_id = input("QQ application id: ").strip()
    qq_credential = getpass.getpass("QQ client secret: ")
    first = getpass.getpass("Deployment bundle passphrase: ")
    second = getpass.getpass("Repeat deployment bundle passphrase: ")
    if first != second:
        print("Passphrases do not match.")
        return 2
    create_deployment_bundle(
        args.output,
        QQDeploymentCredentials(app_id, qq_credential),
        passphrase=first,
    )
    print("Encrypted deployment bundle created.")
    return 0


def _install_deployment_bundle(args: argparse.Namespace) -> int:
    require_root()
    _require_tty()
    result = install_deployment_bundle(
        args.bundle,
        passphrase=getpass.getpass("Deployment bundle passphrase: "),
        etc_directory="/etc/zhixu",
        expected_owner_uid=0,
        expected_owner_gid=0,
        recovery_output=args.recovery_output,
    )
    print(
        "Deployment credentials installed: "
        f"files={result.credential_files} "
        f"recovery_created={str(result.recovery_bundle_created).lower()}."
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        build_parser().print_help()
        return 0
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "preflight":
        return _preflight()
    if args.command == "bootstrap-admin":
        return _bootstrap_admin(args)
    if args.command == "bootstrap-qq-owner":
        return _bootstrap_qq_owner(args)
    if args.command == "backup":
        return _backup(args)
    if args.command == "restore":
        return _restore(args)
    if args.command == "generate-grant-key":
        return _generate_grant_key(args)
    if args.command == "create-deployment-bundle":
        return _create_deployment_bundle(args)
    if args.command == "install-deployment-bundle":
        return _install_deployment_bundle(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
