"""Fixed-purpose DeepSeek proxy isolated from application data."""

from __future__ import annotations

import argparse
import ipaddress
import json
import signal
import threading
import urllib.error
import urllib.request
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .common import configure_logging, read_text_credential

MAX_REQUEST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


class DeepSeekProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], *, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _RejectRedirects(),
        )
        super().__init__(address, DeepSeekProxyHandler)


class DeepSeekProxyHandler(BaseHTTPRequestHandler):
    server_version = "ZhixuLLM"
    sys_version = ""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, b'{"status":"ready"}')
        else:
            self._send(404, b'{"error":"not_found"}')

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions" or self.headers.get("Transfer-Encoding"):
            self._send(404, b'{"error":"not_found"}')
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = MAX_REQUEST_BYTES + 1
        if not 0 < length <= MAX_REQUEST_BYTES:
            self._send(413, b'{"error":"request_rejected"}')
            return
        try:
            value = json.loads(self.rfile.read(length))
            messages = value["messages"]
            response_format = value.get("response_format", {})
            schema = (
                response_format.get("json_schema", {}).get("schema")
                if isinstance(response_format, dict)
                else None
            )
            if (
                not isinstance(value, dict)
                or not isinstance(messages, list)
                or not 1 <= len(messages) <= 4
                or any(
                    not isinstance(item, dict)
                    or set(item) != {"role", "content"}
                    or item["role"] not in {"system", "user"}
                    or not isinstance(item["content"], str)
                    or len(item["content"]) > 16_000
                    for item in messages
                )
                or not isinstance(schema, dict)
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self._send(422, b'{"error":"request_rejected"}')
            return
        server = self.server
        assert isinstance(server, DeepSeekProxyServer)
        forwarded_messages = [dict(item) for item in messages]
        forwarded_messages[0]["content"] += (
            "\nRequired JSON schema (follow exactly; omit no required fields):\n"
            + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        )
        payload = json.dumps(
            {
                "model": server.model,
                "messages": forwarded_messages,
                "response_format": {"type": "json_object"},
                "thinking": {"type": "disabled"},
                "max_tokens": 1000,
            },
            separators=(",", ":"),
        ).encode()
        request = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {server.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with server.opener.open(request, timeout=20) as response:
                status = int(response.status)
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            body = exc.read(MAX_RESPONSE_BYTES + 1)
        except OSError:
            self._send(502, b'{"error":"upstream_unavailable"}')
            return
        if len(body) > MAX_RESPONSE_BYTES:
            self._send(502, b'{"error":"upstream_response_too_large"}')
            return
        self._send(status, body)

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zhixu-llm-proxy")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8841)
    parser.add_argument("--api-key-file", required=True)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    address = ipaddress.ip_address(args.bind)
    if not address.is_loopback or not 1 <= args.port <= 65535:
        raise SystemExit("LLM proxy may only bind a valid loopback address and port")
    if args.model != "deepseek-v4-flash":
        raise SystemExit("LLM proxy model is not allowed")
    server = DeepSeekProxyServer(
        (str(address), args.port),
        api_key=read_text_credential(args.api_key_file),
        model=args.model,
    )

    def stop(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
