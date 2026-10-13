"""``read(before=...)``'s input validation (issue #300 review): ``clamp_before`` is the pure
check behind it, tested directly since the normal MCP path (``before: int | None`` in the tool
signature) coerces a bool, a numeric string or a float to ``int`` before it ever reaches the
broker -- the broker's own check is what still catches a genuinely wrong type reaching
``agent.read`` another way (a future caller, or a client that doesn't coerce)."""

from __future__ import annotations

import pytest

from switchboard.broker.agents import clamp_before
from switchboard.broker.service import MAX_MESSAGE_ID, ServiceError


@pytest.mark.parametrize("bad", [True, False, "4", 4.0, None, [1], {"a": 1}])
def test_clamp_before_refuses_anything_but_a_plain_int(bad: object) -> None:
    with pytest.raises(ServiceError) as e:
        clamp_before(bad)
    assert e.value.code == "bad_request"


def test_clamp_before_clamps_to_a_sane_range() -> None:
    """A valid int is never refused: it's clamped, the way ``reply_to`` is bounded in
    service.py, so an odd value (negative, or past SQLite's 64-bit integer) still answers
    with an empty or a full page rather than erroring."""
    assert clamp_before(0) == 0
    assert clamp_before(-5) == 0
    assert clamp_before(42) == 42
    assert clamp_before(MAX_MESSAGE_ID) == MAX_MESSAGE_ID
    assert clamp_before(MAX_MESSAGE_ID + 1000) == MAX_MESSAGE_ID
