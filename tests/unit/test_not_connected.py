"""Not connected (#814, DESIGN.md §49.14): the pure rule on its numbers, and the engine on a FakeClock.

Over HTTPS the broker can't check a process on someone's machine, so a session that vanished keeps the last
state it reported. An idle one that is really there holds a wait open, and a busy one reports every tool call,
so "no wait open and nothing heard" for 30 minutes (idle) or 2 hours (busy) means it is gone. Each test says
which wrong behavior would make it fail."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock
from engine_agents import listener, session, stop
from engine_world import World

from switchboard.config import Config
from switchboard.delivery import engine as engine_mod
from switchboard.delivery import rules
from switchboard.models import Notice

IDLE = rules.NOT_CONNECTED_IDLE_S
BUSY = rules.NOT_CONNECTED_BUSY_S
DAY = rules.DAY_S
NC_SWEEP_S = engine_mod.NC_SWEEP_S


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0))


# ------------------------------------------------------------------ the pure rule
def test_an_idle_member_with_no_wait_is_not_connected_after_thirty_minutes_to_the_second() -> None:
    """Just under 30 minutes is still connected, 30 minutes is not, and the answer is when it was last
    heard from. Fails if the edge moves (an off-by-one would show an agent gone that is merely quiet)."""
    assert rules.not_connected_since("idle", 1000.0, False, 1000.0 + IDLE - 1) is None
    assert rules.not_connected_since("idle", 1000.0, False, 1000.0 + IDLE) == 1000.0


def test_a_busy_member_gets_two_hours_because_a_long_build_is_one_tool_call() -> None:
    """Fails if a busy member is held to the idle number: a 40 minute build would show it as gone."""
    assert rules.not_connected_since("busy", 0.5, False, 0.5 + IDLE * 2) is None
    assert rules.not_connected_since("busy", 0.5, False, 0.5 + BUSY) == 0.5
    assert rules.not_connected_since("waiting-approval", 0.5, False, 0.5 + BUSY - 1) is None


@pytest.mark.parametrize("status", ["idle", "starting", "offline"])
def test_starting_and_offline_members_follow_the_idle_number(status: str) -> None:
    """After a broker restart everyone is offline until a hook or listener round: they get the idle
    grace, then show as gone if nothing comes. Fails if offline were held to the busy number or never."""
    assert rules.not_connected_since(status, 1.0, False, 1.0 + IDLE) == 1.0


def test_an_open_wait_means_connected_however_long_ago_it_was_heard() -> None:
    """Codex's Stop wait is open for a day: nothing is heard in that time, and it is there. Fails if
    the wait is ignored."""
    assert rules.not_connected_since("idle", 1.0, True, 1.0 + 10 * DAY) is None


def test_nothing_known_is_never_not_connected() -> None:
    """A row never heard from has no 'since'. Fails if None were read as the epoch (gone since 1970)."""
    assert rules.not_connected_since("idle", None, False, 10 * DAY) is None
    assert rules.heard_at(None, 0, None) is None
    assert rules.heard_at(3.0, 9.0, None) == 9.0


def test_the_cleanup_waits_for_the_days_and_zero_is_never() -> None:
    """Counted from when it was last heard from. Fails if 0 were taken as 'at once', or the edge moved."""
    assert not rules.not_connected_expired(100.0, 100.0 + 3 * DAY - 1, 3)
    assert rules.not_connected_expired(100.0, 100.0 + 3 * DAY, 3)
    assert not rules.not_connected_expired(100.0, 100.0 + 400 * DAY, 0)
    assert not rules.not_connected_expired(None, 100.0 + 400 * DAY, 3)


@pytest.mark.parametrize("bad", [True, "3", 3.5, -1, 366, None])
def test_an_unrecognised_setting_is_the_default_not_off(bad: object) -> None:
    """Fails closed: a value the code doesn't know must never turn the cleanup off (or to zero days)."""
    assert rules.clamp_end_days(bad) == rules.NOT_CONNECTED_END_DAYS
    assert rules.clamp_end_days(0) == 0 and rules.clamp_end_days(365) == 365


def test_a_broadcast_leaves_out_members_that_are_not_connected() -> None:
    """@here and @everyone go to who is there. Fails if a gone member's name is still added to mentions."""
    members = [("alpha", "idle"), ("bravo", "busy"), ("gone", "idle")]
    assert rules.broadcast_targets("everyone", members, ["Gone"]) == ["alpha", "bravo"]
    assert rules.broadcast_targets("here", members, ["gone"]) == ["alpha", "bravo"]
    assert rules.broadcast_targets("here", members) == ["alpha", "bravo", "gone"]


# ------------------------------------------------------------------ the engine on a FakeClock
def test_an_idle_session_with_a_wait_open_stays_connected_and_one_without_goes_after_30_minutes(
    w: World, clock: FakeClock
) -> None:
    """The listener's park is the proof: a session holding one is connected for hours, and the same
    session after its terminal is killed (the park's connection drops) is not connected 30 minutes on.
    Fails if a held wait were ignored, or if closing a wait didn't count as the last time it was there."""
    p, _m, park = listener(w, status="idle")
    clock.advance(5 * 3600)
    assert w.engine.not_connected_since(w.p(p)) is None  # held open the whole time
    w.actions += w.engine.close_conn_sinks(0)  # no conn: nothing closes
    sink = w.engine.sinks.get(park.sink_id)
    assert sink is not None and sink.open
    w.actions += [w.engine._close_sink(sink, None, "disconnect")]  # the connection drops
    dropped_at = clock.now()
    clock.advance(IDLE - 1)
    assert w.engine.not_connected_since(w.p(p)) is None
    clock.advance(1)
    assert w.engine.not_connected_since(w.p(p)) == dropped_at


