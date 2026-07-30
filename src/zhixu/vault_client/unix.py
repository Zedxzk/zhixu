"""Bounded synchronous client for the local vault Unix-socket API."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

MAX_FRAME_BYTES = 64 * 1024


class UnixVaultClient:
    def __init__(self, path: str | Path, *, timeout: float = 5) -> None:
        if timeout <= 0 or timeout > 30:
            raise ValueError("vault client timeout is invalid")
        self.path = str(path)
        self.timeout = timeout

    def call(self, method: str, params: dict[str, object]) -> dict[str, Any]:
        request = json.dumps(
            {"method": method, "params": params},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode() + b"\n"
        if not method or len(request) > MAX_FRAME_BYTES:
            raise ValueError("vault request is invalid")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(self.timeout)
        try:
            client.connect(self.path)
            client.sendall(request)
            raw = bytearray()
            while len(raw) <= MAX_FRAME_BYTES:
                chunk = client.recv(4096)
                if not chunk:
                    break
                raw.extend(chunk)
                if b"\n" in chunk:
                    break
        finally:
            client.close()
        if not raw or len(raw) > MAX_FRAME_BYTES:
            raise RuntimeError("vault returned an invalid response")
        response = json.loads(bytes(raw).split(b"\n", 1)[0])
        if not isinstance(response, dict) or not response.get("ok"):
            raise PermissionError("vault request was rejected")
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("vault returned an invalid result")
        return result
