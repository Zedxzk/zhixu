"""Tencent QQ official-bot adapter."""

from ..receipts import InboundReceiptStore
from .admission import AdmissionDecision, InboundAdmission
from .contacts import QQContactStore, ResolvedQQTarget
from .gateway import QQGatewayProtocol, QQGatewayRunner, QQGatewayState
from .http import QQBotCredentials, QQHttpAdapter, UrllibJsonTransport

__all__ = [
    "AdmissionDecision",
    "InboundAdmission",
    "InboundReceiptStore",
    "QQBotCredentials",
    "QQContactStore",
    "QQGatewayProtocol",
    "QQGatewayRunner",
    "QQGatewayState",
    "QQHttpAdapter",
    "ResolvedQQTarget",
    "UrllibJsonTransport",
]