def test_a_listener_round_that_ends_and_reconnects_at_once_never_shows_as_gone(
    w: World, clock: FakeClock
) -> None:
    """Claude Code's listener waits in rounds of 900 s and parks again at once. Fails if the idle clock
    ran from the first park instead of from each round's end."""
    p, _m, park = listener(w, status="idle")
    for _ in range(8):  # two hours of rounds
        clock.advance(900)
        sink = w.engine.sinks.get(park.sink_id)
        assert sink is not None
        w.actions += [w.engine._close_sink(sink, None, "timeout")]
        out = stop(w, p)
        assert out is not None and out.kind == "park"
        park = out
        assert w.engine.not_connected_since(w.p(p)) is None


def test_a_busy_session_is_gone_after_two_hours_without_a_hook_and_a_hook_brings_it_back(
    w: World, clock: FakeClock
) -> None:
    """Fails if the 2 hours were 30 minutes, or if the next hook didn't clear the state and tell the
    page (a Snapshot of its room, since Members changes with no message to carry it)."""
    p, m = session(w, status="busy")
    clock.advance(BUSY - 1)
    w.actions += w.engine.tick()
    assert w.engine.not_connected == set()
    clock.advance(1 + NC_SWEEP_S)  # the sweep looks every 30 seconds
    w.take()
    w.actions += w.engine.tick()
    assert w.engine.not_connected == {p.id}
    assert any(type(a).__name__ == "Snapshot" and a.room_id == m.room_id for a in w.take())
    w.hook(p, "PostToolUse", tool="Bash")
    assert w.engine.not_connected == set() and w.engine.not_connected_since(w.p(p)) is None
    assert any(type(a).__name__ == "Snapshot" for a in w.take())


def test_any_call_from_the_session_counts_as_being_there(w: World, clock: FakeClock) -> None:
    """A tool call over MCP (touch) clears it too, and is written at most every 15 seconds. Fails if a
    call-only session (no hooks) could never come back, or if every call wrote to the store."""
    p, _m = w.agent("bot", harness="test", status="idle")
    clock.advance(IDLE + 5)
    w.actions += w.engine.tick()
    assert p.id in w.engine.not_connected
    w.actions += w.engine.touch(w.p(p))
    assert w.engine.not_connected == set()
    before = w.p(p).last_seen
    clock.advance(5)
    w.engine.touch(w.p(p))
    assert w.p(p).last_seen == before  # inside the 15 seconds
    clock.advance(11)
    w.engine.touch(w.p(p))
    assert w.p(p).last_seen == clock.now()


def test_a_session_not_connected_for_the_days_is_ended_like_a_kick_but_may_join_again(
    w: World, clock: FakeClock
) -> None:
    """After 3 days it leaves its rooms and its claims go (a Notice says so), with a left_reason of its
    own so its 410 can say why, and it is not 'kicked' (a kicked name can't be reclaimed). Fails if it
    were kicked, kept its claims, or went a second early."""
    p, m = session(w, status="idle")
    w.store.move_claim(w.room.id, m.id, "deploy", 10 * DAY)
    assert w.store.claims_for_room(w.room.id)
    clock.advance(3 * DAY - 40)
    w.actions += w.engine.tick()
    assert w.store.get_membership(m.id).left_at is None  # type: ignore[union-attr]
    w.take()
    clock.advance(40 + 1)
    w.actions += w.engine.tick()
    ended = w.store.get_membership(m.id)
    assert ended is not None and ended.left_reason == "not_connected" and not ended.kicked
    notices = [a for a in w.take() if isinstance(a, Notice)]
    assert any("claude-1 was removed: not connected for 3 days" in n.text for n in notices)
    assert w.store.claims_for_room(w.room.id) == []  # released with the membership


def test_zero_days_never_ends_a_session_and_a_setting_changes_the_days(w: World, clock: FakeClock) -> None:
    """The admin's setting: 0 is never, 1 ends it after a day. Fails if the engine ignored the stored row."""
    p, m = session(w, status="idle")
    w.store.setting_set(rules.NOT_CONNECTED_END_KEY, "0")
    clock.advance(30 * DAY)
    w.actions += w.engine.tick()
    assert w.store.get_membership(m.id).left_at is None  # type: ignore[union-attr]
    w.store.setting_set(rules.NOT_CONNECTED_END_KEY, "1")
    clock.advance(31)
    w.actions += w.engine.tick()
    assert w.store.get_membership(m.id).left_at is not None  # type: ignore[union-attr]
    assert w.p(p).ended_at is not None


def test_a_stored_setting_that_is_garbage_is_the_default(w: World) -> None:
    """Fails open to 'never' (or crashes the tick) on a hand-edited row."""
    w.store.setting_set(rules.NOT_CONNECTED_END_KEY, "soon")
    assert w.engine.not_connected_end_days() == rules.NOT_CONNECTED_END_DAYS
    w.store.setting_set(rules.NOT_CONNECTED_END_KEY, "9999")
    assert w.engine.not_connected_end_days() == rules.NOT_CONNECTED_END_DAYS
