"""Wake immediately (DESIGN.md §8.2): a message from the human,
or an @mention of an agent, wakes an idle agent at once, on every wake path, with
no quiet period (the room's 3 s quiet period, and the hold on unaddressed chatter (#137),
apply only to chatter). The paths over HTTPS (§49.4): a Stop hook the broker holds (Claude Code,
Codex, Cursor) and a ``wait()`` call (Devin, the scripted test agent)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock
from engine_agents import listener, waiting, woke
from engine_world import World

from switchboard.config import Config


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock)  # real defaults: quiet_s = 3


def everyone(w: World) -> dict[str, Any]:
    """One idle member per wake path: three parked Stop hooks and two ``wait()`` calls."""
    out: dict[str, Any] = {}
    for name in ("claude", "codex", "cursor"):
        _p, _m, park = listener(w, f"{name}-1", name)
        out[name] = park
    for name, harness in (("test", "test"), ("devin", "devin")):
        _p, _m, sink = waiting(w, f"{name}-1", harness)
        out[name] = sink
    return out


def woken(w: World, e: dict[str, Any]) -> set[str]:
    w.take()
    return {name for name, sink in e.items() if woke(w, sink)}


def test_a_human_message_wakes_every_path_at_once(w: World) -> None:
    e = everyone(w)
    w.human("hello, everyone")  # no clock advance at all
    assert woken(w, e) == {"claude", "codex", "test", "cursor", "devin"}


def test_an_at_mention_wakes_only_its_target_at_once(w: World) -> None:
    e = everyone(w)
    _pp, peer = w.agent("peer")
    w.agent_says(peer, "@codex-1 @test-1 @cursor-1 over to you", mentions=("codex-1", "test-1", "cursor-1"))
    assert woken(w, e) == {"codex", "test", "cursor"}


def test_chatter_alone_waits_for_the_hold_unless_the_agent_is_in_wait(w: World, clock: FakeClock) -> None:
    """Issue #137: agents' chatter that addresses nobody waits ``unaddressed_hold_s`` (270 s)
    before it wakes an idle agent, on every path that starts a turn. An agent already in its
    own wait() asked for messages, so it still gets them after the quiet period: holding them
    would only time its call out. Fails if the hold is skipped (all five wake at 3 s) or if it
    is applied to wait() too (the test agent and Devin stay empty)."""
    e = everyone(w)
    _pp, peer = w.agent("peer")
    w.agent_says(peer, "just thinking out loud")
    assert woken(w, e) == set()
    clock.advance(3.0)
    w.actions += w.engine.tick()
    assert woken(w, e) == {"test", "devin"}
    clock.advance(270.0 - 3.0 - 0.5)
    w.actions += w.engine.tick()
    assert woken(w, e) - {"test", "devin"} == set()  # their wait() results stay readable
    clock.advance(0.5)
    w.actions += w.engine.tick()
    assert woken(w, e) - {"test", "devin"} == {"claude", "codex", "cursor"}


def test_with_the_hold_off_chatter_waits_only_for_the_quiet_period(tmp_path: Path, clock: FakeClock) -> None:
    """``unaddressed_hold_s = 0`` is the behavior before #137: chatter wakes every idle agent
    once the room has been quiet for ``quiet_s`` (3 s)."""
    w = World(tmp_path, clock, Config().with_delivery(unaddressed_hold_s=0.0))
    e = everyone(w)
    _pp, peer = w.agent("peer")
    w.agent_says(peer, "just thinking out loud")
    assert woken(w, e) == set()
    clock.advance(3.0)
    w.actions += w.engine.tick()
    assert woken(w, e) == {"claude", "codex", "test", "cursor", "devin"}
