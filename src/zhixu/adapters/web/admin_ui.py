"""Same-origin static administration UI with no external asset dependencies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from urllib.parse import urlsplit

from .admin import AdminResponse


@dataclass(frozen=True, slots=True)
class _Asset:
    filename: str
    content_type: str


_ASSETS = {
    "/": _Asset("index.html", "text/html; charset=utf-8"),
    "/ui": _Asset("index.html", "text/html; charset=utf-8"),
    "/assets/app.css": _Asset("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": _Asset("app.js", "text/javascript; charset=utf-8"),
    "/assets/mark.svg": _Asset("mark.svg", "image/svg+xml"),
    "/favicon.svg": _Asset("mark.svg", "image/svg+xml"),
    "/manifest.webmanifest": _Asset(
        "manifest.webmanifest",
        "application/manifest+json",
    ),
}
_CSP = (
    "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
    "form-action 'self'; connect-src 'self'; img-src 'self' data:; "
    "font-src 'self'; style-src 'self'; script-src 'self'"
)


def ui_response(
    method: str,
    target: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> AdminResponse | None:
    """Return a fixed packaged asset, or None when the target is not a UI route."""

    path = urlsplit(target).path.rstrip("/") or "/"
    asset = _ASSETS.get(path)
    if asset is None:
        if path.startswith("/assets/"):
            return AdminResponse(404, {"error": {"code": "not_found"}})
        return None
    if method.upper() not in {"GET", "HEAD"}:
        return AdminResponse(
            405,
            {"error": {"code": "method_not_allowed"}},
            (("Allow", "GET, HEAD"),),
        )
    package = files("zhixu.adapters.web.ui_assets")
    payload = package.joinpath(asset.filename).read_bytes()
    request_headers = {key.lower(): value for key, value in (headers or {}).items()}
    etag = f'"zhixu-ui-{len(payload):x}"'
    common_headers = (
        ("Content-Type", asset.content_type),
        ("Content-Security-Policy", _CSP),
        ("Referrer-Policy", "no-referrer"),
        ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
        ("X-Frame-Options", "DENY"),
        ("ETag", etag),
    )
    if request_headers.get("if-none-match") == etag:
        return AdminResponse(304, b"", common_headers)
    return AdminResponse(200, payload, common_headers)
