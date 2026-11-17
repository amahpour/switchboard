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

# What of an ``@call`` message is spoken (#761). A listener cannot skim, and joinly makes the whole
# line into speech before it plays any of it, so a long line is silence and then a monologue. The
# first three sentences, at most 300 characters (about 20 seconds aloud), are the answer; the room
# still has the whole message as text.
SPEAK_SENTENCES = 3
SPEAK_MAX = 300
# What a batch with call lines carries while voice is on: the only place agents learn how to speak.
# VOICE_HINT stays short (a test holds it under 90 characters); VOICE_STYLE says how, before it.
VOICE_STYLE = (
    "On a call, talk, don't read: after @call, one or two short sentences, no lists, tables, code or links,"
    ' then stop or offer more ("want the details?"). Put details in a normal say() without @call.'
    ' If you need a tool first, say "one moment" with @call.'
)
# What is spoken in place of the part that is not (#761): a listener who hears a cut sentence-end and then
# silence takes the line for finished. The pointer is its own short sentence, spoken after the kept ones, and
# it is not counted in ``SPEAK_MAX``.
SPOKEN_POINTER = "The rest is in the room."
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
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=\S)")
# an abbreviation's full stop does not end a sentence
_ABBREVIATIONS = ("e.g.", "i.e.", "vs.", "mr.", "mrs.", "ms.", "dr.", "st.")


def call_text(text: str) -> str | None:
    """The text after a leading ``@call``, or None when ``text`` does not start with it."""
    found = _AT_CALL.match(text)
    return text[found.end() :] if found else None


def speakable(text: str, limit: int = SPEAK_MAX, sentences: int = SPEAK_SENTENCES) -> str:
    """``text`` as it would be read aloud, or "" when nothing is left to say.

    Markdown marks are dropped and a link keeps its words. A fenced code block becomes "code
    omitted", and an artifact line, a bare URL and an HTML tag are left out. Lines are joined
    with a full stop where they had none, so a list is not one long sentence. Only the first
    ``sentences`` sentences are kept, as many as fit in ``limit`` characters, so the cut always
    falls between sentences. One sentence longer than ``limit`` is cut at a word instead.
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
    return _first_sentences(spoken.strip(), limit, sentences)


def call_line(text: str) -> str:
    """What a call speaks for an ``@call`` message's text: ``speakable``, and when it left something out
    (more sentences or characters than the cap, or one sentence cut at a word) ``SPOKEN_POINTER`` after it, so
    the listener knows there is more and where it is. "" when nothing is left to say, and the whole text when
    it fits. The room always has the message as written."""
    kept = speakable(text)
    if not kept:
        return ""
    if speakable(text, limit=len(text) + len(kept) + 1, sentences=len(text) + 1) == kept:
        return kept
    return f"{kept} {SPOKEN_POINTER}"


def split_sentences(text: str) -> list[str]:
    """``text`` as sentences: split after ``.``, ``!`` or ``?`` followed by a space.

    A full stop inside a number or an address has no space after it, so it never splits; the
    common abbreviations are kept whole. A sentence keeps its end mark.
    """
    parts: list[str] = []
    for part in _SENTENCE_END.split(text):
        if parts and parts[-1].lower().endswith(_ABBREVIATIONS):
            parts[-1] += " " + part
        else:
            parts.append(part)
    return parts


def _first_sentences(text: str, limit: int, count: int) -> str:
    """The first ``count`` sentences of ``text`` that fit in ``limit`` characters, whole."""
    if len(text) <= limit and count >= len(split_sentences(text)):
        return text
    kept: list[str] = []
    for sentence in split_sentences(text)[: max(count, 1)]:
        if len(" ".join([*kept, sentence])) > limit:
            break
        kept.append(sentence)
    if not kept:  # the first sentence alone is too long: a word boundary is the best there is
        return _cut(text, limit)
    return " ".join(kept)


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
