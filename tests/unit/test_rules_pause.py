"""/pause (DESIGN.md §8.5, §10): it stops every wake at once,
on every sink and path, including queued batches, pending Stop continuations and
open wait() calls (which return "paused"). A wait() issued during the pause stays
open and returns "paused" at its timeout, so a wait loop can't spin. read() still
works (an explicit pull). /resume delivers what waited.

Covered per path: wait() (test agent, Devin, Claude), the Claude inbox (idle wake and
the bypass mid-task push) and Claude hook context (PostToolUse, UserPromptSubmit),
Codex turn/start, turn/steer, codex queue and PostToolUse context, the Cursor stop
park and postToolUse context, the Devin Stop block and re-arm, and the runner (a
cancelled push never reaches its transport)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock
from engine_world import World
from test_claude_adapter import claude, reg
from test_codex_adapter import TID, attach, codex
from test_cursor_adapter import cursor, park_result, stop
from test_devin_adapter import devin, wait_post
from test_devin_adapter import open_wait as devin_wait

from switchboard.config import Config
from switchboard.delivery.engine import paused_text
from switchboard.delivery.runner import Runner
from switchboard.models import Push, ResolveSink


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def pause(w: World) -> None:
    """What `/pause` does: the command sets the flag, then the engine's pause actions."""
    w.store.set_paused(w.room.id, True, "paused by alice")
    w.actions += w.engine.on_command(w.room.id, "pause")


def resume(w: World) -> None:
    w.store.set_paused(w.room.id, False)
    w.actions += w.engine.on_command(w.room.id, "resume")


def pushes(w: World) -> list[Push]:
    return [a for a in w.take() if isinstance(a, Push)]


def budget(w: World) -> int:
    return w.store.room_by_id(w.room.id).budget_remaining


def listen(w: World, p: Any, m: Any, wid: str, secs: float = 50) -> Any:
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), wid, secs)
    w.actions += acts
    return sink


# ---------------------------------------------------------------- wait() sinks
@pytest.mark.parametrize("harness", ["test", "devin", "claude"])
def test_open_waits_return_paused_at_once(w: World, harness: str) -> None:
    p, m = w.agent("bot", harness=harness, hooks=harness != "test")
    s = listen(w, p, m, "w1")
    pause(w)
    [res] = w.resolved(s.id)
    assert res == {"status": "paused", "text": paused_text("#build")}
    assert "End your turn now" in res["text"] and not s.open


def test_a_wait_during_the_pause_stays_open_and_returns_paused_at_its_timeout(w: World) -> None:
    p, m = w.agent("bot")
    pause(w)
    s = listen(w, p, m, "w1", secs=30)
    w.human("while paused")
    assert s.open and w.resolved(s.id) == []  # no wake while paused
    w.clock.advance(30)
    w.actions += w.engine.tick()
    assert w.resolved(s.id)[0]["status"] == "paused"  # not "timeout": a wait loop can't spin


def test_resume_fills_a_wait_opened_during_the_pause(w: World) -> None:
    p, m = w.agent("bot")
    pause(w)
    s = listen(w, p, m, "w1")
    msg = w.human("while paused")
    resume(w)
    [res] = w.resolved(s.id)
    assert res["status"] == "messages" and f"id={msg.id} " in res["text"]


def test_read_still_works_while_paused(w: World) -> None:
    p, m = w.agent("bot", ack="immediate")
    pause(w)
    msg = w.human("an explicit pull is not a wake")
    text, bid, count, _more, _acts = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert count == 1 and f"id={msg.id} " in text and bid is not None


# ------------------------------------------------------------- queued batches
def test_queued_push_batches_are_cancelled_and_come_back_at_resume(w: World) -> None:
    pc, mc, _conn = claude(w, "claude-1")
    px, mx = codex(w)
    attach(w)
    msg = w.human("wake up")
    queued = {x.path: x for x in pushes(w)}
    assert set(queued) == {"inbox", "turn_start"}
    pause(w)
    for x in queued.values():
        b = w.store.get_batch(x.batch_id)
        assert b.state == "cancelled" and b.expire_reason == "pause"
    for m in (mc, mx):
        d = w.delivery(m, msg)
        assert d["state"] == "pending" and d["attempts"] == 0  # cancelled is not a failed attempt
    reg(w, pc, "idle")
    attach(w)
    resume(w)
    assert {x.path for x in pushes(w)} == {"inbox", "turn_start"}


