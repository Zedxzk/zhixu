"""Messaging platform adapters."""

from .receipts import InboundReceiptStore
from .registry import ChannelRegistry, RegisteredChannel
from .targets import OutboundTargetStore, ResolvedOutboundTarget

__all__ = [
    "ChannelRegistry",
    "InboundReceiptStore",
    "OutboundTargetStore",
    "RegisteredChannel",
    "ResolvedOutboundTarget",
]
