"""Pinned-IP HTTPS webhooks with DNS, host, and network allowlists."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from zhixu.adapters.channels.targets import OutboundTargetStore
from zhixu.channels import ChannelCapabilities, ChannelDeliveryResult, OutboundMessage
from zhixu.domain import DataClassification
from zhixu.domain.errors import PermissionDenied, ValidationError


class Resolver(Protocol):
    def resolve(self, host: str, port: int) -> tuple[str, ...]: ...


class SystemResolver:
    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        addresses = {
            str(item[4][0])
            for item in socket.getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
        }
        return tuple(sorted(addresses))


@dataclass(frozen=True, slots=True)
class ValidatedWebhook:
    host: str
    port: int
    request_target: str
    addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WebhookEgressPolicy:
    allowed_hosts: frozenset[str]
    allowed_ip_networks: tuple[str, ...] = ()
    allowed_ports: frozenset[int] = frozenset({443})
    resolver: Resolver = field(default_factory=SystemResolver, repr=False, compare=False)

    def __post_init__(self) -> None:
        normalized = frozenset(_normalize_host(host) for host in self.allowed_hosts)
        if not normalized:
            raise ValidationError("at least one webhook host must be allowlisted")
        object.__setattr__(self, "allowed_hosts", normalized)
        networks = tuple(
            str(ipaddress.ip_network(value, strict=True))
            for value in self.allowed_ip_networks
        )
        object.__setattr__(self, "allowed_ip_networks", networks)
        if not self.allowed_ports or any(not 1 <= port <= 65535 for port in self.allowed_ports):
            raise ValidationError("webhook port allowlist is invalid")

    def validate(self, url: str) -> ValidatedWebhook:
        try:
            parsed = urlsplit(url)
            port = parsed.port or 443
        except ValueError as exc:
            raise ValidationError("webhook URL is invalid") from exc
        if parsed.scheme.lower() != "https":
            raise PermissionDenied("webhook URL must use HTTPS")
        if parsed.username or parsed.password or parsed.fragment:
            raise PermissionDenied("webhook URL credentials and fragments are forbidden")
        if parsed.hostname is None:
            raise ValidationError("webhook URL host is required")
        host = _normalize_host(parsed.hostname)
        if host not in self.allowed_hosts:
            raise PermissionDenied("webhook host is not allowlisted")
        if port not in self.allowed_ports:
            raise PermissionDenied("webhook port is not allowlisted")
        try:
            resolved = self.resolver.resolve(host, port)
        except OSError as exc:
            raise PermissionDenied("webhook host could not be safely resolved") from exc
        if not resolved:
            raise PermissionDenied("webhook host did not resolve")
        addresses: list[str] = []
        networks = tuple(ipaddress.ip_network(value) for value in self.allowed_ip_networks)
        for raw in resolved:
            try:
                address = ipaddress.ip_address(raw)
            except ValueError as exc:
                raise PermissionDenied("webhook DNS returned an invalid address") from exc
            if not address.is_global:
                raise PermissionDenied("webhook address is not globally routable")
            if networks and not any(address in network for network in networks):
                raise PermissionDenied("webhook address is outside the IP allowlist")
            addresses.append(str(address))
        path = parsed.path or "/"
        request_target = f"{path}?{parsed.query}" if parsed.query else path
        return ValidatedWebhook(host, port, request_target, tuple(sorted(set(addresses))))


class WebhookTransport(Protocol):
    def post(
        self,
        target: ValidatedWebhook,
        *,
        body: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> int: ...


class PinnedHTTPSWebhookTransport:
    """Connects only to addresses already validated by WebhookEgressPolicy."""

    def post(
        self,
        target: ValidatedWebhook,
        *,
        body: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> int:
        context = ssl.create_default_context()
        for address in target.addresses:
            connection = _PinnedHTTPSConnection(
                target.host,
                target.port,
                address=address,
                timeout=timeout,
                context=context,
            )
            try:
                connection.request(
                    "POST",
                    target.request_target,
                    body=body,
                    headers=headers,
                )
                response = connection.getresponse()
                response.read(4096)
                return int(response.status)
            except (OSError, ssl.SSLError, http.client.HTTPException):
                continue
            finally:
                connection.close()
        return 0


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        port: int,
        *,
        address: str,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_address, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


@dataclass(frozen=True, slots=True)
class WebhookCredentials:
    channel_account: str
    signing_secret: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not self.channel_account.strip() or len(self.signing_secret) < 32:
            raise ValidationError("webhook credentials are incomplete")


class WebhookOutboundAdapter:
    channel = "webhook"
    capabilities = ChannelCapabilities(
        inbound_text=False,
        outbound_text=True,
        proactive_push=True,
    )

    def __init__(
        self,
        credentials: WebhookCredentials,
        targets: OutboundTargetStore,
        policy: WebhookEgressPolicy,
        *,
        transport: WebhookTransport | None = None,
        timeout: float = 10,
    ) -> None:
        self.credentials = credentials
        self.targets = targets
        self.policy = policy
        self.transport = transport or PinnedHTTPSWebhookTransport()
        self.timeout = timeout

    @property
    def channel_account(self) -> str:
        return self.credentials.channel_account

    def send(self, message: OutboundMessage) -> ChannelDeliveryResult:
        if (
            message.channel != self.channel
            or message.channel_account != self.channel_account
        ):
            return ChannelDeliveryResult(False, False, "account_mismatch")
        if message.classification >= DataClassification.CONFIDENTIAL:
            return ChannelDeliveryResult(False, False, "classification_blocked")
        target = self.targets.resolve(
            channel=self.channel,
            channel_account=self.channel_account,
            opaque_ref=message.target_ref,
        )
        if target.kind != "endpoint":
            return ChannelDeliveryResult(False, False, "target_kind_unsupported")
        validated = self.policy.validate(target.value)
        body = json.dumps(
            {
                "event": "zhixu.notification",
                "message": {
                    "text": message.text,
                    "classification": int(message.classification),
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(
            self.credentials.signing_secret,
            timestamp.encode() + b"\n" + body,
            hashlib.sha256,
        ).hexdigest()
        status = self.transport.post(
            validated,
            body=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "zhixu-webhook/1",
                "X-Zhixu-Timestamp": timestamp,
                "X-Zhixu-Signature": f"v1={signature}",
            },
            timeout=self.timeout,
        )
        if 200 <= status < 300:
            return ChannelDeliveryResult(True, provider_code="ok")
        if 300 <= status < 400:
            return ChannelDeliveryResult(False, False, "redirect_blocked")
        return ChannelDeliveryResult(
            False,
            retryable=status == 0 or status in {408, 425, 429} or status >= 500,
            provider_code="network_unavailable" if status == 0 else f"http_{status}",
        )


def _normalize_host(value: str) -> str:
    host = value.rstrip(".").lower()
    if not host or any(character.isspace() for character in host):
        raise ValidationError("webhook host is invalid")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValidationError("webhook host is invalid") from exc
