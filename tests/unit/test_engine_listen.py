"""SPIKE (#694): listen mode in the delivery engine, and a replayed call.

Off, a call line that names nobody never starts a turn (§52). For a member a person set to listen it is
chatter that becomes due by the quiet period or the max hold, like a reply, and everything since the
member's last turn comes in one batch. Every other member keeps today's rule.
"""

from __future__ import annotations

from pathlib import Path

from conftest import FakeClock
from engine_agents import result, waiting, woke
from engine_world import World
from listen_replay import replay
from test_rules_release import NOW, item, room

from switchboard.config import Config
from switchboard.delivery import rules


def world(tmp_path: Path, clock: FakeClock, quiet_s: float = 5.0, max_hold_s: float = 30.0) -> World:
    return World(
        tmp_path, clock, Config(human_name="alice").with_delivery(quiet_s=quiet_s, max_hold_s=max_hold_s)
    )


def line(w: World, text: str, mentions: tuple[str, ...] = ()) -> None:
    msg = w.store.insert_message(
        w.room.id, sender_name="bob", sender_kind="meeting", via="meeting", text=text, mentions=mentions
    )
    w.actions += w.engine.on_message(msg.id)


def test_chatter_due_counts_an_aside_only_for_a_listener() -> None:
    """Fails if an aside is due for everyone (off must stay as §52 says), or never due for a listener."""
    aside = item(1, 0, ts=NOW - 100, kind="meeting")
    r = room(last_msg_at=NOW - 100)
    assert not rules.chatter_due(r, [aside], NOW, 3.0, 60.0)
    assert rules.chatter_due(r, [aside], NOW, 3.0, 60.0, listening=True)


def test_a_listener_waits_for_the_quiet_period_then_for_the_max_hold() -> None:
    """The room is not quiet and the line is fresh: not due. Quiet: due. Never quiet, but the line has waited
    the max hold: due. Fails if listening skips either rule and wakes on the first line."""
    aside = item(1, 0, ts=NOW - 5, kind="meeting")
    busy = room(last_msg_at=NOW - 1)
    assert not rules.chatter_due(busy, [aside], NOW, 3.0, 60.0, listening=True)
    assert rules.chatter_due(room(last_msg_at=NOW - 4), [aside], NOW, 3.0, 60.0, listening=True)
    old = item(1, 0, ts=NOW - 61, kind="meeting")
    assert rules.chatter_due(busy, [old], NOW, 3.0, 60.0, listening=True)


def test_listening_does_not_make_agent_chatter_due_sooner() -> None:
    """Only call lines change. An agent's line still waits for the hold (hold_s), listener or not."""
    chat = item(1, 0, ts=NOW - 5)
    r = room(last_msg_at=NOW - 100)
    assert not rules.chatter_due(r, [chat], NOW, 3.0, 60.0, hold_s=270.0, listening=True)


def test_a_listener_wakes_at_a_pause_and_gets_the_lines_in_one_batch(
    tmp_path: Path, clock: FakeClock
) -> None:
    """Three lines, then quiet: one wake carrying all three. The other agent in the room stays asleep.
    Fails if the listener gets a wake per line, misses a line, or the other agent wakes."""
    w = world(tmp_path, clock)
    _p, m, listener = waiting(w, "claude-1")
    _q, _qm, other = waiting(w, "codex-1")
    w.engine.listening.add(m.id)
    for i in range(3):
        line(w, f"status item {i}")
        clock.advance(2)
        w.actions += w.engine.tick()
        assert not woke(w, listener)
    clock.advance(5)
    w.actions += w.engine.tick()
    assert woke(w, listener) and not woke(w, other)
    text = result(w, listener)["text"]
    assert all(f"status item {i}" in text for i in range(3))


def test_a_listener_wakes_after_the_max_hold_while_people_keep_talking(
    tmp_path: Path, clock: FakeClock
) -> None:
    """A line every 2 s never leaves a 5 s pause, so the max hold (30 s) is what wakes it. Fails if
    the hold is ignored and the agent hears nothing until the call is over."""
    w = world(tmp_path, clock)
    _p, m, listener = waiting(w, "claude-1")
    w.engine.listening.add(m.id)
    for _ in range(14):
        line(w, "still talking")
        clock.advance(2)
        w.actions += w.engine.tick()
        assert not woke(w, listener)
    for _ in range(3):
        line(w, "and still talking")
        clock.advance(2)
        w.actions += w.engine.tick()
    assert woke(w, listener)


def test_turning_it_off_restores_the_rule(tmp_path: Path, clock: FakeClock) -> None:
    """Fails if the set is not consulted at evaluation time."""
    w = world(tmp_path, clock)
    _p, m, listener = waiting(w, "claude-1")
    w.engine.listening.add(m.id)
    w.engine.listening.discard(m.id)
    line(w, "status item")
    clock.advance(500)
    w.actions += w.engine.tick()
    assert not woke(w, listener)


def test_the_wake_spends_the_room_budget(tmp_path: Path, clock: FakeClock) -> None:
    """A listening wake is a counted wake, so the hourly budget is the cap on turns a call can cause."""
    w = world(tmp_path, clock)
    _p, m, listener = waiting(w, "claude-1")
    w.engine.listening.add(m.id)
    line(w, "status item")
    clock.advance(10)
    w.actions += w.engine.tick()
    assert woke(w, listener)
    r = w.store.room_by_id(w.room.id)
    assert r is not None and r.budget_remaining == r.budget_per_hour - 1


# ------------------------------------------------------------- the replayed call
def test_replay_off_is_silent(tmp_path: Path) -> None:
    """Today's rule over 22 minutes of talk that names nobody: no wake at all."""
    assert replay(3.0, 60.0, listening=False, tmp=tmp_path).n_wakes == 0


def test_replay_listening_hears_every_line_with_sane_settings(tmp_path: Path) -> None:
    """quiet 20 s, hold 45 s: every line reaches the agent, within the hold plus a tick and the 10 s turn,
    the briefed line included, and the hourly budget is not spent. Fails if lines are dropped, or waits
    pass the hold."""
    r = replay(20.0, 45.0, tmp=tmp_path)
    assert r.line_delays() and len(r.line_delays()) == 123
    assert max(r.line_delays()) <= 45.0 + 10.0 + 2.0
    assert r.delay_for("key") is not None and r.min_budget > 0


def test_replay_a_short_quiet_period_spends_the_budget(tmp_path: Path) -> None:
    """quiet 3 s wakes after nearly every line and runs the budget of 60 out within the call, so later
    lines (the date question) are never heard. This is why the default quiet period is wrong for a call."""
    r = replay(3.0, 60.0, tmp=tmp_path)
    assert r.min_budget == 0 and r.delay_for("date") is None
