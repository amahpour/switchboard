"""Quiet: wake only for what is addressed (#548, DESIGN.md §50).

Off, delivery is today's. On, unaddressed chatter (an agent's message, prio 0, not a
reply to this member) never starts a turn: not after ``unaddressed_hold_s``, not by
answering a Stop, and not by ending an open ``wait()``. A person's message, an
@mention and a reply still wake it, and that chatter comes along. It still shows at
``read()``. A Stop with only that chatter leaves the park open; Devin's Stop does not
block with it. Mid-task stays priority only. ``/wake`` still forces it out, because
the person asked.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest
from conftest import FakeClock
from engine_agents import listener, result, session, waiting, woke
from engine_world import World
from test_devin_adapter import devin
from test_rules_release import NOW, item, room

from switchboard.config import Config
from switchboard.delivery import rules

HOLD = 10.0


def ids_of(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"^- id=(\d+) ", text, re.M)]


def release(
    pending,
    *,
    addressed_only: bool = False,
    hold_s: float = 270.0,
    wake_now: bool = False,
    eff: str = "idle",
):
    return rules.releasable(
        pending,
        room=room(),
        eff=eff,
        peer_batch_boundary=-1,
        boundary_seq=0,
        now=NOW,
        quiet_s=3.0,
        max_hold_s=60.0,
        batch_max_msgs=20,
        max_chars=6000,
        hold_s=hold_s,
        wake_now=wake_now,
        addressed_only=addressed_only,
    )


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(
        tmp_path,
        clock,
        Config().with_delivery(quiet_s=3.0, max_hold_s=60.0, unaddressed_hold_s=HOLD),
    )


def _quiet(w: World, membership_id: int, on: bool) -> None:
    if on:
        w.store.set_quiet(membership_id, True)


# ---------------------------------------------------------------- pure policy
def test_unaddressed_chatter_past_the_hold_is_due_only_when_not_quiet() -> None:
    """Fails if ``addressed_only`` is ignored: chatter that has waited the hold would
    still start a turn for a quiet member, and an open ``wait()`` (hold_s 0) would too."""
    chat = item(1, 0, ts=NOW - 400)
    assert release([chat], addressed_only=False) is not None
    assert release([chat], addressed_only=True) is None
    assert release([chat], addressed_only=True, hold_s=0.0) is None
    assert release([chat], addressed_only=False, hold_s=0.0) is not None


def test_a_reply_is_due_either_way_and_brings_the_chatter() -> None:
    """A reply to the member uses the quiet period with the switch off or on, and the
    unaddressed chatter rides that wake. Fails if quiet drops the reply, or drops the
    chatter that should come along with it."""
    chat = item(1, 0, ts=NOW - 400)
    reply = dataclasses.replace(item(2, 0, ts=NOW - 10), reply_to_me=True)
    off = release([chat, reply], addressed_only=False)
    on = release([chat, reply], addressed_only=True)
    assert off is not None and on is not None
    assert [i.message_id for i in off.items] == [i.message_id for i in on.items]
    assert {i.message_id for i in on.items} == {1, 2}


def test_a_person_and_a_mention_wake_either_way_and_bring_the_chatter() -> None:
    chat = item(1, 0, ts=NOW - 5)
    person = item(2, 2, ts=NOW)
    mention = item(3, 1, ts=NOW)
    for flag in (False, True):
        got = release([chat, person], addressed_only=flag)
        assert got is not None and {i.message_id for i in got.items} == {1, 2}
        got = release([chat, mention], addressed_only=flag)
        assert got is not None and {i.message_id for i in got.items} == {1, 3}


def test_wake_now_still_releases_chatter_when_quiet() -> None:
    """``/wake`` is a person asking. Fails if quiet swallows it, or if chatter that has
    not been asked for starts the turn anyway."""
    chat = item(1, 0, ts=NOW - 400)
    assert release([chat], addressed_only=True, wake_now=False) is None
    forced = release([chat], addressed_only=True, wake_now=True)
    assert forced is not None and [i.message_id for i in forced.items] == [1]


def test_busy_stays_priority_only_when_quiet() -> None:
    """Mid-task does not gain unaddressed chatter. Fails if quiet (or its absence)
    puts chatter on the busy path."""
    chat = item(1, 0, ts=NOW - 400)
    person = item(2, 2)
    for flag in (False, True):
        assert release([chat], addressed_only=flag, eff="busy") is None
        got = release([chat, person], addressed_only=flag, eff="busy")
        assert got is not None and [i.message_id for i in got.items] == [2]


def test_quiet_chatter_is_not_what_the_budget_is_holding() -> None:
    """A notice that the budget is exhausted is for a wake the budget stopped. Quiet
    stopped this one. A mention, and a reply, still count. Fails if the flag is ignored."""
    chat = item(1, 0)
    kw = dict(room=room(budget_remaining=0), eff="idle", peer_batch_boundary=-1, boundary_seq=0)
    assert rules.budget_blocked([chat], addressed_only=False, **kw)
    assert not rules.budget_blocked([chat], addressed_only=True, **kw)
    assert rules.budget_blocked([item(2, 1)], addressed_only=True, **kw)
    reply = dataclasses.replace(chat, reply_to_me=True)
    assert rules.budget_blocked([reply], addressed_only=True, **kw)


def test_a_quiet_member_has_no_wakes_at() -> None:
    chat = item(1, 0, ts=NOW - 50)
    assert rules.chatter_wakes_at([chat], HOLD) == pytest.approx(NOW - 50 + HOLD)
    assert rules.chatter_wakes_at([chat], HOLD, addressed_only=True) is None


# ---------------------------------------------------------------- the engine
@pytest.mark.parametrize("harness", ["claude", "codex", "cursor", "opencode"])
@pytest.mark.parametrize("quiet", [False, True])
def test_a_stop_answers_with_chatter_only_when_the_member_is_not_quiet(
    w: World, clock: FakeClock, harness: str, quiet: bool
) -> None:
    """The hold elapses. Off, the parked Stop is answered. On, it stays open: chatter
    does not start the turn. Fails if quiet is ignored (the park would fill) or if the
    switch off stops waking."""
    _p, m, park = listener(w, f"{harness}-1", harness)
    _quiet(w, m.id, quiet)
    _pp, peer = w.agent("peer")
    note = w.agent_says(peer, "peers comparing notes")
    clock.advance(HOLD - 1)
    w.actions += w.engine.tick()
    assert not woke(w, park)
    clock.advance(1)
    w.actions += w.engine.tick()
    if quiet:
        clock.advance(500)
        w.actions += w.engine.tick()
        assert not woke(w, park)
        assert w.delivery(m, note)["state"] == "pending"
        assert w.engine.wakes_at(m.id) is None
    else:
        assert woke(w, park)
        assert note.id in ids_of(result(w, park)["text"])


@pytest.mark.parametrize("quiet", [False, True])
def test_devin_stop_blocks_with_chatter_only_when_not_quiet(w: World, clock: FakeClock, quiet: bool) -> None:
    """Devin has no park. Off, the Stop block carries the chatter. On, it does not:
    the existing nothing-to-deliver path re-arms with ``call wait()`` and the note
    stays pending. Fails if a quiet Devin is blocked with the chatter."""
    p, m = devin(w)
    _quiet(w, m.id, quiet)
    w.hook(p, "UserPromptSubmit", gen="g1")
    _pp, peer = w.agent("peer")
    note = w.agent_says(peer, "unaddressed note zebra-quiet")
    clock.advance(HOLD)
    out = w.hook(p, "Stop", gen="g1")
    assert out is not None and out.kind == "continue"
    if quiet:
        assert out.batch_id is None
        assert "zebra-quiet" not in out.text
        assert "call wait" in out.text
        assert w.delivery(m, note)["state"] == "pending"
    else:
        assert out.batch_id is not None
        assert note.id in ids_of(out.text)


@pytest.mark.parametrize("harness", ["test", "claude"])
@pytest.mark.parametrize("quiet", [False, True])
def test_an_open_wait_ends_for_chatter_only_when_not_quiet(
    w: World, clock: FakeClock, harness: str, quiet: bool
) -> None:
    """``wait()`` asks for messages, so the hold is 0 and chatter is due after the quiet
    period. Quiet keeps the call open. Fails if a quiet member's wait returns the chatter."""
    name = "bot" if harness == "test" else "claude-1"
    _p, m, sink = waiting(w, name, harness)
    _quiet(w, m.id, quiet)
    _pp, peer = w.agent("peer")
    note = w.agent_says(peer, "while you were waiting")
    clock.advance(3)
    w.actions += w.engine.tick()
    if quiet:
        clock.advance(HOLD)
        w.actions += w.engine.tick()
        assert not woke(w, sink)
        assert w.delivery(m, note)["state"] == "pending"
    else:
        assert woke(w, sink)
        assert note.id in ids_of(result(w, sink)["text"])


