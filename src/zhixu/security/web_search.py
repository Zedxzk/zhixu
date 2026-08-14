"""Fail-closed screening before a user query enters an external web search."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from .patterns import ASSIGNMENT, CREDENTIAL_LABELS, TOKEN_PATTERN

# The first four and the last are personal identifiers and entropy heuristics
# that only make sense before an internet search; they stay private to this
# module. The credential shapes come from the shared vocabulary.
_SENSITIVE_PATTERNS = (
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)(?:\d[ -]?){12,19}(?!\d)"),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    TOKEN_PATTERN,
    re.compile(
        rf"(?:{CREDENTIAL_LABELS}){ASSIGNMENT}" + r"\S{6,}",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?=[A-Za-z0-9_-]{24,}\b)(?=[^\s]*[A-Za-z])"
        r"(?=[^\s]*\d)[A-Za-z0-9_-]+\b"
    ),
)
_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def web_query_is_safe(query: str) -> bool:
    """Reject likely credentials and personal identifiers without logging them."""

    if not query or len(query) > 500:
        return False
    if any(pattern.search(query) for pattern in _SENSITIVE_PATTERNS):
        return False
    for match in _URL_PATTERN.finditer(query):
        parsed = urlsplit(match.group(0))
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or hostname == "localhost"
            or hostname.endswith((".local", ".internal"))
        ):
            return False
        try:
            if not ipaddress.ip_address(hostname).is_global:
                return False
        except ValueError:
            pass
    return True
