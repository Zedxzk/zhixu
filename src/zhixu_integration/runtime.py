"""Peer-authenticated Unix socket runtime for the GitHub PAT executor."""

from __future__ import annotations

import argparse
import asyncio
import grp
import json
import os
import pwd
import signal
import socket
import stat
import struct
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .github import GitHubPATExecutor, UrllibGitHubTransport

MAX_FRAME_BYTES = 64 * 1024


class PATExecutorServer:
    def __init__(
        self,
        path: str | Path,
        executor: GitHubPATExecutor,
        *,
        allowed_uids: set[int],
        socket_gid: int,
    ) -> None:
        if not allowed_uids:
            raise ValueError("PAT executor requires an allowed Unix UID")
        self.path = Path(path)
        self.executor = executor
        self.allowed_uids = allowed_uids
        self.socket_gid = socket_gid
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o770)
        if self.path.exists():
            if not stat.S_ISSOCK(self.path.lstat().st_mode):
                raise FileExistsError("PAT executor path is occupied by a non-socket")
            self.path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle,
            path=self.path,
            limit=MAX_FRAME_BYTES,
        )
        os.chown(self.path, -1, self.socket_gid)
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
            peer = writer.get_extra_info("socket")
            if peer is None or _peer_uid(peer) not in self.allowed_uids:
                await _reply(writer, False, "peer_denied", {})
                return
            raw = await reader.readline()
            if not raw or len(raw) > MAX_FRAME_BYTES:
                await _reply(writer, False, "invalid_frame", {})
                return
            value = json.loads(raw)
            if (
                not isinstance(value, dict)
                or set(value) != {"credential", "request"}
                or not isinstance(value["credential"], str)
                or not isinstance(value["request"], dict)
            ):
                raise ValueError("executor request is invalid")
            result = await asyncio.to_thread(
                self.executor.execute,
                value["credential"],
                value["request"],
            )
            await _reply(writer, result.ok, result.code, result.data)
        except Exception:
            await _reply(writer, False, "executor_error", {})
        finally:
            writer.close()
            await writer.wait_closed()


def _peer_uid(peer: socket.socket) -> int:
    if not hasattr(socket, "SO_PEERCRED"):
        raise RuntimeError("Unix peer credentials are unavailable")
    value = peer.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,
        struct.calcsize("3i"),
    )
    _pid, uid, _gid = struct.unpack("3i", value)
    return int(uid)


async def _reply(
    writer: asyncio.StreamWriter,
    ok: bool,
    code: str,
    data: dict[str, Any],
) -> None:
    frame = json.dumps(
        {"ok": ok, "code": code, "data": data},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    if len(frame) + 1 > MAX_FRAME_BYTES:
        frame = b'{"ok":false,"code":"response_too_large","data":{}}'
    writer.write(frame + b"\n")
    await writer.drain()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zhixu-pat-executor")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--socket-group", required=True)
    parser.add_argument("--allowed-user", action="append", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=10)
    return parser


async def run(args: argparse.Namespace) -> None:
    allowed_uids = {pwd.getpwnam(name).pw_uid for name in args.allowed_user}
    socket_gid = grp.getgrnam(args.socket_group).gr_gid
    server = PATExecutorServer(
        args.socket,
        GitHubPATExecutor(
            UrllibGitHubTransport(),
            timeout_seconds=args.timeout_seconds,
        ),
        allowed_uids=allowed_uids,
        socket_gid=socket_gid,
    )
    await server.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for selected in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(selected, stop.set)
    try:
        await stop.wait()
    finally:
        await server.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
