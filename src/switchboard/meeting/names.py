"""Spoken forms of a screen name, and which member a transcript line addresses (DESIGN.md §52).

Speech-to-text writes "Claude one" or "claude 1", never ``@claude-1``. A bare "Claude"
matches when exactly one member's name starts with it. Nothing here reads a clock or
the store: the bridge passes the room's members in.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

# one to twenty: past that, only the digits ("claude 21"), which is what was asked for
_WORDS = (
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
    "twenty",
)
_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.IGNORECASE)
_LEAD = re.compile(r"(?:hey|hi|okay|ok)\b[\s,]*", re.IGNORECASE)


@dataclass(frozen=True)
class Match:
    """``one`` names ``name``. ``ambiguous`` names every member the phrase could be.
    ``none`` addressed nobody. ``heard`` is the phrase as the transcript wrote it."""

    outcome: str
    name: str | None = None
    heard: str | None = None
    names: tuple[str, ...] = ()


def _norm(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def base_of(name: str) -> str:
    """The name without a trailing number: ``claude-1`` and ``claude`` are both ``claude``."""
    parts = _norm(name).split("-")
    if len(parts) >= 2 and parts[-1].isdigit():
        parts = parts[:-1]
    return " ".join(p for p in parts if p)


def spoken_forms(name: str) -> tuple[str, ...]:
    """How speech-to-text writes ``name``. ``claude-1`` is "claude one", "claude 1",
    "claude-1" and "claude number one". A digit past twenty stays a digit."""
    raw = _norm(name)
    forms: list[str] = []

    def add(form: str) -> None:
        form = " ".join(form.split())
        if form and form not in forms:
            forms.append(form)

    add(raw)
    spaced = raw.replace("-", " ")
    add(spaced)
    parts = spaced.split()
    if parts and parts[-1].isdigit():
        n = int(parts[-1])
        stem = " ".join(parts[:-1])
        add(f"{stem} {n}" if stem else str(n))
        if 1 <= n <= len(_WORDS):
            word = _WORDS[n - 1]
            add(f"{stem} {word}" if stem else word)
            add(f"{stem} number {word}" if stem else f"number {word}")
    return tuple(forms)


def hotwords(names: Iterable[str]) -> list[str]:
    """The transcriber's hints: every spoken form, and the bare base, once each.
    First letter capitalised, the way the names are said."""
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        for form in (*spoken_forms(name), base_of(name)):
            shown = form[:1].upper() + form[1:] if form else form
            key = shown.lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(shown)
    return out


def _tokens(text: str) -> list[str]:
    return [m.group(0).lower() for m in _TOKEN.finditer(text)]


def _prefixes(words: list[str]) -> list[str]:
    """Longest run first, at most four words: a spoken form is that short."""
    out = []
    for n in range(min(4, len(words)), 0, -1):
        out.append(" ".join(words[:n]))
    return out


def _phrases(text: str) -> list[str]:
    """Address phrases, longest first. A name counts only at the start of the line
    (after hey/hi/ok/okay), after one of those words, or immediately before a comma.
    "I asked Claude yesterday" contributes none of those."""
    found: list[str] = []

    def take(words: list[str]) -> None:
        for phrase in _prefixes(words):
            if phrase not in found:
                found.append(phrase)

    low = text.strip()
    start = _LEAD.sub("", low, count=1)
    take(_tokens(start))
    for m in _LEAD.finditer(low):
        rest = low[m.end() :]
        take(_tokens(rest))
    for m in re.finditer(r"([a-z0-9]+(?:-[a-z0-9]+)*(?:\s+[a-z0-9]+(?:-[a-z0-9]+)*){0,3})\s*,", low, re.I):
        words = _tokens(m.group(1))
        for n in range(len(words), 0, -1):
            phrase = " ".join(words[-n:])
            if phrase not in found:
                found.append(phrase)
    found.sort(key=len, reverse=True)
    return found


def _heard(text: str, phrase: str) -> str:
    m = re.search(r"\b" + re.escape(phrase) + r"\b", text, re.IGNORECASE)
    return m.group(0) if m else phrase


def _starts(screen: str, phrase: str) -> bool:
    """A bare base, whole tokens only: ``claude`` starts ``claude-1``, not ``claudette``."""
    if not phrase or " " in phrase or "-" in phrase:
        return False
    s = screen.lower()
    p = phrase.lower()
    if s == p:
        return True
    return s.startswith(p) and not s[len(p)].isalpha()


_ECHO_RUN = 3  # hint words in a row
_BOT_WORDS = frozenset({"switchboard", "transcribing"})  # joinly adds its own name to the hints


def is_echo(text: str, members: Iterable[str]) -> bool:
    """True when ``text`` looks like the transcriber writing out its own hint list.

    During unclear audio Whisper sometimes writes the hotwords back ("Claude Claude one
    Codex Cursor Devin switchboard", seen twice in the spike for #573). That is not a
    call-out: a run of three or more hint words in a row that names two or more
    different members, or the participant's own name with a member, wakes nobody."""
    names = [n for n in members if n]
    owners: dict[str, set[str]] = {}
    for name in names:
        base = base_of(name)
        for form in (*spoken_forms(name), base):
            for tok in _tokens(form):
                owners.setdefault(tok, set()).add(base)
    run: list[str] = []
    bases: set[str] = set()

    def echo() -> bool:
        if len(run) < _ECHO_RUN:
            return False
        bot = any(t in _BOT_WORDS for t in run)
        return len(bases) >= 2 or (bot and len(bases) >= 1)

    for tok in _tokens(text):
        if tok in owners or tok in _BOT_WORDS:
            run.append(tok)
            bases |= owners.get(tok, set())
            continue
        if echo():
            return True
        run, bases = [], set()
    return echo()


def match(text: str, members: Iterable[str]) -> Match:
    """The one member ``text`` addresses, or ambiguous, or none.

    A spoken form ("claude one") wins over the bare base. The bare base matches when
    exactly one member's name starts with it; two members ask who was meant.
    """
    names = [n for n in members if n]
    if not text or not names or is_echo(text, names):
        return Match("none")
    forms = {n: set(spoken_forms(n)) for n in names}
    for phrase in _phrases(text):
        specific = [n for n in names if phrase in forms[n]]
        if len(specific) == 1:
            return Match("one", name=specific[0], heard=_heard(text, phrase))
        if len(specific) > 1:
            return Match("ambiguous", heard=_heard(text, phrase), names=tuple(specific))
        hits = [n for n in names if _starts(n, phrase)]
        if len(hits) == 1:
            return Match("one", name=hits[0], heard=_heard(text, phrase))
        if len(hits) > 1:
            return Match("ambiguous", heard=_heard(text, phrase), names=tuple(hits))
    return Match("none")
