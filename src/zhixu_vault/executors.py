"""Trusted machine-secret executors that never return the credential."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .service import ExecutionResult
from .types import SecretValue

PATOperation = Callable[[str, dict[str, Any]], ExecutionResult]
MAX_EXECUTOR_FRAME_BYTES = 64 * 1024


@dataclass(slots=True)
class PATIntegrationExecutor:
    """Dispatches an allowlisted PAT operation inside the isolated executor boundary."""

    operations: dict[str, PATOperation]

    def execute(
        self,
        secret: SecretValue,
        request: dict[str, Any],
    ) -> ExecutionResult:
        operation_name = str(request.get("operation") or "")
        operation = self.operations.get(operation_name)
        if operation is None:
            return ExecutionResult(False, "operation_denied", {})
        safe_request = {
            key: value for key, value in request.items() if key != "credential"
        }
        return operation(secret.text(), safe_request)


@dataclass(frozen=True, slots=True)
class UnixSocketMachineExecutor:
    """Hands a secret to one fixed local integration boundary over a Unix socket."""

    path: Path
    timeout_seconds: float = 10

    def __post_init__(self) -> None:
        if (
            not self.path.is_absolute()
            or "\0" in str(self.path)
            or not 0.1 <= self.timeout_seconds <= 60
        ):
            raise ValueError("machine executor socket configuration is invalid")

    def execute(
        self,
        secret: SecretValue,
        request: dict[str, Any],
    ) -> ExecutionResult:
        if "credential" in request:
            raise PermissionError("executor request cannot supply credential material")
        frame = json.dumps(
            {
                "credential": secret.text(),
                "request": request,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if len(frame) + 1 > MAX_EXECUTOR_FRAME_BYTES:
            raise ValueError("machine executor request is too large")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout_seconds)
            client.connect(str(self.path))
            client.sendall(frame + b"\n")
            response = _read_frame(client)
        value = json.loads(response)
        if (
            not isinstance(value, dict)
            or set(value) != {"ok", "code", "data"}
            or not isinstance(value["ok"], bool)
            or not isinstance(value["code"], str)
            or not isinstance(value["data"], dict)
            or not value["code"]
            or len(value["code"]) > 80
        ):
            raise ValueError("machine executor response is invalid")
        return ExecutionResult(value["ok"], value["code"], value["data"])


def _read_frame(client: socket.socket) -> bytes:
    chunks = bytearray()
    while len(chunks) <= MAX_EXECUTOR_FRAME_BYTES:
        value = client.recv(min(4096, MAX_EXECUTOR_FRAME_BYTES + 1 - len(chunks)))
        if not value:
            break
        chunks.extend(value)
        if b"\n" in value:
            break
    if not chunks.endswith(b"\n") or len(chunks) > MAX_EXECUTOR_FRAME_BYTES:
        raise ValueError("machine executor response frame is invalid")
    return bytes(chunks[:-1])
