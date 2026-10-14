"""The hold on unaddressed chatter (issue #137, DESIGN.md §8.2): agents' chatter that addresses
nobody waits ``unaddressed_hold_s`` before it wakes an idle agent, collecting into one batch, so
four idle agents don't each take a turn for every short exchange between two others. A person's
message, an @mention and a reply to the agent's own message still wake it at once (or after the
quiet period), and ``/wake`` lets a person skip the hold for one agent."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from conftest import FakeClock
from engine_world import World
from test_claude_adapter import claude, pushes, reg
from test_rules_release import NOW, item, rel, room

from switchboard.broker.hub import Hub
from switchboard.broker.service import BrokerInfo, RoomService
from switchboard.config import Config
from switchboard.delivery import rules

HOLD = 270.0


def held(*items, now=NOW, hold=HOLD, **kw):
    return rules.releasable(
        list(items),
        room=kw.pop("r", None) or room(last_msg_at=now - 100),
        eff=kw.pop("eff", "idle"),
        peer_batch_boundary=kw.pop("pbb", -1),
        boundary_seq=kw.pop("seq", 0),
        now=now,
        quiet_s=3.0,
        max_hold_s=60.0,
        batch_max_msgs=20,
        max_chars=6000,
        hold_s=hold,
        **kw,
    )


# ------------------------------------------------------------------ the rule
def test_unaddressed_chatter_waits_for_the_hold_not_the_quiet_period() -> None:
    """Quiet for 100 s and past the 60 s max hold, which would release it before #137: it still
    waits until its oldest item has waited 270 s. Fails if the hold doesn't replace the quiet
    period and the max hold for chatter that addresses nobody."""
    c = item(1, 0, ts=NOW - (HOLD - 1))
    assert held(c) is None
    assert [i.message_id for i in held(c, now=NOW + 1).items] == [1]


def test_with_the_hold_off_the_quiet_period_decides_as_before() -> None:
    c = item(1, 0, ts=NOW - 10)
    assert held(c, hold=0.0) is not None
    assert rel([c]) is not None  # rel() is the release without hold_s: the old rule


def test_a_held_batch_carries_everything_that_arrived_meanwhile() -> None:
    """One wake for the whole exchange: the oldest item's hold releases the newer ones too."""
    old, mid, new = item(1, 0, ts=NOW - HOLD), item(2, 0, ts=NOW - 100), item(3, 0, ts=NOW - 1)
    assert [i.message_id for i in held(old, mid, new).items] == [1, 2, 3]
    assert held(mid, new) is None


def test_a_reply_to_the_members_own_message_goes_by_the_quiet_period() -> None:
    """A reply addresses the member it answers, like an @mention: it doesn't wait on the hold,
    and it takes the held chatter along. Fails if ``reply_to_me`` is ignored."""
    reply = dataclasses.replace(item(1, 0, ts=NOW - 10), reply_to_me=True)
    chatter = item(2, 0, ts=NOW - 20)
    got = held(reply, chatter)
    assert got is not None and [i.message_id for i in got.items] == [1, 2]
    assert held(chatter) is None


def test_a_capped_batch_keeps_the_reply_that_released_it() -> None:
    """Found by the simulation (seed 4): with a batch cap of 2, a reply released the held chatter
    and the cap kept the two oldest chatter items instead, so the reply waited and the others
    went out 34 s into a 270 s hold. The reply must be in the batch it releases."""
    old = [item(i, 0, ts=NOW - 30) for i in (1, 2, 3)]
    reply = dataclasses.replace(item(4, 0, ts=NOW - 10), reply_to_me=True)
    got = rules.releasable(
        [*old, reply],
        room=room(last_msg_at=NOW - 10),
        eff="idle",
        peer_batch_boundary=-1,
        boundary_seq=0,
        now=NOW,
        quiet_s=3.0,
        max_hold_s=60.0,
        batch_max_msgs=2,
        max_chars=6000,
        hold_s=HOLD,
    )
    assert got is not None and [i.message_id for i in got.items] == [4, 1]


def test_a_mention_or_a_person_still_wakes_at_once_and_takes_the_held_chatter() -> None:
    chatter = item(1, 0, ts=NOW - 5)
    for prio in (1, 2):
        got = held(chatter, item(2, prio, ts=NOW))
        assert got is not None and {i.message_id for i in got.items} == {1, 2}


def test_wake_now_skips_the_hold_the_budget_and_the_peer_batch_rule() -> None:
    """/wake is a person's own ask: like their message it needs no budget, and the one peer batch
    per turn boundary doesn't hold it back. A busy member still gets no chatter mid-task."""
    c = item(1, 0, ts=NOW - 1)
    spent = room(budget_remaining=0, last_msg_at=NOW - 1)
    assert held(c, r=spent, pbb=3, seq=3) is None
    got = held(c, r=spent, pbb=3, seq=3, wake_now=True)
    assert got is not None and got.kind == "wake" and [i.message_id for i in got.items] == [1]
    assert held(c, eff="busy", wake_now=True) is None


