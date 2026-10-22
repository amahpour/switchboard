"""A SessionStart report does not end the listener that the same start opened (DESIGN.md §49.4, #484),
with a FakeClock. Claude Code runs the plugin's two SessionStart hooks, ``bin/session`` (the report) and
``bin/listen`` (the listener), at the same time. The listener's first round counts as a Stop and parks;
if the report arrived after it, the engine ended that park as "a hook of the session" and the session was
deaf until its next Stop."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock
from engine_agents import listener, result, session, stop, woke
from engine_world import World

from switchboard.config import Config


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def test_a_session_start_after_the_listeners_first_round_leaves_the_park_to_wake_it(w: World) -> None:
    """The order the live tests hit: quit (SessionEnd), then the resumed session's listener parks, then
    its SessionStart report arrives. Fails if the report ends the park (the @mention then queues as
    "stopped and not listening" and nothing wakes the session)."""
    p, m = session(w, "claude-1", "claude", status="idle")
    w.hook(p, "SessionEnd")
    assert w.p(p).status == "offline"
    out = stop(w, p)  # the listener's first round, ahead of the report
    assert out is not None and out.kind == "park" and w.p(p).status == "idle"
    w.hook(p, "SessionStart", source="resume")  # the report, late
    assert result(w, out) is None, "the SessionStart report ended the listener's park"
    assert [s.id for s in w.engine.sinks.parks_for(p.id)] == [out.sink_id]
    assert w.engine.parked_reason(m.id) is None  # Members does not call it deaf
    w.human("@claude-1 are you there")
    assert woke(w, out)


def test_the_report_first_order_still_works(w: World) -> None:
    """The other order: the report, then the listener's first round. The session is idle and parks once."""
    p, m = session(w, "claude-1", "claude", status="idle")
    w.hook(p, "SessionEnd")
    w.hook(p, "SessionStart", source="resume")
    assert w.p(p).status == "idle"
    out = stop(w, p)
    assert out is not None and out.kind == "park"
    w.human("@claude-1 are you there")
    assert woke(w, out)


def test_the_session_start_report_still_sets_the_session_idle(w: World) -> None:
    """Not ending the park must not change what the report is for: an offline member comes back idle."""
    p, _m = session(w, "claude-1", "claude", status="idle")
    w.hook(p, "SessionEnd")
    w.hook(p, "SessionStart", source="startup")
    assert w.p(p).status == "idle"


@pytest.mark.parametrize("event", ["UserPromptSubmit", "PostToolUse", "SessionEnd"])
def test_any_other_hook_still_ends_the_park(w: World, event: str) -> None:
    """The person typed, a tool ran, or the session quit: the conversation moved on, so the parked
    listener ends (§9.4). Fails if the exception for SessionStart spreads to the other hooks."""
    p, _m, park = listener(w, "claude-1", "claude")
    assert result(w, park) is None
    w.hook(p, event)
    assert result(w, park) is not None, f"{event} left the park open"
