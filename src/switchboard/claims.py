"""Pure advisory claim policy for work outside a room (DESIGN.md §47, #293).

Issue #541 adds the other direction: a claim is also what its holder watches. A message
that names a live claim is addressed to the holder, the same as an @mention of it
(``watchers``, DESIGN.md §8.1, §47). The matching is here, pure; the SQL that supplies the
live claims and writes the delivery rows is ``Store.insert_message``.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal, Sequence

from switchboard.envelope import clean, visible

KEY_MAX = 120
NOTE_MAX = 160  # the status line's cap: a state, a link, or both
TTL_MAX_S = 24 * 3600
MEMBER_MAX = 8


class ClaimError(ValueError):
    pass


@dataclass(frozen=True)
class Claim:
    key: str
    membership_id: int
    holder: str
    expires_at: float
    note: str = ""


@dataclass(frozen=True)
class Decision:
    action: Literal["acquired", "renewed", "released", "held", "limit"]
    holder: str = ""
    expires_at: float = 0.0
    note: str = ""


def normalize_key(value: object) -> str:
    """One bounded, printable label; whitespace cannot make a second spelling of a key."""
    if not isinstance(value, str):
        raise ClaimError("claim key must be text")
    key = " ".join(clean(value).split())[:KEY_MAX].strip()
    if not key:
        raise ClaimError("claim key is required")
    return key


def normalize_note(value: object) -> str:
    """A claim's state or a link. Empty stays empty.

    Cleaned the way ``sanitize`` cleans a string before it quotes it (``visible``:
    NFKC, controls dropped, ``yk:`` defanged), then whitespace collapsed and cut
    to ``NOTE_MAX``, the status line's cap. A non-string is refused.
    """
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ClaimError("claim note must be text")
    return " ".join(visible(value).split())[:NOTE_MAX]


def normalize_ttl(value: object) -> int:
    """Zero releases. A positive duration is capped at a day; negative never means release."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ClaimError("claim ttl must be a nonnegative whole number of seconds")
    return min(value, TTL_MAX_S)


def decide(held: Sequence[Claim], member_id: int, key: str, ttl: int, now: float, note: str = "") -> Decision:
    """Choose a move from active claims at ``now``; no I/O and no hidden time source.

    ``note`` is already cleaned. Empty on a renewal keeps the stored note; a non-empty
    one replaces it. Release drops the row, so the note goes with it. Another member's
    note is ignored with the refusal.
    """
    active = [c for c in held if c.expires_at > now]
    existing = next((c for c in active if c.key.casefold() == key.casefold()), None)
    if existing is not None and existing.membership_id != member_id:
        return Decision("held", holder=existing.holder, expires_at=existing.expires_at)
    if ttl == 0:
        return Decision("released")
    if existing is not None:
        kept = note if note else existing.note
        return Decision("renewed", holder=existing.holder, expires_at=now + ttl, note=kept)
    if sum(c.membership_id == member_id for c in active) >= MEMBER_MAX:
        return Decision("limit")
    return Decision("acquired", expires_at=now + ttl, note=note)


# ------------------------------------------------------------------- watching (#541, §8.1)
# The sender kinds whose text can address a claim's holder. These are the ones a message can
# come from today; a kind this code doesn't know addresses nobody by a claim (fail closed).
# A system notice is never one of them: the broker's own lines name rooms and people, not work.
WATCH_SENDERS = frozenset({"human", "agent", "meeting"})
# Only this much of a message is scanned. A say is capped at ``max_msg_chars`` (4,000 by default)
# long before this, so the cap bounds the work for a caller that doesn't cap, and no more.
SCAN_MAX = 20_000
# Punctuation that identifiers, paths, URLs and issue references are built from. Next to a
# letter, a digit or ``_`` it continues a token (``x.py``, ``feat/x``, ``org/repo#12``,
# ``vlc-cli!56``); with nothing of that kind beyond it, it is only the end of a sentence
# or a list (``!56.``, ``!56?``, ``SYSINT-645:``). Every other character ends a token:
# white space, brackets, quotes, commas, semicolons, backticks and asterisks (Markdown).
JOINERS = frozenset("-./#!:@?&=%+~")


def fold(text: str) -> str:
    """The form a message and a key are compared in: what the agent would be shown (``clean``:
    NFKC, controls and zero-width characters dropped), case-folded, white space collapsed.
    Only the first ``SCAN_MAX`` characters are looked at."""
    return " ".join(clean(text[:SCAN_MAX]).casefold().split())


def _word(c: str) -> bool:
    return c.isalnum() or c == "_"


def _glued_left(text: str, start: int) -> bool:
    """Does something continue the token to the left of ``text[start]``?"""
    if start == 0:
        return False
    prev = text[start - 1]
    return _word(prev) or (prev in JOINERS and start >= 2 and _word(text[start - 2]))


def _glued_right(text: str, end: int) -> bool:
    """Does something continue the token to the right of ``text[end - 1]``?"""
    nxt = text[end : end + 1]
    return _word(nxt) or (nxt in JOINERS and _word(text[end + 1 : end + 2]))


def names_key(folded_text: str, key: str) -> bool:
    """True when ``folded_text`` (``fold`` of a message) has ``key`` as a whole token (DESIGN.md §47).

    The key is folded the same way. An occurrence counts when nothing continues it on either side
    (``_glued_left``, ``_glued_right``): ``!56`` is in ``see !56.`` and ``(!56)``, and is not in
    ``!567``, ``x!56`` or ``vlc-cli!56``; ``feat/x`` is not in ``feat/x.py`` or ``origin/feat/x``.
    The key is the holder's own spelling, so a longer path or a different project's ``!56`` is
    another thing. A key with no letter or digit is never a token.
    """
    k = fold(key)
    if not any(c.isalnum() for c in k):
        return False
    start = folded_text.find(k)
    while start != -1:
        end = start + len(k)
        if not (_glued_left(folded_text, start) or _glued_right(folded_text, end)):
            return True
        start = folded_text.find(k, start + 1)
    return False


def watchers(
    sender_kind: str,
    text: str,
    held: Sequence[Claim],
    now: float,
    exclude: Collection[int] = (),
) -> frozenset[int]:
    """The memberships a message addresses by naming one of their live claims (#541).

    Live is ``expires_at > now``: the deadline is the end, as in ``decide``. ``exclude`` are
    memberships that never are (the sender: a message does not address its own author
    through its own claim). Only a person, an agent or a call can address anyone this way
    (``WATCH_SENDERS``); the reach is that of an @mention and no more, so the caller
    classifies each as it classifies an @mentioned member.
    """
    if sender_kind not in WATCH_SENDERS:
        return frozenset()
    live = [c for c in held if c.expires_at > now and c.membership_id not in exclude]
    if not live:
        return frozenset()
    folded = fold(text)
    return frozenset(c.membership_id for c in live if names_key(folded, c.key))
