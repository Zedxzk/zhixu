"""Length-bounded JSON RPC over an authenticated Unix domain socket."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import struct
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .crypto import VaultKeyring
from .service import VaultService

MAX_FRAME_BYTES = 64 * 1024


class VaultRPCDispatcher:
    def __init__(self, service: VaultService, keyring: VaultKeyring) -> None:
        self.service = service
        self.keyring = keyring

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        method = str(request.get("method") or "")
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        if method == "status":
            return {"sealed": self.keyring.sealed}
        if method == "lock":
            self.keyring.lock()
            return {"sealed": True}
        if method == "list_metadata":
            values = self.service.list_metadata(
                str(params.get("grant") or ""),
                str(params.get("secret_id") or ""),
            )
            return {
                "items": [
                    {
                        **asdict(item),
                        "kind": item.kind.value,
                        "classification": item.classification.value,
                        "created_at": item.created_at.isoformat(),
                        "updated_at": item.updated_at.isoformat(),
                    }
                    for item in values
                ]
            }
        if method == "use":
            result = self.service.use(
                str(params.get("grant") or ""),
                str(params.get("secret_id") or ""),
                executor_name=str(params.get("executor") or ""),
                request=(
                    params.get("request")
                    if isinstance(params.get("request"), dict)
                    else {}
                ),
            )
            return {"ok": result.ok, "code": result.code, "data": result.data}
        if method == "reveal":
            with self.service.reveal(
                str(params.get("grant") or ""),
                str(params.get("secret_id") or ""),
            ) as secret:
                return {"value": secret.text()}
        raise ValueError("unsupported vault RPC method")


class UnixVaultServer:
    def __init__(
        self,
        path: str | Path,
        dispatcher: VaultRPCDispatcher,
        *,
        allowed_uids: set[int],
    ) -> None:
        if not allowed_uids:
            raise ValueError("at least one allowed Unix UID is required")
        self.path = Path(path)
        self.dispatcher = dispatcher
        self.allowed_uids = allowed_uids
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        if self.path.exists():
            mode = self.path.lstat().st_mode
            if not stat.S_ISSOCK(mode):
                raise FileExistsError("vault socket path is occupied by a non-socket")
            self.path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle,
            path=self.path,
            limit=MAX_FRAME_BYTES,
        )
        os.chmod(self.path, 0o660)

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            peer_socket = writer.get_extra_info("socket")
            if peer_socket is None or self._peer_uid(peer_socket) not in self.allowed_uids:
                await self._reply(writer, {"ok": False, "error": "peer_denied"})
                return
            raw = await reader.readline()
            if not raw or len(raw) > MAX_FRAME_BYTES:
                await self._reply(writer, {"ok": False, "error": "invalid_frame"})
                return
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            result = self.dispatcher.dispatch(request)
            await self._reply(writer, {"ok": True, "result": result})
        except Exception as exc:
            await self._reply(
                writer,
                {"ok": False, "error": type(exc).__name__},
            )
        finally:
            writer.close()
            await writer.wait_closed()

    @staticmethod
    def _peer_uid(peer_socket: socket.socket) -> int:
        if not hasattr(socket, "SO_PEERCRED"):
            raise RuntimeError("Unix peer credentials are unavailable")
        credentials = peer_socket.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            struct.calcsize("3i"),
        )
        _pid, uid, _gid = struct.unpack("3i", credentials)
        return int(uid)

    @staticmethod
    async def _reply(
        writer: asyncio.StreamWriter,
        payload: dict[str, Any],
    ) -> None:
        writer.write(
            json.dumps(payload, separators=(",", ":")).encode() + b"\n"
        )
        await writer.drain()
