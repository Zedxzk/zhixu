"""Channel registration with explicit conversational versus outbound-only modes."""

from __future__ import annotations

from dataclasses import dataclass

from zhixu.domain.errors import ConflictError, NotFoundError
from zhixu.ports import ChannelAdapter


@dataclass(frozen=True, slots=True)
class RegisteredChannel:
    channel: str
    channel_account: str
    mode: str
    capabilities: dict[str, bool]


class ChannelRegistry:
    def __init__(
        self,
        adapters: tuple[ChannelAdapter, ...] = (),
        *,
        declared: tuple[RegisteredChannel, ...] = (),
    ) -> None:
        self._adapters: dict[tuple[str, str], ChannelAdapter] = {}
        self._declared = {
            (item.channel, item.channel_account): item for item in declared
        }
        if len(self._declared) != len(declared):
            raise ConflictError("channel account declaration is duplicated")
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: ChannelAdapter) -> None:
        key = (adapter.channel, adapter.channel_account)
        if key in self._adapters or key in self._declared:
            raise ConflictError("channel account is already registered")
        self._adapters[key] = adapter

    def get(self, channel: str, channel_account: str) -> ChannelAdapter:
        try:
            return self._adapters[(channel, channel_account)]
        except KeyError as exc:
            raise NotFoundError("channel account is not registered") from exc

    def describe(self) -> list[RegisteredChannel]:
        result = list(self._declared.values())
        for adapter in self._adapters.values():
            capabilities = adapter.capabilities
            mode = "conversational" if capabilities.inbound_text else "outbound-only"
            result.append(
                RegisteredChannel(
                    adapter.channel,
                    adapter.channel_account,
                    mode,
                    {
                        "inbound_text": capabilities.inbound_text,
                        "outbound_text": capabilities.outbound_text,
                        "proactive_push": capabilities.proactive_push,
                        "buttons": capabilities.buttons,
                        "attachments": capabilities.attachments,
                        "voice": capabilities.voice,
                        "groups": capabilities.groups,
                    },
                )
            )
        return sorted(result, key=lambda item: (item.channel, item.channel_account))

    def conversational(self) -> list[RegisteredChannel]:
        return [item for item in self.describe() if item.mode == "conversational"]
