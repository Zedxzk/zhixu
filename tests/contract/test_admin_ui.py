from __future__ import annotations

from importlib.resources import files

from zhixu.adapters.web import AdminResponse
from zhixu.adapters.web.admin_ui import ui_response
from zhixu.runtime.api import CompositePrivateAPI


class _AdminStub:
    def dispatch(
        self,
        method: str,
        target: str,
        *,
        headers: dict[str, str],
        body: bytes,
    ) -> AdminResponse:
        del method, target, headers, body
        return AdminResponse(404, {"error": {"code": "not_found"}})


class _InternalStub(_AdminStub):
    pass


def _headers(response: AdminResponse) -> dict[str, str]:
    return {key.lower(): value for key, value in response.headers}


def test_ui_is_packaged_and_contains_no_remote_dependencies() -> None:
    package = files("zhixu.adapters.web.ui_assets")
    index = package.joinpath("index.html").read_text(encoding="utf-8")
    script = package.joinpath("app.js").read_text(encoding="utf-8")
    style = package.joinpath("app.css").read_text(encoding="utf-8")

    assert "<title>知序" in index
    assert 'src="/assets/app.js"' in index
    assert 'href="/assets/app.css"' in index
    assert "http://" not in index + script + style
    assert "https://" not in index + script + style
    assert "innerHTML" not in script
    assert 'sessionStorage.getItem("zhixu.session")' in script
    assert 'localStorage.getItem("zhixu.session")' not in script
    assert 'id="workspace-scope"' in index
    assert 'api("/admin/workspaces")' in script
    assert "workspaceTag(item)" in script
    assert 'class="content-grid system-observability-grid"' in index
    assert ".system-table .table-row" in style
    assert "cell.title = text" in script
    assert 'id="confirm-submit"' in index
    assert '"X-Zhixu-Confirm": "true"' in script
    assert "可能重复的重要日子" in script


def test_ui_response_has_strict_browser_security_headers() -> None:
    response = ui_response("GET", "/")

    assert response is not None
    assert response.status == 200
    assert isinstance(response.body, bytes)
    assert "知序" in response.body.decode()
    headers = _headers(response)
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert "default-src 'self'" in headers["content-security-policy"]
    assert "object-src 'none'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-frame-options"] == "DENY"
    assert "camera=()" in headers["permissions-policy"]


def test_ui_assets_support_head_and_conditional_requests() -> None:
    first = ui_response("HEAD", "/assets/app.js")

    assert first is not None
    assert first.status == 200
    assert isinstance(first.body, bytes)
    etag = _headers(first)["etag"]
    cached = ui_response(
        "GET",
        "/assets/app.js",
        headers={"If-None-Match": etag},
    )
    assert cached is not None
    assert cached.status == 304
    assert cached.body == b""


def test_ui_asset_routes_are_fixed_and_do_not_allow_traversal() -> None:
    missing = ui_response("GET", "/assets/../admin.py")
    method = ui_response("POST", "/")

    assert missing is not None
    assert missing.status == 404
    assert method is not None
    assert method.status == 405
    assert _headers(method)["allow"] == "GET, HEAD"


def test_composite_serves_ui_only_when_admin_web_is_enabled() -> None:
    enabled = CompositePrivateAPI(
        _AdminStub(),  # type: ignore[arg-type]
        _InternalStub(),  # type: ignore[arg-type]
        admin_enabled=True,
    )
    headless = CompositePrivateAPI(
        _AdminStub(),  # type: ignore[arg-type]
        _InternalStub(),  # type: ignore[arg-type]
        admin_enabled=False,
    )

    served = enabled.dispatch("GET", "/")
    refused = headless.dispatch("GET", "/")
    assert served.status == 200
    assert isinstance(served.body, bytes)
    assert refused.status == 404
