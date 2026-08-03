from __future__ import annotations

import urllib.request
from datetime import UTC, datetime
from typing import Any

import pytest

from zhixu.adapters.llm import OpenAICompatibleLLM
from zhixu.adapters.llm.openai_compatible import UrllibLLMTransport
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
            "usage": {
                "prompt_tokens": 4,
                "completion_tokens": 2,
                "prompt_cache_hit_tokens": 3,
                "prompt_cache_miss_tokens": 1,
            },
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
    assert response.input_units == 4
    assert response.output_units == 2
    assert response.cached_input_units == 3
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


def test_openai_compatible_adapter_marks_explicit_web_search() -> None:
    transport = FakeTransport()
    client = OpenAICompatibleLLM(
        provider_ref="synthetic",
        base_url="http://localhost:9999/v1",
        api_key="",
        is_local=True,
        transport=transport,
    )

    client.generate(
        LLMRequest(
            "model",
            "system",
            "public question",
            {"type": "object"},
            web_search=True,
        ),
        timeout_seconds=1,
    )

    assert transport.calls[0]["payload"]["web_search"] is True


def test_llm_transport_disables_proxies_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []

    class Opener:
        pass

    def build_opener(*handlers: object) -> Opener:
        captured.extend(handlers)
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    UrllibLLMTransport()

    proxy = next(
        item for item in captured if isinstance(item, urllib.request.ProxyHandler)
    )
    assert proxy.proxies == {}
    assert any(type(item).__name__ == "_RejectRedirects" for item in captured)


@pytest.mark.parametrize(
    ("base_url", "is_local"),
    [
        ("http://provider.example.invalid/v1", False),
        ("https://user@provider.example.invalid/v1", False),
        ("https://provider.example.invalid/v1?redirect=1", False),
        ("https://provider.example.invalid/v1#fragment", False),
        ("http://provider.example.invalid/v1", True),
    ],
)
def test_llm_endpoint_rejects_credential_leak_boundaries(
    base_url: str,
    is_local: bool,
) -> None:
    with pytest.raises(ValueError):
        OpenAICompatibleLLM(
            provider_ref="synthetic",
            base_url=base_url,
            api_key="",
            is_local=is_local,
        )


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


def test_explicit_question_is_the_only_rule_routed_to_web_search() -> None:
    router = RuleIntentRouter(FrozenClock(datetime(2026, 6, 1, 8, tzinfo=UTC)))

    explicit = router.route("/问 current synthetic fact")
    implicit = router.route("current synthetic fact")

    assert explicit is not None
    assert explicit.action is IntentAction.ANSWER
    assert explicit.arguments["web_search"] is True
    assert implicit is None


def test_calendar_commands_route_without_an_llm() -> None:
    router = RuleIntentRouter(FrozenClock(datetime(2026, 6, 1, 8, tzinfo=UTC)))

    current = router.route("/日历")
    selected = router.route("/月历 2026-8")

    assert current is not None
    assert current.action is IntentAction.VIEW_CALENDAR
    assert current.arguments == {}
    assert selected is not None
    assert selected.action is IntentAction.VIEW_CALENDAR
    assert selected.arguments == {"year": 2026, "month": 8}
