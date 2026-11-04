"""What an agent's ``@call`` line says aloud in a live call (DESIGN.md §52).

An agent's ``say()`` that starts with ``@call`` is spoken when a person has turned voice on.
The room still gets the message as written. This module is pure: it finds the ``@call``,
and turns Markdown into something a text-to-speech voice can read without spelling out
punctuation, code or addresses.
"""

from __future__ import annotations

import re

from switchboard.envelope import clean
from switchboard.files import parse_artifact_line

SPEAK_MAX = 600  # characters of one spoken line
QUEUE_MAX = 3  # lines waiting while one is spoken
# the one line a batch with call lines carries while voice is on: the only place agents learn it
VOICE_HINT = "Voice is on in this call: start a say() line with @call to have it spoken."

# ``@call`` at the start, and not the front of a longer name (``@call-1``, ``@calling``)
_AT_CALL = re.compile(r"\A\s*@call(?![A-Za-z0-9_-])[\s:,.;-]*", re.IGNORECASE)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_HEADING = re.compile(r"^ {0,3}#{1,6}\s+")
_QUOTE = re.compile(r"^ {0,3}(?:>\s?)+")
_BULLET = re.compile(r"^ {0,3}(?:[-*+]|\d{1,9}[.)])\s+")
_RULE = re.compile(r"^ {0,3}(?:[-*_][ \t]*){3,}$")
_TAG = re.compile(r"</?[A-Za-z][^>]*>")
# emphasis marks at the edge of a word, so ``some_name`` keeps its underscore; strike-through and code ticks
_MARKS = re.compile(r"(?<!\w)[*_]+|[*_]+(?!\w)|~~|`+")
_SPACES = re.compile(r"\s+")


def call_text(text: str) -> str | None:
    """The text after a leading ``@call``, or None when ``text`` does not start with it."""
    found = _AT_CALL.match(text)
    return text[found.end() :] if found else None


def speakable(text: str, limit: int = SPEAK_MAX) -> str:
    """``text`` as it would be read aloud, or "" when nothing is left to say.

    Markdown marks are dropped and a link keeps its words. A fenced code block becomes "code
    omitted", and an artifact line, a bare URL and an HTML tag are left out. Lines are joined
    with a full stop where they had none, so a list is not one long sentence. The result is cut
    at a word boundary within ``limit`` characters.
    """
    pieces: list[str] = []
    fence: str | None = None
    for line in clean(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        opened = _FENCE.match(line)
        if fence is not None:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence):
                fence = None
            continue
        if opened:
            fence = opened.group(1)
            pieces.append("code omitted")
            continue
        if parse_artifact_line(line) is not None or _RULE.match(line):
            continue
        line = _HEADING.sub("", line)
        line = _QUOTE.sub("", line)
        line = _BULLET.sub("", line)
        line = _IMAGE.sub(r"\1", line)
        line = _LINK.sub(r"\1", line)
        line = _URL.sub("", line)
        line = _TAG.sub("", line)
        line = _MARKS.sub("", line)
        line = _SPACES.sub(" ", line).strip()
        if line:
            pieces.append(line)
    spoken = ""
    for piece in pieces:
        if spoken:
            spoken += " " if spoken[-1] in ".!?:;" else ". "
        spoken += piece
    return _cut(spoken.strip(), limit)


def _cut(text: str, limit: int) -> str:
    """At most ``limit`` characters, ending on a whole word and a full stop."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    if text[limit] != " ":  # the cut falls inside a word: back up to the space before it
        space = head.rfind(" ")
        if space > 0:
            head = head[:space]
    head = head.rstrip(" ,;:-")
    return head if head[-1:] in ".!?" else head + "."
