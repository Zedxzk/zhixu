"""Shared vocabulary for the credential detectors.

Only the credential shapes live here. The web-search gate keeps its own,
deliberately broader personal-identifier patterns private: an email address or
a phone number is a reason not to run an internet search, but it is not a
reason to redact an ordinary note.
"""

from __future__ import annotations

import re

# Nouns that introduce a credential. Kept as a bare alternation so each caller
# anchors it the way it needs.
CREDENTIAL_LABELS = (
    r"密码|口令|password|passcode|api[_ -]?key|access[_ -]?token"
    r"|secret"
)

# What separates such a noun from its value.
ASSIGNMENT = r"\s*(?:是|为|=|:|：)\s*"

# A looser separator for screening stored text. A model rewrites "密码是 X" as
# "密码 X", so a gate that insists on the assignment mark stops recognising a
# value it hid itself. Whitespace alone is safe here because the value must
# still be ASCII-shaped, which "密码 忘了" is not.
SEPARATOR = r"\s*(?:是|为|=|:|：|\s)\s*"

# Vendor-issued tokens, which are credentials on their own with no label.
TOKEN_PATTERN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|github_pat_[A-Za-z0-9_]{8,}|"
    r"gh[pousr]_[A-Za-z0-9]{8,}|AKID[A-Za-z0-9]{8,})\b",
    re.IGNORECASE,
)
