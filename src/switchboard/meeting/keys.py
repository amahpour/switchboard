"""A hosted call's key, and what is said of it (DESIGN.md §54, #623).

A person's machine can run the meeting bot for one call and push what it hears into a room. What lets it
is a key made for that one call, on that machine, like a listener key (§49.11): the host script makes
``sbc_`` and 64 hex in a file only its owner reads and never shows it to a model. The script registers
only its sha256, at ``POST /mcp/meeting/host``, and the server keeps only that. The key is a bearer
for the call's three routes and nothing else, and ends with the call.

Everything here is pure: the shapes, the hash, the fingerprint a person can check by eye (the relay prints
the same eight characters when it starts), and the values a claim from outside is held to.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from switchboard.envelope import clean

PREFIX = "sbc_"  # apart from the agent token's ``sbat_`` and the listener key's ``sbl_``
KEY_RE = re.compile(r"sbc_[0-9a-f]{64}")
HASH_RE = re.compile(r"[0-9a-f]{64}")
PLATFORMS = ("zoom", "meet", "teams", "unknown")  # a hosted call may not say which it is on
MODEL_MAX = 80  # the model's name is the host's claim, cut


def key_hash(key: str) -> str:
    """The sha256 of the whole key, ``sbc_`` included, in lower-case hex: the only form the server keeps."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def hash_in(value: Any) -> str | None:
    """``value`` as a key hash when it is 64 lower-case hex characters, else None. A key itself
    is not one, so a key given where its hash belongs is refused, not kept."""
    return value if isinstance(value, str) and HASH_RE.fullmatch(value) is not None else None


def fingerprint(h: str) -> str:
    """The first eight hex characters of a key's sha256 as ``7f3a·91c2``: short enough to read
    out and compare, long enough that two of a person's calls differ. It shows nothing that
    could be used as the key: the hash is itself a one-way image of it."""
    return f"{h[:4]}·{h[4:8]}"


def platform_in(value: Any) -> str | None:
    """The platform a host says its call is on, if it is one of ``PLATFORMS``. Anything else is
    refused, so a value this code does not know is never kept as one it does."""
    word = value.strip().lower() if isinstance(value, str) else ""
    return word if word in PLATFORMS else None


def model_in(value: Any) -> str:
    """The speech model a host says it transcribes with: text, cleaned, cut to ``MODEL_MAX``."""
    return " ".join(clean(value).split())[:MODEL_MAX] if isinstance(value, str) else ""
