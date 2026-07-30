from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from zhixu.adapters.llm import OpenAICompatibleLLM
from zhixu.application import IntentAction, RuleIntentRouter
from zhixu.ports import FrozenClock, LLMRequest


class FakeTransport:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def post(
        self,
        url: str,
        *,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append(
            {
                "url": url,
                "payload": payload,
                "headers": headers,
                "timeout": timeout_seconds,
            }
        )
        if self.status == 408:
            return 408, {}
        return 200, {
            "choices": [{"message": {"content": '{"answer":"synthetic"}'}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        }


def test_openai_compatible_adapter_uses_strict_schema_without_leaking_key() -> None:
    transport = FakeTransport()
    client = OpenAICompatibleLLM(
        provider_ref="synthetic",
        base_url="https://example.invalid/v1",
        api_key="fakekey",  # pragma: allowlist secret
        transport=transport,
    )
    request = LLMRequest(
        model="synthetic-model",
        system_prompt="private-system-canary",
        user_prompt="private-user-canary",
        response_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )

    response = client.generate(request, timeout_seconds=3)

    assert response.content == '{"answer":"synthetic"}'
    call = transport.calls[0]
    assert call["url"] == "https://example.invalid/v1/chat/completions"
    assert call["payload"]["response_format"]["json_schema"]["strict"] is True
    assert call["headers"]["Authorization"] == "Bearer fakekey"
    assert "fakekey" not in repr(client)
    assert "private-user-canary" not in repr(request)


def test_openai_compatible_timeout_is_reported_without_response_body() -> None:
    client = OpenAICompatibleLLM(
        provider_ref="synthetic",
        base_url="http://localhost:9999/v1",
        api_key="",
        is_local=True,
        transport=FakeTransport(status=408),
    )

    with pytest.raises(TimeoutError):
        client.generate(LLMRequest("model", "system", "user"), timeout_seconds=1)


def test_chinese_absolute_reminder_parser_rejects_past_or_invalid_time() -> None:
    clock = FrozenClock(datetime(2026, 6, 1, 8, tzinfo=UTC))
    router = RuleIntentRouter(clock, timezone="Asia/Shanghai")

    future = router.route("明天下午3点30分提醒我Synthetic meeting")
    past = router.route("今天上午7点提醒我Synthetic past")
    invalid = router.route("明天25点提醒我Synthetic invalid")

    assert future is not None
    assert future.action is IntentAction.CREATE_REMINDER
    assert future.arguments["fire_at"].hour == 15
    assert future.arguments["fire_at"].minute == 30
    assert past is None
    assert invalid is None
