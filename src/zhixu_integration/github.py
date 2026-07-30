"""Read-only GitHub PAT operations with a fixed provider origin."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

GITHUB_API_ORIGIN = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
MAX_RESPONSE_BYTES = 1024 * 1024
_SLUG = re.compile(r"[A-Za-z0-9_.-]{1,100}")


@dataclass(frozen=True, slots=True)
class IntegrationResult:
    ok: bool
    code: str
    data: dict[str, Any]


class GitHubTransport(Protocol):
    def request(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, object]: ...


class UrllibGitHubTransport:
    def request(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, object]:
        request = urllib.request.Request(url, headers=headers, method="GET")
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _RejectRedirects(),
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            body = exc.read(MAX_RESPONSE_BYTES + 1)
            status = int(exc.code)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("GitHub response is too large")
        try:
            return status, json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("GitHub response is not valid JSON") from exc


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
class GitHubPATExecutor:
    transport: GitHubTransport
    timeout_seconds: float = 10

    def __post_init__(self) -> None:
        if not 0.1 <= self.timeout_seconds <= 30:
            raise ValueError("GitHub executor timeout is invalid")

    def execute(
        self,
        credential: str,
        request: dict[str, Any],
    ) -> IntegrationResult:
        if not credential or len(credential) > 4096:
            return IntegrationResult(False, "credential_invalid", {})
        try:
            path = self._path(request)
        except (TypeError, ValueError):
            return IntegrationResult(False, "operation_denied", {})
        try:
            status, value = self.transport.request(
                GITHUB_API_ORIGIN + path,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {credential}",
                    "X-GitHub-Api-Version": GITHUB_API_VERSION,
                    "User-Agent": "zhixu-pat-executor",
                },
                timeout=self.timeout_seconds,
            )
        except Exception:
            return IntegrationResult(False, "network_unavailable", {})
        if 200 <= status < 300:
            return IntegrationResult(
                True,
                "completed",
                {"status": status, "response": value},
            )
        code = (
            "authentication_failed"
            if status in {401, 403}
            else "rate_limited"
            if status == 429
            else "provider_error"
        )
        return IntegrationResult(False, code, {"status": status})

    @staticmethod
    def _path(request: dict[str, Any]) -> str:
        operation = request.get("operation")
        if operation == "github.get_authenticated_user" and set(request) == {
            "operation"
        }:
            return "/user"
        if operation == "github.get_repository" and set(request) == {
            "operation",
            "owner",
            "repository",
        }:
            owner = request.get("owner")
            repository = request.get("repository")
            if (
                not isinstance(owner, str)
                or not isinstance(repository, str)
                or _SLUG.fullmatch(owner) is None
                or _SLUG.fullmatch(repository) is None
            ):
                raise ValueError("repository coordinates are invalid")
            return f"/repos/{owner}/{repository}"
        if operation == "github.list_repositories" and set(request) <= {
            "operation",
            "visibility",
            "per_page",
        }:
            visibility = request.get("visibility", "all")
            per_page = request.get("per_page", 30)
            if visibility not in {"all", "public", "private"}:
                raise ValueError("repository visibility is invalid")
            if (
                not isinstance(per_page, int)
                or isinstance(per_page, bool)
                or not 1 <= per_page <= 100
            ):
                raise ValueError("repository page size is invalid")
            query = urllib.parse.urlencode(
                {
                    "visibility": visibility,
                    "per_page": per_page,
                    "sort": "updated",
                }
            )
            return f"/user/repos?{query}"
        raise ValueError("GitHub PAT operation is not allowlisted")
