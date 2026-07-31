"""Loopback-only HTTP runtime for the private administration API."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import signal
import threading
from collections.abc import Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from zhixu.adapters.channels import (
    ChannelRegistry,
    InboundReceiptStore,
    OutboundTargetStore,
    RegisteredChannel,
)
from zhixu.adapters.llm import OpenAICompatibleLLM
from zhixu.adapters.storage.sqlite import (
    AdminCredentialStore,
    AdminReadStore,
    AdminSessionStore,
    AgendaRepository,
    ChannelRouteStore,
    Database,
    GrantRepository,
    IdentityLinkStore,
    NoteRepository,
    ReminderRepository,
    SQLiteLLMUsage,
    TaskRepository,
    UserRepository,
)
from zhixu.adapters.web import AdminAPI, AdminResponse, HealthRegistry
from zhixu.adapters.web.internal_channel import InternalChannelAPI
from zhixu.application import (
    AssistantEngine,
    LLMGateway,
    ModelIntentClassifier,
    RuleIntentRouter,
    ZhixuServices,
)
from zhixu.channels import ChannelCapabilities
from zhixu.delivery import OutboxStore, QuotaManager, QuotaRule
from zhixu.delivery.quota import QuotaWindow
from zhixu.domain import PolicyEngine
from zhixu.ports import LLMBudgetLimit, SystemClock
from zhixu.security import FieldCipher, LLMEgressPolicy, OpaqueReferenceFactory
from zhixu.vault_client import CapabilityGrantIssuer, UnixVaultClient

from .common import configure_logging, read_key_file, read_text_credential
from .probes import loopback_http_available, vault_available

MAX_BODY_BYTES = 1_048_576


class CompositePrivateAPI:
    def __init__(
        self,
        admin: AdminAPI,
        internal: InternalChannelAPI,
        *,
        admin_enabled: bool = True,
    ) -> None:
        self.admin = admin
        self.internal = internal
        self.admin_enabled = admin_enabled

    def dispatch(
        self,
        method: str,
        target: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> AdminResponse:
        path = target.split("?", 1)[0].rstrip("/") or "/"
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        if path.startswith("/internal/"):
            return self.internal.dispatch(
                method.upper(),
                path,
                headers=normalized,
                body=body,
            )
        if path in {"/health/live", "/health/ready"}:
            return self.admin.dispatch(method, target, headers=headers, body=body)
        if not self.admin_enabled:
            return AdminResponse(
                404,
                {
                    "error": {
                        "code": "not_found",
                        "message": "resource not found",
                    }
                },
            )
        return self.admin.dispatch(method, target, headers=headers, body=body)


class PrivateHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], api: CompositePrivateAPI) -> None:
        self.api = api
        super().__init__(address, PrivateRequestHandler)


class PrivateRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Zhixu"
    sys_version = ""

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def log_message(self, _format: str, *_args: object) -> None:
        """Paths can contain opaque resource refs, so default access logging is disabled."""

    def _dispatch(self) -> None:
        if self.headers.get("Transfer-Encoding"):
            self._send(
                AdminResponse(
                    400,
                    {"error": {"code": "invalid_request", "message": "chunked bodies rejected"}},
                )
            )
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = MAX_BODY_BYTES + 1
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(
                AdminResponse(
                    413,
                    {"error": {"code": "body_too_large", "message": "request body is too large"}},
                )
            )
            return
        body = self.rfile.read(length) if length else b""
        server = self.server
        assert isinstance(server, PrivateHTTPServer)
        response = server.api.dispatch(
            self.command,
            self.path,
            headers={key: value for key, value in self.headers.items()},
            body=body,
        )
        self._send(response)

    def _send(self, response: AdminResponse) -> None:
        payload = json.dumps(
            response.body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        for key, value in response.headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zhixu-api")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8840)
    parser.add_argument("--database", required=True)
    parser.add_argument("--field-key-file", required=True)
    parser.add_argument("--reference-key-file", required=True)
    parser.add_argument("--challenge-key-file", required=True)
    parser.add_argument("--channel-service-token-file", required=True)
    parser.add_argument("--qq-account", required=True)
    parser.add_argument("--grant-issuer-private-key-file", required=True)
    parser.add_argument("--vault-socket", default="/run/zhixu/vault/vault.sock")
    parser.add_argument("--outbound-target-database", default="")
    parser.add_argument("--outbound-field-key-file", default="")
    parser.add_argument("--outbound-accounts-file", default="")
    parser.add_argument(
        "--llm-base-url",
        default=os.environ.get("ZHIXU_LLM_BASE_URL", ""),
    )
    parser.add_argument(
        "--llm-model",
        default=os.environ.get("ZHIXU_LLM_MODEL", ""),
    )
    parser.add_argument(
        "--llm-local",
        action="store_true",
        default=_environment_flag("ZHIXU_LLM_LOCAL"),
    )
    parser.add_argument(
        "--allow-personal-llm-egress",
        action="store_true",
        default=_environment_flag("ZHIXU_ALLOW_PERSONAL_LLM_EGRESS"),
    )
    parser.add_argument(
        "--llm-web-search",
        action="store_true",
        default=_environment_flag("ZHIXU_LLM_WEB_SEARCH"),
    )
    parser.add_argument(
        "--allow-confidential-local-llm",
        action="store_true",
        default=_environment_flag("ZHIXU_ALLOW_CONFIDENTIAL_LOCAL_LLM"),
    )
    parser.add_argument(
        "--llm-health-url",
        default=os.environ.get(
            "ZHIXU_LLM_HEALTH_URL",
            "http://127.0.0.1:8841/health",
        ),
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def create_api(args: argparse.Namespace) -> CompositePrivateAPI:
    database = Database(Path(args.database))
    database.migrate()
    clock = SystemClock()
    grants = GrantRepository(database)
    policy = PolicyEngine(grants.has_grant)
    reads = AdminReadStore(database)
    services = ZhixuServices(
        agenda=AgendaRepository(database),
        tasks=TaskRepository(database),
        notes=NoteRepository(database),
        reminders=ReminderRepository(database),
        policy=policy,
        clock=clock,
    )
    references = OpaqueReferenceFactory(
        read_key_file(args.reference_key_file)
    )
    users = UserRepository(database)
    routes = ChannelRouteStore(database)
    outbound_configuration = _outbound_accounts(args.outbound_accounts_file)
    outbound_values = (
        args.outbound_target_database,
        args.outbound_field_key_file,
        args.outbound_accounts_file,
    )
    if any(outbound_values) and not all(outbound_values):
        raise ValueError("outbound target runtime configuration is incomplete")
    outbound_targets = None
    if all(outbound_values):
        outbound_database = Database(Path(args.outbound_target_database))
        outbound_database.migrate()
        outbound_targets = OutboundTargetStore(
            outbound_database,
            FieldCipher(
                read_key_file(args.outbound_field_key_file, exact_bytes=32)
            ),
            references,
        )
    classifier = None
    llm_gateway = None
    if bool(args.llm_base_url) != bool(args.llm_model):
        raise ValueError("LLM base URL and model must be configured together")
    if args.llm_base_url:
        credential_directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
        llm_key_path = (
            Path(credential_directory) / "llm_api_key"
            if credential_directory
            else None
        )
        llm_key = (
            read_text_credential(llm_key_path)
            if (
                llm_key_path is not None
                and llm_key_path.is_file()
                and llm_key_path.stat().st_size > 0
            )
            else ""
        )
        llm_gateway = LLMGateway(
            client=OpenAICompatibleLLM(
                provider_ref="configured",
                base_url=args.llm_base_url,
                api_key=llm_key,
                is_local=args.llm_local,
            ),
            usage=SQLiteLLMUsage(database, clock),
            clock=clock,
            egress=LLMEgressPolicy(
                allow_personal_to_external=args.allow_personal_llm_egress,
                allow_confidential_to_local=args.allow_confidential_local_llm,
            ),
            limits=(
                LLMBudgetLimit("day", 200, 200_000, 200_000),
                LLMBudgetLimit("month", 3_000, 3_000_000, 3_000_000),
            ),
        )
        classifier = ModelIntentClassifier(
            llm_gateway,
            model=args.llm_model,
        )
    optional_probes = {
        "vault": lambda: vault_available(args.vault_socket),
    }
    if args.llm_base_url:
        optional_probes["llm"] = lambda: loopback_http_available(args.llm_health_url)
    qq_capabilities = ChannelCapabilities(
        inbound_text=True,
        outbound_text=True,
        proactive_push=True,
        buttons=True,
        attachments=True,
        groups=True,
    )
    declared_channels = (
        RegisteredChannel(
            "qq",
            args.qq_account,
            "conversational",
            {
                "inbound_text": True,
                "outbound_text": True,
                "proactive_push": True,
                "buttons": True,
                "attachments": True,
                "voice": False,
                "groups": True,
            },
        ),
        *(item[0] for item in outbound_configuration),
    )
    admin = AdminAPI(
        services=services,
        policy=policy,
        users=users,
        grants=grants,
        sessions=AdminSessionStore(database),
        credentials=AdminCredentialStore(database),
        channel_routes=routes,
        outbox=OutboxStore(database),
        vault_client=UnixVaultClient(args.vault_socket),
        grant_issuer=CapabilityGrantIssuer.from_private_bytes(
            read_key_file(
                args.grant_issuer_private_key_file,
                exact_bytes=32,
            ),
            issuer="zhixu-auth",
        ),
        channels=ChannelRegistry(
            declared=declared_channels,
        ),
        outbound_targets=outbound_targets,
        outbound_target_kinds={
            (descriptor.channel, descriptor.channel_account): target_kind
            for descriptor, target_kind in outbound_configuration
        },
        identity_links=IdentityLinkStore(
            database,
            challenge_key=read_key_file(args.challenge_key_file),
        ),
        reads=reads,
        clock=clock,
        field_cipher=FieldCipher(
            read_key_file(args.field_key_file, exact_bytes=32)
        ),
        references=references,
        health=HealthRegistry(
            storage_probe=lambda: reads.status()["storage"] == "available",
            optional_probes=optional_probes,
        ),
    )
    internal = InternalChannelAPI(
        service_token=read_text_credential(args.channel_service_token_file),
        users=users,
        routes=routes,
        receipts=InboundReceiptStore(database, references),
        assistant=AssistantEngine(
            services=services,
            router=RuleIntentRouter(clock),
            classifier=classifier,
            llm_gateway=llm_gateway,
            llm_model=args.llm_model,
            web_search_enabled=args.llm_web_search,
        ),
        outbox=OutboxStore(database),
        quota=QuotaManager(
            database,
            (
                QuotaRule("provider", QuotaWindow.SECOND, 20),
                QuotaRule("account", QuotaWindow.MINUTE, 500),
                QuotaRule("conversation", QuotaWindow.MINUTE, 60),
                QuotaRule("user", QuotaWindow.DAY, 1000),
            ),
        ),
        references=references,
        capabilities={
            "qq": qq_capabilities,
            **{
                descriptor.channel: ChannelCapabilities(
                    outbound_text=True,
                    proactive_push=True,
                )
                for descriptor, _target_kind in outbound_configuration
            },
        },
        field_cipher=FieldCipher(
            read_key_file(args.field_key_file, exact_bytes=32)
        ),
    )
    return CompositePrivateAPI(
        admin,
        internal,
        admin_enabled=_environment_flag(
            "ZHIXU_ADMIN_WEB_ENABLED",
            default=True,
        ),
    )


def _environment_flag(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _outbound_accounts(
    path: str,
) -> tuple[tuple[RegisteredChannel, str], ...]:
    if not path:
        return ()
    raw = Path(path).read_bytes()
    if len(raw) > 64 * 1024:
        raise ValueError("outbound account configuration is too large")
    value = json.loads(raw)
    if not isinstance(value, list):
        raise ValueError("outbound account configuration must be a list")
    result: list[tuple[RegisteredChannel, str]] = []
    supported = {
        "email": "recipient",
        "wecom": "user",
        "webhook": "endpoint",
    }
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "channel",
            "channel_account",
        }:
            raise ValueError("outbound account declaration is invalid")
        channel = item["channel"]
        account = item["channel_account"]
        if (
            not isinstance(channel, str)
            or channel not in supported
            or not isinstance(account, str)
            or not account.strip()
            or len(account) > 160
            or (channel, account) in seen
        ):
            raise ValueError("outbound account declaration is invalid")
        seen.add((channel, account))
        result.append(
            (
                RegisteredChannel(
                    channel,
                    account,
                    "outbound-only",
                    {
                        "inbound_text": False,
                        "outbound_text": True,
                        "proactive_push": True,
                        "buttons": False,
                        "attachments": False,
                        "voice": False,
                        "groups": False,
                    },
                ),
                supported[channel],
            )
        )
    return tuple(result)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    try:
        address = ipaddress.ip_address(args.bind)
    except ValueError as exc:
        raise SystemExit("API bind address must be a loopback IP literal") from exc
    if not address.is_loopback or not 1 <= args.port <= 65535:
        raise SystemExit("API may only bind a valid loopback address and port")
    server = PrivateHTTPServer((str(address), args.port), create_api(args))

    def stop(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
