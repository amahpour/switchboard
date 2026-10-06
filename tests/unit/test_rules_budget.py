"""Room wake budget (DESIGN.md §8.3): it counts wakes and
continuations (each is a full model turn), not posts; the default is 60 per room per
hour. When it runs out, agents wake only for the human's messages until the human
raises it with /budget. One notice per window."""

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
from test_rules_release import NOW, item, room

from switchboard.config import Config
from switchboard.delivery import rules
from switchboard.models import Notice, Push


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def left(w: World) -> int:
    return w.store.room_by_id(w.room.id).budget_remaining


def pushes(w: World) -> list[Push]:
    return [a for a in w.take() if isinstance(a, Push)]


def result(w: World, sink: Any) -> dict[str, Any] | None:
    """What a sink was resolved with (None while it is open)."""
    s = w.engine.sinks.get(sink.id)
    return None if s is None or s.open else s.result


def set_budget(w: World, n: int) -> None:
    """What `/budget n` does: store it, then re-evaluate the room."""
    w.store.set_budget(w.room.id, n)
    w.actions += w.engine.on_command(w.room.id, "budget")


# ------------------------------------------------------------------ pure rules
def test_budget_zero_blocks_mentions_and_chatter_but_not_humans() -> None:
    r0 = room(budget_remaining=0)
    assert (
        rules.releasable(
            [item(1, 1)],
            room=r0,
            eff="idle",
            peer_batch_boundary=-1,
            boundary_seq=0,
            now=NOW,
            quiet_s=3,
            max_hold_s=60,
            batch_max_msgs=20,
            max_chars=6000,
        )
        is None
    )
    r = rules.releasable(
        [item(1, 2), item(2, 0)],
        room=r0,
        eff="idle",
        peer_batch_boundary=-1,
        boundary_seq=0,
        now=NOW,
        quiet_s=3,
        max_hold_s=60,
        batch_max_msgs=20,
        max_chars=6000,
    )
    assert r is not None and r.counted and [i.message_id for i in r.items] == [1, 2]
    assert rules.budget_blocked([item(1, 1)], room=r0, eff="idle", peer_batch_boundary=-1, boundary_seq=0)
    assert not rules.budget_blocked([item(1, 2)], room=r0, eff="idle", peer_batch_boundary=-1, boundary_seq=0)
    assert not rules.budget_blocked(
        [item(1, 1)], room=room(), eff="idle", peer_batch_boundary=-1, boundary_seq=0
    )


def test_mid_task_priority_is_never_counted_nor_blocked() -> None:
    r = rules.releasable(
        [item(1, 1)],
        room=room(budget_remaining=0),
        eff="busy",
        peer_batch_boundary=-1,
        boundary_seq=0,
        now=NOW,
        quiet_s=3,
        max_hold_s=60,
        batch_max_msgs=20,
        max_chars=6000,
    )
    assert r is not None and r.kind == "priority" and not r.counted


# ------------------------------------------------------------- what counts
def test_wakes_decrement_budget_to_a_floor_and_notice_once(w: World) -> None:
    w.store.set_budget(w.room.id, 1)
    p, m = w.agent("bot")
    _p2, m2 = w.agent("peer")
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    w.actions += acts
    w.agent_says(m2, "chatter one")  # a chatter wake: counted
    assert w.resolved(sink.id)[0]["status"] == "messages"
    assert left(w) == 0
    notices = [a for a in w.take() if isinstance(a, Notice)]
    assert len(notices) == 1 and notices[0].level == "warn"
    # not only that wakes stopped: how to get going again (#118)
    assert "the wake budget for this hour is used up" in notices[0].text
    assert "Raise it with /budget <n> in the web UI." in notices[0].text
    # more chatter: blocked by the empty budget (no second notice this window)
    w.actions += w.engine.before_call(w.p(p))
    sink2, acts = w.engine.open_wait(w.p(p), w.m(m), "w2", 50)
    w.actions += acts
    w.agent_says(m2, "chatter two")
    assert w.resolved(sink2.id) == []
    assert not [a for a in w.take() if isinstance(a, Notice)]
    assert w.store.count_events("budget_exhausted") == 1
    # the human still gets through (and is counted, floor 0)
    w.human("human needs you")
    res = w.resolved(sink2.id)
    assert res and res[0]["status"] == "messages" and "human needs you" in res[0]["text"]
    assert left(w) == 0