def test_wakes_at_is_the_oldest_unaddressed_item_plus_the_hold() -> None:
    a, b = item(1, 0, ts=NOW - 50), item(2, 0, ts=NOW - 10)
    assert rules.chatter_wakes_at([b, a], HOLD) == NOW - 50 + HOLD
    assert rules.chatter_wakes_at([a], 0.0) is None
    assert rules.chatter_wakes_at([], HOLD) is None
    assert rules.chatter_wakes_at([a, item(3, 1)], HOLD) is None  # a mention wakes it at once
    assert rules.chatter_wakes_at([a, dataclasses.replace(b, reply_to_me=True)], HOLD) is None
    assert rules.chatter_wakes_at([item(4, 0, notified=NOW - 5)], HOLD) is None  # pull-only


# ---------------------------------------------------------------- the store
@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock)  # the real defaults: the hold is 270 s


def test_the_store_marks_a_reply_to_the_recipients_own_message(w: World) -> None:
    """Fails if ``reply_to_me`` is computed for the wrong member (the sender of the reply, or
    everyone in the room)."""
    _pa, a = w.agent("claude-1")
    _pb, b = w.agent("codex-1")
    _pc, c = w.agent("devin-1")
    first = w.agent_says(a, "I think it's the cache")
    reply = w.agent_says(b, "it isn't", reply_to=first.id)
    a_items = {i.message_id: i.reply_to_me for i in w.store.pending_items(a.id)}
    c_items = {i.message_id: i.reply_to_me for i in w.store.pending_items(c.id)}
    assert a_items == {reply.id: True}
    assert c_items == {first.id: False, reply.id: False}


# ---------------------------------------------------------------- the engine
def test_an_idle_agent_gets_the_whole_exchange_in_one_wake_at_the_hold(w: World, clock: FakeClock) -> None:
    """Two other agents talk for a minute without naming claude-1: it is woken once, 270 s after
    the first message, with all three. Fails if it wakes per message (the bug #137 is about)."""
    p, m, _conn = claude(w)
    _pa, a = w.agent("codex-1")
    _pb, b = w.agent("devin-1")
    first = w.agent_says(a, "the cache key is wrong")
    clock.advance(30)
    second = w.agent_says(b, "agreed, it drops the host")
    clock.advance(30)
    third = w.agent_says(a, "fixing it")
    w.actions += w.engine.tick()
    assert pushes(w) == []
    assert w.engine.wakes_at(m.id) == pytest.approx(first.ts + 270)
    clock.advance(270 - 60 - 1)
    reg(w, p, "idle")
    w.actions += w.engine.tick()
    assert pushes(w) == []
    clock.advance(1)
    reg(w, p, "idle")
    w.actions += w.engine.tick()
    [push] = pushes(w)
    assert all(f"id={x.id} " in push.text for x in (first, second, third))
    assert w.engine.wakes_at(m.id) is None


def test_an_agent_in_its_own_wait_shows_no_wake_time(w: World) -> None:
    """wait() gets chatter after the quiet period (the hold would only time it out), so the
    member list must not promise a later wake for it."""
    p, m = w.agent("bot")
    _pa, a = w.agent("codex-1")
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    w.actions += acts
    w.agent_says(a, "thinking out loud")
    assert w.engine.wakes_at(m.id) is None


def test_wake_now_wakes_an_idle_agent_at_once_and_only_for_what_was_waiting(
    w: World, clock: FakeClock
) -> None:
    """/wake sends the waiting chatter now. Chatter that arrives after it waits on the hold
    again: fails if the ask outlives the messages it was for (it did, in the first draft)."""
    p, m, _conn = claude(w)
    _pa, a = w.agent("codex-1")
    msg = w.agent_says(a, "just chatter")
    assert pushes(w) == []
    w.actions += w.engine.on_command(w.room.id, "wake", m.id)
    [push] = pushes(w)
    assert f"id={msg.id} " in push.text
    w.store.mark_posted(push.batch_id)
    w.actions += w.engine.on_confirm(push.batch_id, "test")
    clock.advance(10)
    w.actions += w.engine.set_status(w.p(p), "idle", "hook:Stop", bump=True)  # the woken turn ends
    reg(w, p, "idle")
    later = w.agent_says(a, "more chatter")
    w.actions += w.engine.tick()
    assert pushes(w) == []
    assert m.id not in w.engine.wake_now
    assert w.engine.wakes_at(m.id) == pytest.approx(later.ts + 270)


