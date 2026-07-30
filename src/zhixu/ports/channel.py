"""Ports implemented by messaging channel adapters."""

from __future__ import annotations

from typing import Protocol

from zhixu.channels import ChannelCapabilities, ChannelDeliveryResult, OutboundMessage


class ChannelAdapter(Protocol):
    @property
    def channel(self) -> str: ...

    @property
    def channel_account(self) -> str: ...

    @property
    def capabilities(self) -> ChannelCapabilities: ...

    def send(self, message: OutboundMessage) -> ChannelDeliveryResult: ...