def test_each_kind_of_wake_and_continuation_is_counted(w: World) -> None:
    """idle_wake (Claude inbox, Codex turn/start), wait_return, stop_cont (Cursor
    follow-up, Devin Stop block) and the Devin re-arm: one budget unit each."""
    pc, mc, _conn = claude(w, "claude-1")
    px, mx = codex(w)
    attach(w)
    pt, mt = w.agent("bot")
    pu, mu = cursor(w, status="busy")
    park = stop(w, pu)
    s, acts = w.engine.open_wait(w.p(pt), w.m(mt), "w1", 50)
    w.actions += acts
    pd, md = devin(w)
    w.hook(pd, "UserPromptSubmit", gen="g1")
    start = left(w)
    w.human("everyone, wake up")  # 5 wakes: inbox, turn/start, wait, follow-up; devin is busy
    assert {x.path for x in pushes(w)} == {"inbox", "turn_start"}
    assert park_result(w, park.sink_id)["status"] == "messages"
    assert result(w, s)["status"] == "messages"
    assert left(w) == start - 4
    out = w.hook(pd, "Stop", gen="g1")  # the Devin Stop block hands it over: counted
    assert out is not None and out.kind == "continue"
    assert left(w) == start - 5
    counted = {
        (b["path"], b["wake_kind"])
        for b in w.store.con.execute("SELECT path, wake_kind FROM batches WHERE budget_counted=1").fetchall()
    }
    assert counted == {
        ("inbox", "idle_wake"),
        ("turn_start", "idle_wake"),
        ("wait", "wait_return"),
        ("stop_followup", "stop_cont"),
        ("stop_block", "stop_cont"),
    }
    # the re-arm ("call wait()") is a counted continuation with no batch
    w.engine.on_hook_ack(out.batch_id, out.ack)
    w.hook(pd, "PreToolUse", gen="g1", tool="read", tool_use_id="t1")  # confirms the block
    w.store.mark_handled(md.id)
    before = left(w)
    out = w.hook(pd, "Stop", gen="g1")
    assert out is not None and out.kind == "continue" and "wait(" in out.text
    assert left(w) == before - 1 and w.store.count_events("rearm") == 1


def test_mid_task_delivery_and_pulls_are_not_counted(w: World) -> None:
    pc, mc, _conn = claude(w, "claude-1", status="busy", attached=False, registry=None)
    pb, mb, _c2 = claude(w, "claude-2", status="busy", mode="bypass", registry="busy")
    px, mx = codex(w, status="busy")
    attach(w, view="busy")
    pt, mt = w.agent("bot", ack="immediate")
    start = left(w)
    w.human("mid-task for everyone")
    assert {x.path for x in pushes(w)} == {"inbox", "steer"}  # bypass inbox, Codex steer
    out = w.hook(pc, "PostToolUse", ok=True)
    assert out is not None and w.store.get_batch(out.batch_id).path == "hook_ctx"
    out = w.hook(pc, "UserPromptSubmit", gen="g2")  # nothing left: the first batch is in flight
    w.engine.pull(w.p(pt), w.m(mt), "read", 20)
    assert left(w) == start
    assert w.store.con.execute("SELECT COUNT(*) FROM batches WHERE budget_counted=1").fetchone()[0] == 0