def test_wake_now_on_a_busy_agent_goes_out_when_it_is_idle_not_at_the_hold(
    w: World, clock: FakeClock
) -> None:
    """A person asked for it now: a busy agent can't take chatter mid-task, so it gets the
    messages the moment it is idle again, not 270 s after they arrived. Fails if the ask is
    dropped while the agent is busy."""
    p, m, _conn = claude(w, status="busy", registry="busy")
    _pa, a = w.agent("codex-1")
    msg = w.agent_says(a, "just chatter")
    w.actions += w.engine.on_command(w.room.id, "wake", m.id)
    assert pushes(w) == []
    clock.advance(10)
    w.hook(p, "Stop")
    reg(w, p, "idle")
    w.actions += w.engine.evaluate_participant(p.id)
    [push] = pushes(w)
    assert f"id={msg.id} " in push.text


def test_a_parked_agent_shows_no_wake_time(w: World) -> None:
    """Found in the recording: after Wake now on an agent with no wake path, Members still said
    "wakes at", a wake that can't happen. Once it is parked, the time goes (the Inspector says
    to poke it)."""
    p, m, _conn = claude(w, attached=False)
    _pa, a = w.agent("codex-1")
    msg = w.agent_says(a, "just chatter")
    assert w.engine.wakes_at(m.id) == pytest.approx(msg.ts + 270)
    w.actions += w.engine.on_command(w.room.id, "wake", m.id)
    assert w.engine.parked_reason(m.id)
    assert w.engine.wakes_at(m.id) is None


def test_leaving_drops_a_pending_wake_now(w: World) -> None:
    p, m, _conn = claude(w, status="busy", registry="busy")
    _pa, a = w.agent("codex-1")
    w.agent_says(a, "just chatter")
    w.actions += w.engine.on_command(w.room.id, "wake", m.id)
    assert m.id in w.engine.wake_now
    w.actions += w.engine.on_membership_ended(m.id, "leave")
    assert m.id not in w.engine.wake_now


# ------------------------------------------------------------ /wake, the command
class Shim:
    def __init__(self, w: World) -> None:
        self.w = w

    def on_message(self, msg) -> None:
        self.w.actions += self.w.engine.on_message(msg.id)

    def on_command(self, room, name: str, membership_id: int | None) -> None:
        self.w.actions += self.w.engine.on_command(room.id, name, membership_id)

    def on_membership_ended(self, membership_id: int, reason: str) -> None:
        self.w.actions += self.w.engine.on_membership_ended(membership_id, reason)

    def parked_reason(self, membership_id: int) -> str | None:
        return self.w.engine.parked_reason(membership_id)

    def wakes_at(self, membership_id: int) -> float | None:
        return self.w.engine.wakes_at(membership_id)

    def board_reason(self, member) -> None:
        return None

    def member_claims(self, member) -> tuple[None, None]:
        return None, None


@pytest.fixture
def svc(w: World, clock: FakeClock) -> RoomService:
    s = RoomService(w.store, Hub(), w.cfg, BrokerInfo(port=7419, test_mode=True), clock)
    s.delivery = Shim(w)
    return s


def test_the_wake_command_and_the_member_list(w: World, svc: RoomService) -> None:
    """The member list says when held chatter wakes the agent; /wake sends it now and the time
    goes; with nothing waiting it says so. Fails if ``wakes_at`` isn't in the members frame or
    /wake doesn't reach the engine."""
    from test_commands import WEB

    p, m, _conn = claude(w)
    _pa, a = w.agent("codex-1")
    msg = w.agent_says(a, "just chatter")
    [row] = [r for r in svc.members("#build") if r["name"] == "claude-1"]
    assert row["wakes_at"] == pytest.approx(msg.ts + 270) and row["queued"] == 1
    assert svc.command("#build", "/wake claude-1", WEB)["text"] == "waking claude-1 for 1 waiting message"
    [push] = pushes(w)
    assert f"id={msg.id} " in push.text
    [row] = [r for r in svc.members("#build") if r["name"] == "claude-1"]
    assert row["wakes_at"] is None
    assert svc.command("#build", "/wake claude-1", WEB)["text"] == "nothing is waiting for claude-1"


def test_wake_refuses_a_held_member_and_a_paused_room(w: World, svc: RoomService) -> None:
    from test_commands import WEB

    p, m, _conn = claude(w)
    _pa, a = w.agent("codex-1")
    w.agent_says(a, "just chatter")
    svc.command("#build", "/hold claude-1", WEB)
    assert "is held" in svc.command("#build", "/wake claude-1", WEB)["text"]
    svc.command("#build", "/release claude-1", WEB)
    w.take()
    svc.command("#build", "/pause", WEB)
    assert "is paused" in svc.command("#build", "/wake claude-1", WEB)["text"]
    assert m.id not in w.engine.wake_now


def test_the_default_hold_is_just_inside_a_five_minute_cache() -> None:
    assert Config().delivery.unaddressed_hold_s == 270.0
