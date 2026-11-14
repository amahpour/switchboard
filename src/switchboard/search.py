"""Search, the pure part (DESIGN.md §58, #724): what a person typed becomes a safe query, and what the index
answers becomes a snippet with highlight offsets.

Nothing here touches SQL or I/O. The query is input from outside, so it is never handed to the index as
query syntax: every word is a quoted term (a quoted term has no operators in it), terms are ANDed, a
"quoted phrase" is a phrase, and the last bare word also matches as a prefix, so results follow the typing.
Anything the index would read as an operator (``NEAR``, ``OR``, ``-``, ``:``, ``^``, ``*``, a column filter,
parentheses, a stray quote) is either an ordinary word or dropped. A query that leaves no term yields None,
which the caller answers with no results.

The store (``Store.search``) is the one place that knows the index is SQLite's FTS5. What it is given is the
string ``match_query`` builds, and what it returns goes through ``parse_snippet``: a Postgres version (§58.7)
replaces the store's method and the two builders here, and the route and the page stay as they are.
"""

from __future__ import annotations

import re
import unicodedata

from switchboard.envelope import clean

MAX_QUERY_CHARS = 200
MAX_TERMS = 12
MAX_TERM_CHARS = 64
PREFIX_MIN = 2  # a prefix match on one letter would be most of the index
SNIPPET_TOKENS = 24
SNIPPET_MAX = 240  # characters, after the cut to a sentence-sized window
ELLIPSIS = "…"

# runs of letters and digits in any script: what the index calls a token (it splits on everything else, the
# underscore included), so a word with a dash in it is two terms, as the index holds it
_WORD = re.compile(r"[^\W_]+")
# a phrase: straight or curly double quotes around anything without a quote in it
_PHRASE = re.compile(r"[\"“”]([^\"“”]*)[\"“”]")


def _words(text: str) -> list[str]:
    return [w[:MAX_TERM_CHARS] for w in _WORD.findall(text)]


def terms_of(raw: object) -> list[tuple[str, bool]]:
    """The query as a list of ``(text, is_phrase)``: a phrase's text is its words, space-joined. At most
    ``MAX_TERMS`` words in all (a phrase counts its words); a phrase of one word is an ordinary word."""
    if not isinstance(raw, str):
        return []
    # cut before normalizing, so a huge string is never normalized whole
    text = unicodedata.normalize("NFC", raw[:MAX_QUERY_CHARS])[:MAX_QUERY_CHARS]
    out: list[tuple[str, bool]] = []
    used = 0

    def add(words: list[str], phrase: bool) -> None:
        nonlocal used
        words = words[: MAX_TERMS - used]
        if not words:
            return
        used += len(words)
        if phrase and len(words) > 1:
            out.append((" ".join(words), True))
        else:
            out.extend((w, False) for w in words)

    pos = 0
    for m in _PHRASE.finditer(text):
        add(_words(text[pos : m.start()]), False)
        add(_words(m.group(1)), True)
        pos = m.end()
    add(_words(text[pos:]), False)  # an unclosed quote is just its words
    return out


def match_query(raw: object) -> str | None:
    """The index's own query for what the person typed, or None when nothing searchable is in it.

    Every word is wrapped in double quotes: its characters are letters and digits only, so there is no
    quote to close and nothing inside for the index to read as syntax. Terms are joined by a space, which is
    AND. The last term, when it is a bare word of ``PREFIX_MIN`` letters or more, gets a ``*``."""
    terms = terms_of(raw)
    if not terms:
        return None
    parts = [f'"{t}"' for t, _ in terms]
    last, phrase = terms[-1]
    if not phrase and len(last) >= PREFIX_MIN:
        parts[-1] += "*"
    return " ".join(parts)


def words_of(raw: object) -> list[str]:
    """The words of a query, for a page that wants to say what was searched for (phrases are split)."""
    return [w for t, _ in terms_of(raw) for w in t.split(" ")]


def utf16_len(s: str) -> int:
    """Length in UTF-16 code units: what a JavaScript string's offsets count, so a highlight lands right
    after an emoji too."""
    return len(s.encode("utf-16-le")) // 2


def markers(nonce: str) -> tuple[str, str]:
    """The strings the index wraps a hit in. They carry a nonce chosen for this one call, so text that
    contains a control character cannot fake a highlight."""
    return f"\x02{nonce}\x03", f"\x03{nonce}\x02"


def parse_snippet(raw: str, nonce: str) -> tuple[str, list[list[int]]]:
    """``(text, ranges)`` from the index's snippet, which wraps each hit in the markers: the text is
    display-cleaned, its white space collapsed to single spaces and cut to ``SNIPPET_MAX`` characters, and
    each range is ``[start, end)`` in UTF-16 units, never overlapping, never past the end."""
    opening, closing = markers(nonce)
    # the index's own ellipsis at either end is set aside: cleaning the text would spell it "..."
    lead, trail = raw.startswith(ELLIPSIS), raw.endswith(ELLIPSIS)
    raw = raw[int(lead) : len(raw) - int(trail)]
    text = ELLIPSIS if lead else ""
    ranges: list[list[int]] = []
    start: int | None = None
    pos = 0
    for m in re.finditer(re.escape(opening) + "|" + re.escape(closing), raw):
        piece = re.sub(r"\s+", " ", clean(raw[pos : m.start()]))
        text += piece
        pos = m.end()
        if m.group() == opening:
            start = utf16_len(text)
        elif start is not None:
            end = utf16_len(text)
            if end > start:
                ranges.append([start, end])
            start = None
    text += re.sub(r"\s+", " ", clean(raw[pos:])) + (ELLIPSIS if trail else "")
    if len(text) > SNIPPET_MAX:
        text = text[: SNIPPET_MAX - 1].rstrip() + ELLIPSIS
    limit = utf16_len(text) - (1 if text.endswith(ELLIPSIS) else 0)
    kept: list[list[int]] = []
    for a, b in ranges:
        b = min(b, limit)
        if a < b:
            kept.append([a, b])
    return text, kept
