"""Bounded loopback and Unix-socket health probes."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def vault_available(path: str | Path, *, timeout: float = 1.0) -> bool:
    request = json.dumps({"method": "status", "params": {}}).encode() + b"\n"
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(path))
        client.sendall(request)
        raw = bytearray()
        while len(raw) <= 64 * 1024:
            chunk = client.recv(4096)
            if not chunk:
                break
            raw.extend(chunk)
            if b"\n" in chunk:
                break
        value = json.loads(bytes(raw).split(b"\n", 1)[0])
        return bool(value.get("ok")) and isinstance(value.get("result"), dict)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    finally:
        client.close()


def loopback_http_available(url: str, *, timeout: float = 1.0) -> bool:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "http"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return False
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                host,
                parsed.port or 80,
                type=socket.SOCK_STREAM,
            )
        }
    except OSError:
        return False
    if not addresses or any(
        not _is_loopback(address) for address in addresses
    ):
        return False
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RejectRedirects(),
    )
    try:
        with opener.open(url, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except (OSError, urllib.error.URLError):
        return False


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


def _is_loopback(value: str) -> bool:
    import ipaddress

    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False
