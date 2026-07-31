"""Reject common private artifacts and infrastructure data before they enter Git.

The scanner only reads tracked and untracked, non-ignored candidate files. It does not
inspect `.private`, runtime data, local media, or other paths excluded by `.gitignore`.
It complements a secret scanner; it is not a replacement for review.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
FORBIDDEN_SUFFIXES = {
    ".bak",
    ".db",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
    ".sqlite",
    ".sqlite3",
}
FORBIDDEN_PARTS = {
    ".private",
    "backups",
    "data",
    "docs/design",
    "docs/internal",
    "secrets",
    "state",
}


def _patterns() -> tuple[tuple[str, re.Pattern[bytes]], ...]:
    # Assemble sensitive literals so this source does not match itself.
    unix_home = b"/" + b"home" + b"/"
    windows_home = b"C:" + b"\\\\" + b"Users" + b"\\\\"
    private_ipv4 = (
        rb"\b(?:10\.(?:\d{1,3}\.){2}\d{1,3}|"
        rb"192\.168\.(?:\d{1,3}\.)\d{1,3}|"
        rb"172\.(?:1[6-9]|2\d|3[01])\.(?:\d{1,3}\.)\d{1,3})\b"
    )
    assigned_secret = (
        rb"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\b"
        rb"\s*[:=]\s*(?:"
        rb"[\"'][A-Za-z0-9_./+=-]{12,}[\"']|"
        rb"(?=[A-Za-z0-9_./+=-]{12,})(?=[A-Za-z0-9_./+=-]*[0-9./+=-])"
        rb"[A-Za-z0-9_./+=-]{12,})"
    )
    return (
        ("absolute Unix home path", re.compile(re.escape(unix_home))),
        ("absolute Windows home path", re.compile(re.escape(windows_home))),
        ("private IPv4 address", re.compile(private_ipv4)),
        ("assigned secret-like value", re.compile(assigned_secret)),
    )


def candidate_files(*, tracked_only: bool) -> list[Path]:
    args = ["git", "ls-files", "-z"]
    if not tracked_only:
        args.extend(["--cached", "--others", "--exclude-standard"])
    result = subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    names = {
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    }
    return sorted((ROOT / name for name in names), key=lambda path: str(path))


def scan_path(path: Path) -> list[str]:
    relative = path.relative_to(ROOT)
    normalized = relative.as_posix()
    findings: list[str] = []

    if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
        findings.append("forbidden private-data file type")
    if any(normalized == part or normalized.startswith(f"{part}/") for part in FORBIDDEN_PARTS):
        findings.append("forbidden private path")
    if "IMPLEMENTATION_PLAN" in path.name.upper() or "DESIGN" in path.name.upper():
        findings.append("internal design document name")
    if path.resolve() == SELF or not path.is_file():
        return findings

    content = path.read_bytes()
    if b"\0" in content[:8192]:
        return findings
    for label, pattern in _patterns():
        if pattern.search(content):
            findings.append(label)
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--tracked-only",
        action="store_true",
        help="scan only files already tracked by Git",
    )
    args = parser.parse_args()

    all_findings: list[tuple[Path, list[str]]] = []
    for path in candidate_files(tracked_only=args.tracked_only):
        findings = scan_path(path)
        if findings:
            all_findings.append((path.relative_to(ROOT), findings))

    if all_findings:
        print("Privacy scan failed:")
        for path, findings in all_findings:
            print(f"- {path}: {', '.join(findings)}")
        return 1
    print("Privacy scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
