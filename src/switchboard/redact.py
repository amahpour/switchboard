"""Obvious secrets cut out of a chat message before it is stored (DESIGN.md §11, item 10, #819).

A transcript outlives the session, and people paste secrets into it, or agents echo them. The step that stores
a message (``Service.human_say`` for a person, ``AgentApi.say`` for an agent) calls ``redact`` first, so a
secret never reaches the database, the search index, the WebSocket or another agent. The patterns are fixed
here: a room message never changes them.

**Best effort, and tightened only by evidence.** Only shapes that are obviously a secret match, so an ordinary
sentence, a hash in a code block, a git SHA or a UUID is left alone: a well-known key prefix followed by a
long body that has a digit in it, ``Bearer`` and a long token that has a digit, a private-key block, and a
``password=``-style assignment with a real-looking value. A secret that fits none of them is stored as
written.
This does not replace not pasting secrets. The module is pure: no clock, no I/O, and it never logs a value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The marker that takes a secret's place: what kind of value it was, never the value.
MARKER = "[redacted: {kind}]"

_BODY = r"[A-Za-z0-9_\-]"


@dataclass(frozen=True)
class Pattern:
    """One kind of secret: ``kind`` is what the sender is told and what the marker says; ``regex``'s group 1,
    when it has one, is the part replaced (the rest of the match, a ``Bearer`` or a key's name, stays)."""

    kind: str
    regex: re.Pattern[str]


# One list, one test table (tests/unit/test_redact.py). A new pattern needs a hit and a miss there.
PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        "private key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]{0,30}PRIVATE KEY(?: BLOCK)?-----"
            r"(?:[\s\S]{0,8000}?-----END [A-Z0-9 ]{0,30}PRIVATE KEY(?: BLOCK)?-----"
            r"|(?:\s*[A-Za-z0-9+/=]{20,}){1,200})"  # a header with no footer: the base64 lines under it
        ),
    ),
    # sk-, sk-ant-, sk-proj-: 20 or more characters, at least one a digit ("sk-learn-..." is a word)
    Pattern("api key", re.compile(rf"\bsk-(?=[A-Za-z0-9_\-]*\d){_BODY}{{20,}}")),
    Pattern("api key", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{16,}")),
    Pattern("api key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    Pattern("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    Pattern("github token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}")),
    Pattern("gitlab token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}")),
    Pattern("aws access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    Pattern("slack token", re.compile(r"\bxox[abprs]-(?=[A-Za-z0-9\-]*\d)[A-Za-z0-9\-]{10,}")),
    Pattern("switchboard token", re.compile(r"\bsb(?:at|l|c)_[A-Za-z0-9_\-]{30,}")),
    Pattern(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    ),
    # "Bearer <token>": the word stays; a long token with a digit in it goes (not "Bearer authentication")
    Pattern(
        "bearer token",
        re.compile(r"\b[Bb]earer\s+((?=[A-Za-z0-9._~+/\-]*\d)[A-Za-z0-9._~+/\-]{20,}={0,2})"),
    ),
    # password=hunter22, "api_key": "...", GITHUB_TOKEN=...: the name stays, a value of 8 or more characters
    # that is not a placeholder (<token>, $TOKEN, ${TOKEN}, %TOKEN%, ***, ..., [x]) goes
    Pattern(
        "password or token",
        re.compile(
            r"(?i)\b[\w.\-]*(?:password|passwd|secret|token|api[_\-]?key|apikey|access[_\-]?key)\b[\"']?"
            r"\s*[=:]\s*[\"']?((?![$<{%*.\[\s])[^\s\"'`,;&<>(){}\[\]]{8,})"
            # a colon alone is prose ("token: the value"): it needs a quote, so only an assignment goes
        ),
    ),
)

_NOT_A_VALUE = re.compile(r"(?i)^(?:true|false|null|none|undefined|redacted|changeme|example|placeholder)\b")


def _assignment_ok(m: re.Match[str]) -> bool:
    """A ``name=value`` match only counts with ``=``, or a ``:`` that follows a quote (JSON),
    and a value that is not a word like ``true`` or ``changeme``."""
    sep = m.group(0)[: m.start(1) - m.start(0)]
    colon_only = "=" not in sep
    if colon_only and not re.search(r"[\"']\s*:\s*[\"']?$", sep):
        return False
    return not _NOT_A_VALUE.match(m.group(1)) and not m.group(1).startswith(("[redacted", "redacted"))


def redact(text: str) -> tuple[str, list[str]]:
    """``text`` with each obvious secret replaced by ``[redacted: <kind>]``, and the kinds removed, in the
    order they were found, once each. Text with none comes back unchanged and with no kinds."""
    kinds: list[str] = []
    for pat in PATTERNS:

        def sub(m: re.Match[str], pat: Pattern = pat) -> str:
            if pat.regex.groups and pat.kind == "password or token" and not _assignment_ok(m):
                return m.group(0)
            if pat.kind not in kinds:
                kinds.append(pat.kind)
            marker = MARKER.format(kind=pat.kind)
            if not pat.regex.groups:
                return marker
            s = m.start(1) - m.start(0)
            return m.group(0)[:s] + marker + m.group(0)[m.end(1) - m.start(0) :]

        text = pat.regex.sub(sub, text)
    return text, kinds


def notice(kinds: list[str]) -> str:
    """What the sender is told: the kinds, never a value."""
    what = ", ".join(kinds)
    return (
        f"removed what looks like a secret ({what}) from your message before it was posted. "
        "Redaction is best effort: don't paste secrets."
    )
