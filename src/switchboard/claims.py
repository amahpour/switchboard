"""Pure advisory claim policy for work outside a room (DESIGN.md §47, #293)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from switchboard.envelope import clean

KEY_MAX = 120
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


@dataclass(frozen=True)
class Decision:
    action: Literal["acquired", "renewed", "released", "held", "limit"]
    holder: str = ""
    expires_at: float = 0.0


def normalize_key(value: object) -> str:
    """One bounded, printable label; whitespace cannot make a second spelling of a key."""
    if not isinstance(value, str):
        raise ClaimError("claim key must be text")
    key = " ".join(clean(value).split())[:KEY_MAX].strip()
    if not key:
        raise ClaimError("claim key is required")
    return key


def normalize_ttl(value: object) -> int:
    """Zero releases. A positive duration is capped at a day; negative never means release."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ClaimError("claim ttl must be a nonnegative whole number of seconds")
    return min(value, TTL_MAX_S)


def decide(held: Sequence[Claim], member_id: int, key: str, ttl: int, now: float) -> Decision:
    """Choose a move from active claims at ``now``; no I/O and no hidden time source."""
    active = [c for c in held if c.expires_at > now]
    existing = next((c for c in active if c.key.casefold() == key.casefold()), None)
    if existing is not None and existing.membership_id != member_id:
        return Decision("held", holder=existing.holder, expires_at=existing.expires_at)
    if ttl == 0:
        return Decision("released")
    if existing is not None:
        return Decision("renewed", holder=existing.holder, expires_at=now + ttl)
    if sum(c.membership_id == member_id for c in active) >= MEMBER_MAX:
        return Decision("limit")
    return Decision("acquired", expires_at=now + ttl)