@pytest.mark.parametrize("quiet", [False, True])
def test_a_persons_message_wakes_and_brings_the_chatter(w: World, quiet: bool) -> None:
    _p, m, park = listener(w)
    _quiet(w, m.id, quiet)
    _pp, peer = w.agent("peer")
    note = w.agent_says(peer, "an aside")
    said = w.human("please look at the parser")
    assert woke(w, park)
    got = ids_of(result(w, park)["text"])
    assert said.id in got and note.id in got


@pytest.mark.parametrize("quiet", [False, True])
def test_a_mention_wakes_and_brings_the_chatter(w: World, quiet: bool) -> None:
    _p, m, park = listener(w)
    _quiet(w, m.id, quiet)
    _pp, peer = w.agent("peer")
    note = w.agent_says(peer, "an aside")
    ment = w.agent_says(peer, "@claude-1 a question", mentions=("claude-1",))
    assert woke(w, park)
    got = ids_of(result(w, park)["text"])
    assert ment.id in got and note.id in got


@pytest.mark.parametrize("quiet", [False, True])
def test_a_reply_wakes_after_the_quiet_period_and_brings_the_chatter(
    w: World, clock: FakeClock, quiet: bool
) -> None:
    _p, m, park = listener(w)
    _quiet(w, m.id, quiet)
    _pp, peer = w.agent("peer")
    note = w.agent_says(peer, "an aside")
    mine = w.agent_says(m, "I think the cache key is wrong")
    assert not woke(w, park)
    reply = w.agent_says(peer, "it drops the host", reply_to=mine.id)
    clock.advance(2)
    w.actions += w.engine.tick()
    assert not woke(w, park)
    clock.advance(1)
    w.actions += w.engine.tick()
    assert woke(w, park)
    got = ids_of(result(w, park)["text"])
    assert reply.id in got and note.id in got


