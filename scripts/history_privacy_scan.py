"""Scan every Git blob for private infrastructure literals without printing content."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BLOB_BYTES = 5 * 1024 * 1024


def _git(*args: str, input_value: bytes | None = None) -> bytes:
    return subprocess.run(
        ("git", *args),
        cwd=ROOT,
        input=input_value,
        check=True,
        capture_output=True,
    ).stdout


def _patterns() -> tuple[tuple[str, re.Pattern[bytes]], ...]:
    return (
        ("absolute Unix home path", re.compile(re.escape(b"/" + b"home" + b"/"))),
        (
            "private IPv4 address",
            re.compile(
                rb"\b(?:10\.(?:\d{1,3}\.){2}\d{1,3}|"
                rb"192\.168\.(?:\d{1,3}\.)\d{1,3}|"
                rb"172\.(?:1[6-9]|2\d|3[01])\.(?:\d{1,3}\.)\d{1,3})\b"
            ),
        ),
        (
            "assigned secret-like value",
            re.compile(
                rb"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\b"
                rb"\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{12,}"
            ),
        ),
    )


def main() -> int:
    objects: dict[str, set[str]] = {}
    for line in _git("rev-list", "--objects", "--all").decode(
        "utf-8",
        errors="replace",
    ).splitlines():
        object_id, _, name = line.partition(" ")
        objects.setdefault(object_id, set()).add(name)
    findings: list[tuple[str, str, str]] = []
    for object_id, names in objects.items():
        metadata = _git("cat-file", "-t", object_id).strip()
        if metadata != b"blob":
            continue
        size = int(_git("cat-file", "-s", object_id))
        if size > MAX_BLOB_BYTES:
            findings.append((object_id[:12], _safe_name(names), "oversized blob"))
            continue
        content = _git("cat-file", "blob", object_id)
        for label, pattern in _patterns():
            if pattern.search(content):
                findings.append((object_id[:12], _safe_name(names), label))
    if findings:
        print("Git history privacy scan failed:")
        for object_id, name, label in findings:
            print(f"- blob={object_id} path={name} reason={label}")
        return 1
    print("Git history privacy scan passed.")
    return 0


def _safe_name(names: set[str]) -> str:
    value = sorted(name for name in names if name)[0] if any(names) else "<unknown>"
    return value.replace("\n", "?").replace("\r", "?")[:240]


if __name__ == "__main__":
    raise SystemExit(main())
