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
    assert rules.heard_at(None, 0) is None
    assert rules.heard_at(3.0, 9.0) == 9.0


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


# ------------------------------------------------------------------ statuses the broker sets (#948)
def members_pane_since(w: World, p_id: int) -> float | None:
    """What the Members pane's row shows: ``Store.members``' heard_at through ``not_connected_for``, as
    ``RoomService.member_rows`` asks it (the second copy of the rule, in SQL)."""
    (row,) = [r for r in w.store.members(w.room.id) if r.participant_id == p_id]
    return w.engine.not_connected_for(p_id, row.status, row.heard_at)


def test_a_restart_is_not_hearing_from_a_member_last_seen_hours_before_it(w: World, clock: FakeClock) -> None:
    """The broker's restart sets every session offline with ``status_at`` now (§4). That is the broker,
    not the session: a member last heard from hours before is still not connected right after the
    restart, since the time it was last heard. Fails if status_at counts as a sign of life: after every
    restart (an upgrade) the whole Not connected fold emptied for 30 minutes (#948)."""
    p, _m = session(w, status="idle")
    seen = clock.now()
    clock.advance(5 * 3600)
    w.store.recover_on_start()
    assert w.p(p).status == "offline" and w.p(p).status_src == "restart:idle"  # the restart as before
    assert w.p(p).status_at == clock.now()
    assert w.engine.not_connected_since(w.p(p)) == seen
    assert members_pane_since(w, p.id) == seen


def test_a_member_heard_a_minute_before_a_restart_is_connected_after_it_until_its_own_30_minutes(
    w: World, clock: FakeClock
) -> None:
    """The other edge: a member heard from a minute before the restart keeps the rest of its idle 30
    minutes, counted from when it was heard and not from the restart. Fails if the fix made everyone
    not connected at the restart, or if the restart still moved the clock."""
    p, _m = session(w, status="busy")
    clock.advance(3600)
    w.hook(p, "PostToolUse", tool="Bash")  # heard: a tool call of its turn
    seen = clock.now()
    clock.advance(60)
    w.store.recover_on_start()
    assert w.p(p).status_src == "restart"
    assert w.engine.not_connected_since(w.p(p)) is None
    assert members_pane_since(w, p.id) is None
    clock.advance(IDLE - 60 - 1)
    assert w.engine.not_connected_since(w.p(p)) is None
    clock.advance(1)
    assert w.engine.not_connected_since(w.p(p)) == seen
    assert members_pane_since(w, p.id) == seen


def test_a_dropped_wait_taking_a_session_offline_is_not_hearing_from_it(w: World, clock: FakeClock) -> None:
    """A Devin whose wait() lost its connection is taken offline by the tick (``wait:dropped``, #448)
    130 seconds later. The drop is the last time it was there; the broker's offline is not. Fails if
    that status moved the clock: it would show as gone 130 seconds late, from a time it wasn't heard."""
    p, m = w.agent("dv-https", harness="devin", status="busy", hooks=True, host="laptop")
    w.actions += w.engine.open_wait(w.p(p), w.m(m), "w1", 600)[1]
    clock.advance(600)
    w.actions += w.engine.unwait(w.p(p), "w1")
    w.actions += w.engine.wait_dropped(w.p(p))
    dropped_at = clock.now()
    clock.advance(rules.WAIT_DROP_GRACE_S)
    w.actions += w.engine.tick()
    assert w.p(p).status == "offline" and w.p(p).status_src == "wait:dropped"
    assert w.p(p).status_at == clock.now()
    clock.advance(IDLE - rules.WAIT_DROP_GRACE_S)
    assert w.engine.not_connected_since(w.p(p)) == dropped_at
    assert members_pane_since(w, p.id) == dropped_at


@pytest.mark.parametrize("src", ["expired:no_hook", "rearm:no_hook", "stop:followup", "sink"])
def test_no_status_the_broker_sets_on_its_own_moves_when_a_member_was_heard(
    w: World, clock: FakeClock, src: str
) -> None:
    """The other statuses the engine sets with no hook or call of the session at that moment: a timer
    undoing a follow-up's busy (``expired:*``, ``rearm:no_hook``), a follow-up offered into a park
    (``stop:followup``), a wait's status for a harness whose status comes from its sinks (``sink``).
    None of them is the session being there. Fails if any of them moved heard_at."""
    p, _m = session(w, status="idle")
    seen = clock.now()
    clock.advance(IDLE - 10)
    w.store.set_status(p.id, "busy" if src in ("stop:followup", "sink") else "idle", src, bump_boundary=True)
    assert w.p(p).status_at == clock.now() and w.p(p).status_src == src
    clock.advance(10)
    assert rules.heard_at(w.p(p).last_seen, w.p(p).last_say_at) == seen
    expected = None if src in ("stop:followup", "sink") else seen  # busy gets its 2 hours
    assert w.engine.not_connected_since(w.p(p)) == expected
    assert members_pane_since(w, p.id) == expected
    clock.advance(BUSY)
    assert w.engine.not_connected_since(w.p(p)) == seen
    assert members_pane_since(w, p.id) == seen


def test_a_subagent_tool_that_ends_an_approval_hold_is_hearing_from_the_session(
    w: World, clock: FakeClock
) -> None:
    """A tool that finishes in a subagent ends the session's approval hold (``hook:subagent``, #472). That
    is the session's own hook, so it is the session heard, and the member leaves the Not connected fold
    at once. A subagent's hook writes no ``last_seen`` of its own, so this fails if ending the hold does
    not touch the session: once ``status_at`` stopped counting (#948), a prompt answered after the 2 hours
    left the member not connected while its subagent ran, and the cleanup counted days from the prompt."""
    p, m = w.agent("bot", harness="claude", status="busy", hooks=True)
    w.hook(p, "Notification", notification="permission_prompt")
    assert w.p(p).status == "waiting-approval"
    asked = clock.now()
    clock.advance(BUSY + NC_SWEEP_S)
    w.actions += w.engine.tick()
    assert w.engine.not_connected == {p.id} and w.engine.not_connected_since(w.p(p)) == asked
    w.take()
    w.actions += w.engine.subagent_tool_ran(w.p(p))
    assert w.p(p).status == "busy" and w.p(p).status_src == "hook:subagent"
    assert w.p(p).last_seen == clock.now()
    assert w.engine.not_connected == set() and w.engine.not_connected_since(w.p(p)) is None
    assert members_pane_since(w, p.id) is None
    assert any(type(a).__name__ == "Snapshot" and a.room_id == m.room_id for a in w.take())
