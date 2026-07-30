"""SSRF-resistant outbound webhook adapter."""

from .outbound import (
    SystemResolver,
    WebhookCredentials,
    WebhookEgressPolicy,
    WebhookOutboundAdapter,
)

__all__ = [
    "SystemResolver",
    "WebhookCredentials",
    "WebhookEgressPolicy",
    "WebhookOutboundAdapter",
]
