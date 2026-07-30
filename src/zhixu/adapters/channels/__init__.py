"""Messaging platform adapters."""

from .receipts import InboundReceiptStore
from .registry import ChannelRegistry, RegisteredChannel
from .targets import OutboundTargetResolver, OutboundTargetStore, ResolvedOutboundTarget

__all__ = [
    "ChannelRegistry",
    "InboundReceiptStore",
    "OutboundTargetResolver",
    "OutboundTargetStore",
    "RegisteredChannel",
    "ResolvedOutboundTarget",
]