def test_a_cancelled_push_never_reaches_its_transport(w: World) -> None:
    """The runner re-checks the batch before it hands a frame over (§8.5)."""
    pc, mc, conn = claude(w, "claude-1")
    w.human("wake up")
    [push] = pushes(w)
    pause(w)
    state = type("S", (), {"store": w.store, "engine": w.engine, "clock": w.clock})()
    asyncio.run(Runner(state)._push(push))  # type: ignore[arg-type]
    assert conn.pushes == [] and w.store.get_batch(push.batch_id).posted_at is None


def test_a_steer_queued_before_the_pause_is_cancelled(w: World) -> None:
    p, m = codex(w, status="busy")
    attach(w, view="busy")
    w.human("stop and add a test")
    [push] = pushes(w)
    assert push.path == "steer"
    pause(w)
    assert w.store.get_batch(push.batch_id).state == "cancelled"


# ------------------------------------------------------------------- Claude
def test_claude_inbox_no_idle_wake_while_paused(w: World) -> None:
    p, m, _conn = claude(w)
    pause(w)
    w.human("hello")
    w.clock.advance(5)
    reg(w, p, "idle")
    w.actions += w.engine.tick()
    assert pushes(w) == [] and w.engine.parked_reason(m.id) is None  # paused is not parked
    resume(w)
    [push] = pushes(w)
    assert push.path == "inbox"


def test_claude_bypass_mid_task_inbox_is_paused_too(w: World) -> None:
    p, m, _conn = claude(w, status="busy", mode="bypass", registry="busy")
    pause(w)
    w.human("mid-task, while paused")
    assert pushes(w) == []
    reg(w, p, "busy")
    resume(w)
    assert [x.path for x in pushes(w)] == ["inbox"]


@pytest.mark.parametrize("event", ["PostToolUse", "PostToolUseFailure", "UserPromptSubmit"])
def test_claude_hook_context_is_paused(w: World, event: str) -> None:
    p, m, _conn = claude(w, status="busy", attached=False, registry=None)
    pause(w)
    msg = w.human("mid-task, while paused")
    kw: dict[str, Any] = {"ok": event == "PostToolUse"} if event != "UserPromptSubmit" else {"gen": "g2"}
    assert w.hook(p, event, **kw) is None
    assert w.delivery(m, msg)["state"] == "pending"
    resume(w)
    out = w.hook(p, "PostToolUse", ok=True)
    assert out is not None and f"id={msg.id} " in out.text


def test_pausing_one_room_leaves_the_members_other_rooms_alone(w: World) -> None:
    d = w.cfg.delivery
    other = w.store.create_room("#other", "alice", d.budget_per_hour, d.hop_limit)
    p, m, _conn = claude(w, status="busy", attached=False, registry=None)
    m2 = w.store.create_membership(other.id, p.id, "claude-1", "h2")
    pause(w)
    w.human("in the paused room")
    msg = w.store.insert_message(
        other.id, sender_name="alice", sender_kind="human", via="web", text="in #other"
    )
    w.actions += w.engine.on_message(msg.id)
    out = w.hook(p, "PostToolUse", ok=True)
    assert out is not None and f"id={msg.id} " in out.text
    assert w.store.get_batch(out.batch_id).membership_id == m2.id


# -------------------------------------------------------------------- Codex
def test_codex_no_turn_start_steer_or_context_while_paused(w: World) -> None:
    p, m = codex(w)
    attach(w)
    pause(w)
    w.human("idle wake?")
    assert pushes(w) == []
    w.hook(p, "UserPromptSubmit", sid=TID, gen="t1")  # the thread's own human starts a turn
    attach(w, view="busy")
    w.actions += w.engine.evaluate(m.id)
    assert pushes(w) == []  # no steer either
    assert w.hook(p, "PostToolUse", sid=TID, tool="Bash", ok=True) is None
    resume(w)
    assert [x.path for x in pushes(w)] == ["steer"]


def test_codex_queue_tier_is_paused(w: World) -> None:
    p, m = codex(w, self_agent=True)
    a = attach(w)
    a.loaded = set()
    a.bin_path = "/usr/bin/true"
    pause(w)
    w.human("queue it?")
    assert pushes(w) == []
    resume(w)
    assert [x.path for x in pushes(w)] == ["queue"]


