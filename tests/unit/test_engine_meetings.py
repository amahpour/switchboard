"""A call line in the delivery engine (DESIGN.md §52).

Unmentioned transcript never starts a turn, even after the hold. A call-out wakes
only the agent it named, and the earlier lines ride along. A meeting line does not
move the loop guard's hop count.
"""

from __future__ import annotations

from pathlib import Path

from conftest import FakeClock
from engine_agents import result, waiting, woke
from engine_world import World

from switchboard.config import Config
from switchboard.models import Message


def world(tmp_path: Path, clock: FakeClock) -> World:
    # A short hold, so "past the hold" is a few seconds of the fake clock and not
    # the default four and a half minutes. An ordinary agent line would wake by then.
    cfg = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=1.0, unaddressed_hold_s=5.0)
    return World(tmp_path, clock, cfg)


def meeting(w: World, text: str, mentions: tuple[str, ...] = ()) -> Message:
    msg = w.store.insert_message(
        w.room.id,
        sender_name="call",
        sender_kind="meeting",
        via="meeting",
        text=text,
        mentions=mentions,
    )
    w.actions += w.engine.on_message(msg.id)
    return msg


def test_an_unmentioned_line_never_wakes_an_idle_agent(tmp_path: Path, clock: FakeClock) -> None:
    """Past the hold, the line is still waiting. An agent line would have woken both.
    Fails if a call is treated as chatter that becomes due, or as a person's message."""
    w = world(tmp_path, clock)
    _c, _cm, claude = waiting(w, "claude-1")
    _x, _xm, codex = waiting(w, "codex-1")
    meeting(w, "gas prices are in Los Angeles")
    clock.advance(1000)
    w.actions += w.engine.tick()
    assert not woke(w, claude) and not woke(w, codex)
    assert result(w, claude) is None and result(w, codex) is None


def test_a_call_out_wakes_only_that_agent_and_the_earlier_lines_ride_along(
    tmp_path: Path, clock: FakeClock
) -> None:
    """claude-1 is named, so only claude-1 wakes, and the line from before the call-out
    is in the same batch. codex-1 stays idle. Fails if both wake, or the earlier line
    is left behind, or the envelope doesn't say the line came from the call."""
    w = world(tmp_path, clock)
    _c, _cm, claude = waiting(w, "claude-1")
    _x, _xm, codex = waiting(w, "codex-1")
    meeting(w, "gas prices are in Los Angeles")
    meeting(w, "Claude one, tell me how much", ("claude-1",))
    assert woke(w, claude) and not woke(w, codex)
    text = result(w, claude)["text"]
    assert "kind=meeting" in text and "from=call" in text
    assert "gas prices are in Los Angeles" in text
    assert "tell me how much" in text
    assert "to_you=yes" in text
    room = w.store.room_by_id(w.room.id)
    assert room is not None and room.budget_remaining == room.budget_per_hour - 1


def test_a_meeting_line_does_not_move_the_hop_count(tmp_path: Path, clock: FakeClock) -> None:
    """An agent line counts. A call line neither adds to the count nor resets it, so the
    next agent line is the second hop, not the first and not the third. Fails if a
    transcript is treated as a person (reset) or as an agent (one more hop)."""
    w = world(tmp_path, clock)
    _p, m = w.agent("claude-1", status="idle")
    w.agent_says(m, "one")
    assert w.store.room_by_id(w.room.id).hop_count == 1
    meeting(w, "two")
    assert w.store.room_by_id(w.room.id).hop_count == 1
    w.agent_says(m, "three")
    assert w.store.room_by_id(w.room.id).hop_count == 2
