"""Loop guard (DESIGN.md §8.5): a per-room hop counter across
any number of agents. After 6 agent messages with no human message the room pauses
itself (the same pause as /pause) and the human is told. Only the human's messages
(web or CLI) and /resume reset the counter."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock
from engine_world import World
from test_rules_release import room

from switchboard.config import Config
from switchboard.delivery import rules
from switchboard.models import Notice


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def warn_notices(w: World) -> list[Notice]:
    return [a for a in w.take() if isinstance(a, Notice) and a.level == "warn"]


def test_hop_tripped() -> None:
    assert not rules.hop_tripped(room(hop_count=5))
    assert rules.hop_tripped(room(hop_count=6))
    assert not rules.hop_tripped(room(hop_count=9, paused=True))
    assert not rules.hop_tripped(room(hop_count=9, hop_limit=0))


@pytest.mark.parametrize("n_agents", [1, 2, 3, 5])
def test_trips_at_exactly_six_across_any_number_of_agents(w: World, n_agents: int) -> None:
    ms = [w.agent(f"a{i}")[1] for i in range(n_agents)]
    for i in range(5):
        w.agent_says(ms[i % n_agents], f"hop {i}")
    r = w.store.room_by_id(w.room.id)
    assert r.hop_count == 5 and not r.paused
    assert not warn_notices(w)
    w.agent_says(ms[5 % n_agents], "hop 5")
    r = w.store.room_by_id(w.room.id)
    assert r.paused and r.paused_reason == "loop guard" and r.hop_count == 6
    [note] = warn_notices(w)
    assert "loop guard" in note.text and "6 agent messages" in note.text and "/resume" in note.text
    assert note.persist  # a room notice the human sees in history too
    [ev] = w.store.recent_events(kinds=("loop_guard",))
    assert ev.room_id == w.room.id


def test_only_the_humans_messages_reset_the_counter(w: World) -> None:
    _p, ma = w.agent("a")
    for i in range(5):
        w.agent_says(ma, f"hop {i}")
    # a system notice (e.g. a join line, a command audit) is not the human
    w.store.insert_message(
        w.room.id,
        sender_name="switchboard",
        sender_kind="system",
        via="system",
        kind="notice",
        text="a notice",
    )
    w.store.insert_message(
        w.room.id, sender_name="a", sender_kind="agent", via="mcp", kind="join", text="joined"
    )
    assert w.store.room_by_id(w.room.id).hop_count == 5
    msg = w.store.insert_message(
        w.room.id, sender_name="alice", sender_kind="human", via="cli", text="from the CLI"
    )
    w.actions += w.engine.on_message(msg.id)
    assert w.store.room_by_id(w.room.id).hop_count == 0  # the CLI is the human too
    for i in range(5):
        w.agent_says(ma, f"again {i}")
    w.human("and from the web")
    assert w.store.room_by_id(w.room.id).hop_count == 0
    assert not w.store.room_by_id(w.room.id).paused


def test_the_tripping_message_waits_for_resume(w: World) -> None:
    pa, ma = w.agent("a")
    pb, mb = w.agent("b")
    last = None
    for i in range(6):
        sink, acts = w.engine.open_wait(w.p(pb), w.m(mb), f"wb{i}", 50)
        w.actions += acts
        last = w.agent_says(ma, f"@b hop {i}", mentions=("b",))
        if i < 5:
            assert w.resolved(sink.id)[-1]["status"] == "messages"
            w.actions += w.engine.before_call(w.p(pb))
    assert w.resolved(sink.id)[-1]["status"] == "paused"  # the 6th is stored, not delivered
    assert w.delivery(mb, last)["state"] == "pending"
    # more agent messages while paused don't trip it again
    w.agent_says(ma, "still talking")
    assert w.store.count_events("loop_guard") == 1
    assert w.store.room_by_id(w.room.id).hop_count == 7
    # /resume resets the counter and delivers what waited
    sink, acts = w.engine.open_wait(w.p(pb), w.m(mb), "after", 50)
    w.actions += acts
    r = w.store.set_paused(w.room.id, False)
    assert r.hop_count == 0 and not r.paused
    w.actions += w.engine.on_command(w.room.id, "resume")
    [res] = w.resolved(sink.id)
    assert res["status"] == "messages" and f"id={last.id} " in res["text"]


def test_six_agent_messages_pause_and_a_human_resets(w: World) -> None:
    _pa, ma = w.agent("a")
    pb, mb = w.agent("b")
    sink, acts = w.engine.open_wait(w.p(pb), w.m(mb), "wb", 50)
    w.actions += acts
    for i in range(5):
        w.agent_says(ma, f"hop {i}")
    assert not w.store.room_by_id(w.room.id).paused
    w.human("human in the loop")  # resets the counter
    assert w.store.room_by_id(w.room.id).hop_count == 0
    w.take()
    for i in range(6):
        w.agent_says(ma if i % 2 else mb, f"again {i}")
    r = w.store.room_by_id(w.room.id)
    assert r.paused and r.paused_reason == "loop guard"
    assert w.store.count_events("loop_guard") == 1


def test_loop_guard_pause_answers_open_waits(w: World) -> None:
    _pa, ma = w.agent("a")
    pc, mc = w.agent("c")
    w.store.set_budget(w.room.id, 0)  # no wakes, so c's wait stays open until the pause
    sink, acts = w.engine.open_wait(w.p(pc), w.m(mc), "wc", 50)
    w.actions += acts
    for i in range(6):
        w.agent_says(ma, f"chatter {i}")
    [res] = w.resolved(sink.id)
    assert res["status"] == "paused" and "paused" in res["text"]


def test_a_hop_limit_of_zero_turns_the_guard_off(tmp_path: Path, clock: FakeClock) -> None:
    w = World(tmp_path, clock, Config().with_delivery(hop_limit=0))
    _pa, ma = w.agent("a")
    for i in range(20):
        w.agent_says(ma, f"hop {i}")
    assert not w.store.room_by_id(w.room.id).paused and w.store.count_events("loop_guard") == 0


def test_a_human_message_during_the_guard_pause_does_not_resume(w: World) -> None:
    _pa, ma = w.agent("a")
    for i in range(6):
        w.agent_says(ma, f"hop {i}")
    w.human("I see it")
    r = w.store.room_by_id(w.room.id)
    assert r.paused and r.hop_count == 0  # counter reset, but only /resume unpauses