def test_read_shows_chatter_a_quiet_member_was_not_woken_for(w: World, clock: FakeClock) -> None:
    """The note stays pending through the hold and a long time after, and ``read()``
    shows it in full. Fails if the hold woke it anyway (nothing left to read)."""
    p, m, park = listener(w)
    w.store.set_quiet(m.id, True)
    _pp, peer = w.agent("peer")
    note = w.agent_says(peer, "the aside about the parser")
    clock.advance(HOLD + 50)
    w.actions += w.engine.tick()
    assert not woke(w, park)
    text, bid, n, _more, acts = w.engine.pull(w.p(p), w.m(m), "read", 50)
    w.actions += acts
    assert n >= 1 and bid is not None
    assert f"id={note.id} " in text and "the aside about the parser" in text


@pytest.mark.parametrize("quiet", [False, True])
def test_a_tool_call_does_not_deliver_chatter(w: World, clock: FakeClock, quiet: bool) -> None:
    """PostToolUse stays limited to a person's message and an @mention. Fails if quiet
    starts delivering unaddressed chatter mid-task."""
    p, m = session(w, "claude-1", "claude", status="busy")
    _quiet(w, m.id, quiet)
    _pp, peer = w.agent("peer")
    chat = w.agent_says(peer, "chatter only")
    ment = w.agent_says(peer, "@claude-1 look", mentions=("claude-1",))
    said = w.human("from alice")
    clock.advance(HOLD)
    out = w.hook(p, "PostToolUse", ok=True)
    assert out is not None and out.kind == "context"
    got = ids_of(out.text)
    assert said.id in got and ment.id in got and chat.id not in got
    assert w.delivery(m, chat)["state"] == "pending"


def test_unquiet_delivers_chatter_the_hold_has_already_earned(w: World, clock: FakeClock) -> None:
    """Turning it off re-evaluates, so chatter already past the hold goes now, without
    waiting for the next tick. Fails if ``/unquiet`` only stores the flag."""
    _p, m, park = listener(w)
    w.store.set_quiet(m.id, True)
    _pp, peer = w.agent("peer")
    note = w.agent_says(peer, "waiting out the hold")
    clock.advance(HOLD)
    w.actions += w.engine.tick()
    assert not woke(w, park)
    w.store.set_quiet(m.id, False)
    w.actions += w.engine.on_command(w.room.id, "unquiet", m.id)
    assert woke(w, park)
    assert note.id in ids_of(result(w, park)["text"])


def test_wake_still_forces_a_quiet_members_chatter(w: World) -> None:
    _p, m, park = listener(w)
    w.store.set_quiet(m.id, True)
    _pp, peer = w.agent("peer")
    note = w.agent_says(peer, "not addressed")
    assert not woke(w, park)
    w.actions += w.engine.on_command(w.room.id, "wake", m.id)
    assert woke(w, park)
    assert note.id in ids_of(result(w, park)["text"])
