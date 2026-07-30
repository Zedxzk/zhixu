"""Shared runtime helpers that never read secret values from argv or environment."""

from __future__ import annotations

import base64
import binascii
import logging
from pathlib import Path


def read_key_file(
    path: str | Path,
    *,
    exact_bytes: int | None = None,
    minimum_bytes: int = 32,
) -> bytes:
    source = Path(path)
    raw = source.read_bytes().strip()
    try:
        decoded = base64.b64decode(raw, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        decoded = raw
    if exact_bytes is not None and len(decoded) != exact_bytes:
        raise ValueError(f"credential file {source.name} has an invalid key length")
    if len(decoded) < minimum_bytes:
        raise ValueError(f"credential file {source.name} is too short")
    return decoded


def read_text_credential(path: str | Path, *, maximum: int = 4096) -> str:
    source = Path(path)
    value = source.read_text(encoding="utf-8").strip()
    if not value or len(value) > maximum or "\0" in value:
        raise ValueError(f"credential file {source.name} is invalid")
    return value


def configure_logging(level: str) -> None:
    selected = getattr(logging, level.upper(), None)
    if not isinstance(selected, int):
        raise ValueError("invalid log level")
    logging.basicConfig(
        level=selected,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
