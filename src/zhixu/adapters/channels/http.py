"""Small injectable JSON HTTP transport used by fixed-provider adapters."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Protocol

MAX_JSON_RESPONSE_BYTES = 1024 * 1024


class JsonTransport(Protocol):
    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10,
    ) -> tuple[int, dict[str, Any]]: ...


class UrllibJsonTransport:
    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10,
    ) -> tuple[int, dict[str, Any]]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = {"Accept": "application/json", **(headers or {})}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url,
            data=body,
            headers=request_headers,
            method=method,
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _RejectRedirects(),
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                status = int(response.status)
                body = response.read(MAX_JSON_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            body = exc.read(MAX_JSON_RESPONSE_BYTES + 1)
        if len(body) > MAX_JSON_RESPONSE_BYTES:
            raise ValueError("provider JSON response is too large")
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
