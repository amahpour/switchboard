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
    ``none`` addressed nobody. ``heard`` is the phrase as the transcript wrote it.

    ``text`` is the line as the room should read it: an echoed hint list taken out of it (#602).
    ``echo`` is true when nothing else was said, and ``text`` is then empty: it is not posted (#616)."""

    outcome: str
    name: str | None = None
    heard: str | None = None
    names: tuple[str, ...] = ()
    text: str = ""
    echo: bool = False


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
_SEPARATORS = " \t,;:.-"


@dataclass(frozen=True)
class _Echo:
    """A line with the transcriber's hint list in it, and what is left of the line (``_find_echo``).
    ``shown`` is what the room reads, empty when the line was nothing else. ``said`` is what an
    address is looked for in, empty when the echo leaves no name to address."""

    shown: str
    said: str


def _hints(names: Iterable[str]) -> tuple[dict[str, set[str]], set[str]]:
    """Every word the transcriber is told to listen for, with the members it is a name of, and
    every way a member's name is said, as the words it is made of."""
    owners: dict[str, set[str]] = {}
    said: set[str] = set()
    for name in names:
        base = base_of(name)
        for form in (*spoken_forms(name), base):
            said.add(" ".join(_tokens(form)))
            for tok in _tokens(form):
                owners.setdefault(tok, set()).add(base)
    return owners, said


def _runs(text: str, owners: dict[str, set[str]]) -> list[tuple[list[re.Match[str]], set[str]]]:
    """Each run of hint words in a row, with the members whose names are in it."""
    out: list[tuple[list[re.Match[str]], set[str]]] = []
    run: list[re.Match[str]] = []
    bases: set[str] = set()
    for m in _TOKEN.finditer(text):
        tok = m.group(0).lower()
        if tok in owners or tok in _BOT_WORDS:
            run.append(m)
            bases |= owners.get(tok, set())
        elif run:
            out.append((run, bases))
            run, bases = [], set()
    if run:
        out.append((run, bases))
    return out


def _is_echo_run(run: list[re.Match[str]], bases: set[str], whole_line: bool) -> bool:
    """Whether ``run`` is the transcriber writing out its hint list. Three hint words in a row that
    name two members, or one member and the bot's own word, are. A line that is nothing but hint
    words is, at any length, once it has a member's name and the bot's own word (#616): no one
    says "Claude switchboard" as a call-out. Names alone ("Claude one") are a call-out."""
    bot = any(m.group(0).lower() in _BOT_WORDS for m in run)
    if len(run) >= _ECHO_RUN and (len(bases) >= 2 or (bot and bool(bases))):
        return True
    return whole_line and bot and bool(bases)


def _words(text: str) -> str:
    """``text`` trimmed, or nothing when it has no word in it, only punctuation."""
    return text.strip() if _TOKEN.search(text) else ""


def _find_echo(text: str, names: list[str]) -> _Echo | None:
    """The first echoed hint list in ``text`` and what is left of the line without it, or None.

    Whisper sometimes writes its hint list back (DESIGN.md §52). Alone it is a line nobody said:
    ``shown`` is empty. In front of what was really said it is a lead-in (#602): on a real call
    "Okay, Claude one, can you hear me?" came out as "Claude-1 Claude switchboard Claude one can you
    hear me?". The run is taken out, but the name right before the real words stays, as said
    ("Claude one can you hear me?"): that name is the address. A run that no real words follow, or
    that doesn't end in a name, addresses nobody. What was said before the run stays in ``shown``
    and is not looked at for an address."""
    owners, names_said = _hints(names)
    for run, bases in _runs(text, owners):
        lead, rest = text[: run[0].start()], text[run[-1].end() :]
        if not _is_echo_run(run, bases, not _TOKEN.search(lead) and not _TOKEN.search(rest)):
            continue
        tail, said = _words(rest).lstrip(_SEPARATORS), ""
        if tail:
            words = [m.group(0).lower() for m in run]
            for n in range(min(4, len(words)), 0, -1):
                if " ".join(words[-n:]) in names_said:
                    said = tail = text[run[-n].start() :].strip()
                    break
        return _Echo(shown=" ".join(p for p in (_words(lead), tail) if p), said=said)
    return None


def is_echo(text: str, members: Iterable[str]) -> bool:
    """True when ``text`` is nothing but the transcriber writing out its hint list.

    During unclear audio Whisper sometimes writes the hotwords back ("Claude Claude one
    Codex Cursor Devin switchboard", seen twice in the spike for #573). That is not a
    call-out and not speech: it wakes nobody and is not posted."""
    return match(text, members).echo


def match(text: str, members: Iterable[str]) -> Match:
    """The one member ``text`` addresses, or ambiguous, or none.

    A spoken form ("claude one") wins over the bare base. The bare base matches when
    exactly one member's name starts with it; two members ask who was meant. An echoed
    hint list is taken out of the line first (``_find_echo``, #602, #616).
    """
    names = [n for n in members if n]
    if not text or not names:
        return Match("none", text=text)
    echoed = _find_echo(text, names)
    shown, said = (text, text) if echoed is None else (echoed.shown, echoed.said)
    if not said:
        return Match("none", text=shown, echo=not shown)
    forms = {n: set(spoken_forms(n)) for n in names}
    for phrase in _phrases(said):
        specific = [n for n in names if phrase in forms[n]]
        if len(specific) == 1:
            return Match("one", name=specific[0], heard=_heard(said, phrase), text=shown)
        if len(specific) > 1:
            return Match("ambiguous", heard=_heard(said, phrase), names=tuple(specific), text=shown)
        hits = [n for n in names if _starts(n, phrase)]
        if len(hits) == 1:
            return Match("one", name=hits[0], heard=_heard(said, phrase), text=shown)
        if len(hits) > 1:
            return Match("ambiguous", heard=_heard(said, phrase), names=tuple(hits), text=shown)
    return Match("none", text=shown)
