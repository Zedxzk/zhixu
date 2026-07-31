"""Minimal OpenAI-compatible HTTP adapter without a provider SDK."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from zhixu.ports import LLMRequest, LLMResponse

MAX_LLM_RESPONSE_BYTES = 1024 * 1024


class LLMHttpTransport(Protocol):
    def post(
        self,
        url: str,
        *,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]: ...


class UrllibLLMTransport:
    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _RejectRedirects(),
        )

    def post(
        self,
        url: str,
        *,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                status = int(response.status)
                body = response.read(MAX_LLM_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            body = exc.read(MAX_LLM_RESPONSE_BYTES + 1)
        if len(body) > MAX_LLM_RESPONSE_BYTES:
            raise ValueError("LLM response is too large")
        raw = body.decode("utf-8", "replace")
        try:
            value = json.loads(raw or "{}")
        except ValueError:
            value = {}
        return status, value if isinstance(value, dict) else {}


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        _request: urllib.request.Request,
        _file_pointer: object,
        _code: int,
        _message: str,
        _headers: object,
        _new_url: str,
    ) -> None:
        return None


@dataclass(slots=True)
class OpenAICompatibleLLM:
    provider_ref: str
    base_url: str
    api_key: str = field(repr=False)
    is_local: bool = False
    transport: LLMHttpTransport = field(default_factory=UrllibLLMTransport, repr=False)

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlparse(self.base_url)
        loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (self.is_local and not loopback)
            or (not self.is_local and parsed.scheme != "https" and not loopback)
        ):
            raise ValueError("LLM base URL must be an absolute HTTP(S) URL")
        self.base_url = self.base_url.rstrip("/")

    def generate(self, request: LLMRequest, *, timeout_seconds: float) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
        }
        if request.response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "zhixu_response",
                    "strict": True,
                    "schema": request.response_schema,
                },
            }
        if request.web_search:
            payload["web_search"] = True
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        status, value = self.transport.post(
            f"{self.base_url}/chat/completions",
            payload=payload,
            headers=headers,
            timeout_seconds=timeout_seconds,
        )
        if status == 408:
            raise TimeoutError("LLM request timed out")
        if status < 200 or status >= 300:
            raise RuntimeError(f"LLM provider request failed with HTTP {status}")
        choices = value.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("LLM provider response is missing choices")
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = message.get("content")
        if not isinstance(content, str):
            raise RuntimeError("LLM provider response is missing text")
        usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
        return LLMResponse(
            content,
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
        )
