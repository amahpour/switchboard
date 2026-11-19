"""The one rule of who a public room's members are (#843, DESIGN.md §62): ``is_member``, pure."""

from __future__ import annotations

from typing import Any

from switchboard.broker.service import is_member
from switchboard.models import Room


def test_the_membership_rule() -> None:
    """``is_member``, the one rule: a room someone is in; a private room only so; a public room for
    everyone when the broker has one person. Fails if a private room is opened to the one person who is
    not in it, or a public room's members are ignored when there are several people."""
    base: dict[str, Any] = dict(
        created_at=0.0,
        created_by="alice",
        paused=False,
        paused_reason=None,
        budget_per_hour=60,
        budget_remaining=60,
        budget_window_start=0.0,
        budget_notice_window=None,
        hop_count=0,
        hop_limit=6,
        last_msg_at=None,
    )
    pub = Room(id=1, name="#build", **base)
    priv = Room(id=2, name="#secret", private=True, **base)
    assert is_member(pub, {1}, False) and not is_member(pub, set(), False)
    assert is_member(pub, set(), True)  # one person: every public room is theirs
    assert not is_member(priv, set(), True) and is_member(priv, {2: None}, False)
