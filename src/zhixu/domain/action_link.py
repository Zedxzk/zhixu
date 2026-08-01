"""Validated user-supplied links attached to ordinary resources."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .errors import ValidationError


@dataclass(frozen=True, slots=True)
class ActionLink:
    """A channel-independent action that opens an exact user-supplied HTTPS URL."""

    label: str
    url: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.label.strip() or len(self.label) > 40:
            raise ValidationError("action link label is invalid")
        if not self.url.strip() or len(self.url) > 2048:
            raise ValidationError("action link URL is invalid")
        if any(ord(character) < 32 for character in self.url):
            raise ValidationError("action link URL contains control characters")
        try:
            parsed = urlsplit(self.url)
        except ValueError as exc:
            raise ValidationError("action link URL is invalid") from exc
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValidationError("action links require an HTTPS URL without credentials")
