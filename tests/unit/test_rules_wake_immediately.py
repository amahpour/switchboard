"""Wake immediately (DESIGN.md §8.2): a message from the human,
or an @mention of an agent, wakes an idle agent at once, on every wake path, with
no quiet period (the room's 3 s quiet period, and the hold on unaddressed chatter (#137),
apply only to chatter)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock
from engine_world import World
from test_claude_adapter import claude, reg
from test_codex_adapter import attach, codex
from test_cursor_adapter import cursor, park_result, stop
from test_devin_adapter import devin
from test_devin_adapter import open_wait as devin_wait

from switchboard.config import Config
from switchboard.models import Push


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock)  # real defaults: quiet_s = 3


def everyone(w: World) -> dict[str, Any]:
    """One idle member per wake path."""
    pc, mc, _conn = claude(w, "claude-1")
    px, mx = codex(w)
    attach(w)
    pt, mt = w.agent("bot")
    st, acts = w.engine.open_wait(w.p(pt), w.m(mt), "w1", 3600)
    w.actions += acts
    pu, mu = cursor(w, status="busy")
    park = stop(w, pu)
    pd, md = devin(w)
    sd = devin_wait(w, pd, md)
    return {"claude": mc, "codex": mx, "test": st, "cursor": park, "devin": sd}


def woken(w: World, e: dict[str, Any]) -> set[str]:
    acts = w.take()
    got = {"claude" if a.path == "inbox" else "codex" for a in acts if isinstance(a, Push)}
    for name in ("test", "devin"):
        s = w.engine.sinks.get(e[name].id)
        if s is not None and not s.open and (s.result or {}).get("status") == "messages":
            got.add(name)
    res = park_result(w, e["cursor"].sink_id)
    if res and res.get("status") == "messages":
        got.add("cursor")
    return got


def test_a_human_message_wakes_every_path_at_once(w: World) -> None:
    e = everyone(w)
    w.human("hello, everyone")  # no clock advance at all
    assert woken(w, e) == {"claude", "codex", "test", "cursor", "devin"}


def test_an_at_mention_wakes_only_its_target_at_once(w: World) -> None:
    e = everyone(w)
    _pp, peer = w.agent("peer")
    w.agent_says(peer, "@codex-1 @bot @cursor-1 over to you", mentions=("codex-1", "bot", "cursor-1"))
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
    attach(w)  # the Codex link stays fresh
    reg(w, w.p(w.m(e["claude"]).participant_id), "idle")
    w.actions += w.engine.tick()
    assert woken(w, e) - {"test", "devin"} == set()  # their wait() results stay readable
    clock.advance(0.5)
    attach(w)
    reg(w, w.p(w.m(e["claude"]).participant_id), "idle")
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
    attach(w)  # the Codex link stays fresh
    reg(w, w.p(w.m(e["claude"]).participant_id), "idle")
    w.actions += w.engine.tick()
    assert woken(w, e) == {"claude", "codex", "test", "cursor", "devin"}
