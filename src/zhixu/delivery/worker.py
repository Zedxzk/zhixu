"""One-step delivery worker composed from outbox, quotas and channel ports."""

from __future__ import annotations

from datetime import datetime

from zhixu.domain.errors import ValidationError
from zhixu.ports import ChannelAdapter

from .outbox import OutboxStore
from .quota import QuotaManager, QuotaScope
from .renderer import render_for_capabilities


class DeliveryWorker:
    def __init__(
        self,
        *,
        worker_id: str,
        outbox: OutboxStore,
        quota: QuotaManager,
        adapters: tuple[ChannelAdapter, ...],
    ) -> None:
        self.worker_id = worker_id
        self.outbox = outbox
        self.quota = quota
        self.adapters = {
            (adapter.channel, adapter.channel_account): adapter for adapter in adapters
        }

    def tick(self, *, now: datetime) -> str:
        claimed = self.outbox.claim(worker_id=self.worker_id, now=now)
        if claimed is None:
            return "idle"
        message = claimed.message
        adapter = self.adapters.get((message.channel, message.channel_account))
        if adapter is None:
            from zhixu.channels import ChannelDeliveryResult

            return self.outbox.complete(
                claimed,
                ChannelDeliveryResult(False, False, "adapter_unavailable"),
                now=now,
            )
        decision = self.quota.reserve(
            (
                QuotaScope("provider", message.channel),
                QuotaScope("account", message.channel_account),
                QuotaScope("conversation", message.target_ref),
                QuotaScope("user", claimed.owner_user_id),
            ),
            now=now,
        )
        if not decision.allowed:
            self.outbox.defer_for_quota(
                claimed,
                next_attempt_at=decision.next_available_at,
                now=now,
                reason_code=decision.reason_code,
            )
            return "quota_wait"
        try:
            rendered = render_for_capabilities(message, adapter.capabilities)
            result = adapter.send(rendered)
        except ValidationError:
            from zhixu.channels import ChannelDeliveryResult

            result = ChannelDeliveryResult(False, False, "capability_unsupported")
        except Exception:
            from zhixu.channels import ChannelDeliveryResult

            result = ChannelDeliveryResult(False, True, "adapter_exception")
        return self.outbox.complete(claimed, result, now=now)
