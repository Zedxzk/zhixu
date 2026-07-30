from __future__ import annotations

from zhixu_integration.github import (
    GITHUB_API_ORIGIN,
    GITHUB_API_VERSION,
    GitHubPATExecutor,
)


class FakeGitHubTransport:
    def __init__(self, status: int = 200, response: object | None = None) -> None:
        self.status = status
        self.response = response if response is not None else {"login": "synthetic"}
        self.requests: list[tuple[str, dict[str, str], float]] = []

    def request(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, object]:
        self.requests.append((url, headers, timeout))
        return self.status, self.response


def test_github_pat_executor_allows_only_fixed_read_operations() -> None:
    transport = FakeGitHubTransport()
    executor = GitHubPATExecutor(transport)

    user = executor.execute(
        "synthetic-pat-value",
        {"operation": "github.get_authenticated_user"},
    )
    repository = executor.execute(
        "synthetic-pat-value",
        {
            "operation": "github.get_repository",
            "owner": "synthetic-owner",
            "repository": "synthetic-repository",
        },
    )
    listed = executor.execute(
        "synthetic-pat-value",
        {
            "operation": "github.list_repositories",
            "visibility": "private",
            "per_page": 10,
        },
    )
    denied = executor.execute(
        "synthetic-pat-value",
        {
            "operation": "github.api",
            "method": "DELETE",
            "url": "https://example.invalid/",
        },
    )

    assert user.ok and repository.ok and listed.ok
    assert denied.code == "operation_denied"
    assert len(transport.requests) == 3
    assert [request[0] for request in transport.requests] == [
        f"{GITHUB_API_ORIGIN}/user",
        f"{GITHUB_API_ORIGIN}/repos/synthetic-owner/synthetic-repository",
        (
            f"{GITHUB_API_ORIGIN}/user/repos"
            "?visibility=private&per_page=10&sort=updated"
        ),
    ]
    for _url, headers, timeout in transport.requests:
        assert headers["Authorization"] == "Bearer synthetic-pat-value"
        assert headers["X-GitHub-Api-Version"] == GITHUB_API_VERSION
        assert timeout == 10
    assert "synthetic-pat-value" not in repr(user)


def test_github_pat_executor_returns_minimal_provider_failures() -> None:
    unauthorized = GitHubPATExecutor(FakeGitHubTransport(status=401)).execute(
        "synthetic-pat-value",
        {"operation": "github.get_authenticated_user"},
    )
    provider_error = GitHubPATExecutor(FakeGitHubTransport(status=503)).execute(
        "synthetic-pat-value",
        {"operation": "github.get_authenticated_user"},
    )

    assert unauthorized.data == {"status": 401}
    assert unauthorized.code == "authentication_failed"
    assert provider_error.data == {"status": 503}
    assert provider_error.code == "provider_error"