# ------------------------------------------------------------------- Cursor
def test_cursor_park_is_released_with_no_continuation(w: World) -> None:
    p, m = cursor(w, status="busy")
    out = stop(w, p)
    assert out.kind == "park"
    pause(w)
    assert park_result(w, out.sink_id) == {}  # the hook prints nothing: no follow-up
    assert w.engine.sinks.parks() == []
    assert [a for a in w.actions if isinstance(a, ResolveSink) and a.sink_id == out.sink_id]


def test_cursor_stop_during_the_pause_does_not_park(w: World) -> None:
    p, m = cursor(w, status="busy")
    pause(w)
    w.human("while paused")
    assert w.hook(p, "postToolUse", ok=True) is None  # no context
    assert stop(w, p) is None  # answered at once: nothing held open until /resume
    assert w.engine.sinks.parks() == [] and w.p(p).status == "idle"
    resume(w)
    assert w.engine.parked_reason(m.id)  # stopped, not listening: needs a poke
    out = stop(w, p, loop=0)
    res = park_result(w, out.sink_id)
    assert res is not None and res["status"] == "messages"  # the next stop gets it


def test_leaving_the_only_live_room_releases_a_park_whose_other_rooms_are_paused(w: World) -> None:
    """Found by the property test: the park served #other only; after a kick from
    #other, every room it serves is paused, so it ends like a /pause would end it."""
    d = w.cfg.delivery
    other = w.store.create_room("#other", "alice", d.budget_per_hour, d.hop_limit)
    p, _m = cursor(w, status="busy")
    m2 = w.store.create_membership(other.id, p.id, "cursor-1", "h2")
    out = stop(w, p)
    pause(w)
    assert park_result(w, out.sink_id) is None  # #other still live
    w.store.end_membership(m2.id, "kick", kicked=True)
    w.actions += w.engine.on_membership_ended(m2.id, "kick")
    assert park_result(w, out.sink_id) == {}  # no continuation
    assert w.engine.sinks.parks() == []


def test_cursor_stop_parks_while_another_room_of_the_session_is_live(w: World) -> None:
    d = w.cfg.delivery
    other = w.store.create_room("#other", "alice", d.budget_per_hour, d.hop_limit)
    p, _m = cursor(w, status="busy")
    w.store.create_membership(other.id, p.id, "cursor-1", "h2")
    pause(w)
    out = stop(w, p)
    assert out is not None and out.kind == "park"


# -------------------------------------------------------------------- Devin
def test_devin_wait_loop_stop_and_context_are_paused(w: World) -> None:
    p, m = devin(w)
    s = devin_wait(w, p, m)
    pause(w)
    assert w.resolved(s.id)[0]["status"] == "paused"
    before = budget(w)
    msg = w.human("while paused")
    assert w.hook(p, "PostToolUse", tool="read", ok=True) is None  # no context
    assert w.hook(p, "Stop") is None  # no Stop block with the message...
    assert w.hook(p, "Stop") is None  # ...and no "call wait()" re-arm
    assert budget(w) == before and w.store.count_events("rearm") == 0
    assert w.delivery(m, msg)["state"] == "pending"
    resume(w)
    w.hook(p, "UserPromptSubmit", gen="g2")
    out = w.hook(p, "Stop", gen="g2")
    assert out is not None and out.kind == "continue" and f"id={msg.id}" in out.text


def test_devin_wait_answer_already_given_is_not_taken_back(w: World) -> None:
    """A wait() result handed over before the pause is on its way to the model."""
    p, m = devin(w)
    s = devin_wait(w, p, m, tuid="call_A")
    w.human("just before the pause")
    bid = w.resolved(s.id)[-1]["batch_id"]
    pause(w)
    assert w.store.get_batch(bid).state == "offered"
    w.clock.advance(0.5)
    wait_post(w, p, m, bid, tuid="call_A")
    assert w.store.get_batch(bid).state == "confirmed"


# ---------------------------------------------------------- the loop guard too
def test_the_loop_guard_pause_is_the_same_pause(w: World) -> None:
    pt, mt = w.agent("bot")
    pc, mc = cursor(w, status="busy")
    park = stop(w, pc)
    s = listen(w, pt, mt, "w1")
    w.store.set_budget(w.room.id, 0)  # nothing wakes on the chatter itself
    _pa, ma = w.agent("a")
    for i in range(w.cfg.delivery.hop_limit):
        w.agent_says(ma, f"hop {i}")
    room = w.store.room_by_id(w.room.id)
    assert room.paused and room.paused_reason == "loop guard"
    assert w.resolved(s.id)[0]["status"] == "paused"
    assert park_result(w, park.sink_id) == {}
