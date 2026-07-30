"""Outbound email adapter."""

from .outbound import (
    EmailCredentials,
    EmailOutboundAdapter,
    SmtplibTransport,
    SMTPTransport,
)

__all__ = [
    "EmailCredentials",
    "EmailOutboundAdapter",
    "SMTPTransport",
    "SmtplibTransport",
]