# --------------------------------------------------- exhausted: humans only
def test_exhausted_only_the_human_wakes_on_every_path(w: World) -> None:
    pc, mc, _conn = claude(w, "claude-1")
    px, mx = codex(w)
    attach(w)
    pt, mt = w.agent("bot")
    pu, mu = cursor(w, status="busy")
    park = stop(w, pu)
    s, acts = w.engine.open_wait(w.p(pt), w.m(mt), "w1", 50)
    w.actions += acts
    pd, md = devin(w)
    ds = devin_wait(w, pd, md)
    _pp, peer = w.agent("peer")
    w.store.set_budget(w.room.id, 0)
    names = ("claude-1", "codex-1", "bot", "cursor-1", "devin-1")
    w.agent_says(peer, "@" + " @".join(names), mentions=names)  # @mentions: need budget
    w.agent_says(peer, "plain chatter")
    assert pushes(w) == []
    assert s.open and ds.open and park_result(w, park.sink_id) is None
    w.human("the human always gets through")
    assert {x.path for x in pushes(w)} == {"inbox", "turn_start"}
    for sink in (s, ds):
        assert result(w, sink)["status"] == "messages"
    assert park_result(w, park.sink_id)["status"] == "messages"
    assert left(w) == 0 and w.store.count_events("budget_exhausted") == 1


def test_exhausted_devin_stop_neither_blocks_for_a_mention_nor_rearms(w: World) -> None:
    pd, md = devin(w)
    _pp, peer = w.agent("peer")
    w.hook(pd, "UserPromptSubmit", gen="g1")
    w.store.set_budget(w.room.id, 0)
    w.agent_says(peer, "@devin-1 look", mentions=("devin-1",))
    assert w.hook(pd, "Stop", gen="g1") is None
    assert w.store.count_events("rearm") == 0
    msg = w.human("but me")
    w.hook(pd, "UserPromptSubmit", gen="g2")
    out = w.hook(pd, "Stop", gen="g2")
    assert out is not None and f"id={msg.id}" in out.text


def test_raising_the_budget_releases_what_waited(w: World) -> None:
    p, m = w.agent("bot")
    _pp, peer = w.agent("peer")
    s, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    w.actions += acts
    set_budget(w, 0)
    msg = w.agent_says(peer, "@bot waiting on the budget", mentions=("bot",))
    assert s.open and w.delivery(m, msg)["state"] == "pending"
    set_budget(w, 3)
    [res] = w.resolved(s.id)
    assert res["status"] == "messages" and f"id={msg.id} " in res["text"]
    assert left(w) == 2


def test_budget_refills_each_hour_and_a_new_window_can_notice_again(w: World, clock: FakeClock) -> None:
    w.store.set_budget(w.room.id, 0)
    clock.advance(3601)
    assert w.store.refill_budget(w.room.id).budget_remaining == 60
    p, m = w.agent("bot")
    _pp, peer = w.agent("peer")
    w.store.set_budget(w.room.id, 0)
    s, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 7200)
    w.actions += acts
    w.agent_says(peer, "@bot one", mentions=("bot",))
    assert w.store.count_events("budget_exhausted") == 1
    clock.advance(3600)
    w.actions += w.engine.tick()  # refilled: the mention goes out
    assert w.resolved(s.id)[0]["status"] == "messages"
    assert left(w) == 59


def test_priority_hook_context_is_not_counted(w: World) -> None:
    p, m = w.agent("bot", status="busy")
    w.human("hi @bot", mentions=("bot",))
    out = w.hook(p, "PostToolUse", ok=True)
    assert out is not None and out.kind == "context"
    assert left(w) == 60


def test_idle_claude_on_the_inbox_at_zero_budget_waits_for_the_human(w: World, clock: FakeClock) -> None:
    p, m, _conn = claude(w, "claude-1")
    _pp, peer = w.agent("peer")
    w.store.set_budget(w.room.id, 0)
    w.agent_says(peer, "@claude-1 hi", mentions=("claude-1",))
    clock.advance(1)
    reg(w, p, "idle")
    w.actions += w.engine.tick()
    assert pushes(w) == [] and w.engine.parked_reason(m.id) is None  # budget-held is not parked
    w.human("me")
    [push] = pushes(w)
    assert push.path == "inbox"
