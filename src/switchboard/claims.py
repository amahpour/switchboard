"""Pure advisory claim policy for work outside a room (DESIGN.md §47, #293)."""

from __future__ import annotations

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
