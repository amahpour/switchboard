"""/pause (DESIGN.md §8.5, §10): it stops every wake at once,
on every sink and path, including queued batches, pending Stop continuations and
open wait() calls (which return "paused"). A wait() issued during the pause stays
open and returns "paused" at its timeout, so a wait loop can't spin. read() still
works (an explicit pull). /resume delivers what waited.

Covered per path: wait() (test agent, Devin, Claude), the Stop hook the broker holds (Claude
Code, Codex, Cursor: released with no follow-up) and hook context (PostToolUse, UserPromptSubmit),
and the Devin Stop block and re-arm."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock
from engine_agents import fills, listener, result, session, stop
from engine_world import World
from test_devin_adapter import devin, wait_post
from test_devin_adapter import open_wait as devin_wait

from switchboard.config import Config
from switchboard.delivery.engine import paused_text
from switchboard.models import ResolveSink


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
    assert res == {"status": "paused", "text": paused_text(w.store.room_by_id(w.room.id))}
    # a person's /pause keeps its words (#239)
    assert res["text"] == (
        "[switchboard] #build is paused by your user. End your turn now; don't call wait() again"
        " until your user resumes the room."
    )
    assert not s.open


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


# ------------------------------------------------------ the Stop hook the broker holds
HARNESSES = ["claude", "codex", "cursor"]


@pytest.mark.parametrize("harness", HARNESSES)
def test_a_parked_stop_hook_is_released_with_no_continuation(w: World, harness: str) -> None:
    p, m, park = listener(w, f"{harness}-1", harness)
    pause(w)
    assert result(w, park) == {}  # the hook prints nothing: no follow-up
    assert w.engine.sinks.parks() == []
    assert [a for a in w.actions if isinstance(a, ResolveSink) and a.sink_id == park.sink_id]


@pytest.mark.parametrize("harness", HARNESSES)
def test_no_wake_while_paused_and_resume_comes_back_with_it_at_the_next_stop(w: World, harness: str) -> None:
    p, m, park = listener(w, f"{harness}-1", harness)
    pause(w)
    msg = w.human("hello")
    w.clock.advance(5)
    w.actions += w.engine.tick()
    assert not fills(w) and w.engine.parked_reason(m.id) is None  # paused is not parked
    resume(w)
    assert w.engine.parked_reason(m.id)  # stopped, not listening: needs a poke
    res = result(w, stop(w, p))
    assert res is not None and res["status"] == "messages" and f"id={msg.id} " in res["text"]


@pytest.mark.parametrize("harness", HARNESSES)
def test_a_stop_during_the_pause_does_not_park(w: World, harness: str) -> None:
    p, m = session(w, f"{harness}-1", harness, status="busy")
    pause(w)
    w.human("while paused")
    assert w.hook(p, "PostToolUse", ok=True) is None  # no context
    assert stop(w, p) is None  # answered at once: nothing held open until /resume
    assert w.engine.sinks.parks() == [] and w.p(p).status == "idle"


@pytest.mark.parametrize(
    ("harness", "event"),
    [
        ("claude", "PostToolUse"),
        ("claude", "PostToolUseFailure"),
        ("claude", "UserPromptSubmit"),
        ("cursor", "PostToolUse"),
        ("cursor", "PostToolUseFailure"),
    ],
)
def test_hook_context_is_paused(w: World, harness: str, event: str) -> None:
    p, m = session(w, f"{harness}-1", harness, status="busy")
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
    p, m = session(w, status="busy")
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


def test_leaving_the_only_live_room_releases_a_park_whose_other_rooms_are_paused(w: World) -> None:
    """Found by the property test: the park served #other only; after a kick from
    #other, every room it serves is paused, so it ends like a /pause would end it."""
    d = w.cfg.delivery
    other = w.store.create_room("#other", "alice", d.budget_per_hour, d.hop_limit)
    p, _m = session(w, "cursor-1", "cursor", status="busy")
    m2 = w.store.create_membership(other.id, p.id, "cursor-1", "h2")
    out = stop(w, p)
    pause(w)
    assert result(w, out) is None  # #other still live
    w.store.end_membership(m2.id, "kick", kicked=True)
    w.actions += w.engine.on_membership_ended(m2.id, "kick")
    assert result(w, out) == {}  # no continuation
    assert w.engine.sinks.parks() == []


def test_a_stop_parks_while_another_room_of_the_session_is_live(w: World) -> None:
    d = w.cfg.delivery
    other = w.store.create_room("#other", "alice", d.budget_per_hour, d.hop_limit)
    p, _m = session(w, "cursor-1", "cursor", status="busy")
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
    pc, mc, park = listener(w, "cursor-1", "cursor")
    s = listen(w, pt, mt, "w1")
    w.store.set_budget(w.room.id, 0)  # nothing wakes on the chatter itself
    w.store.set_hop_limit(w.room.id, 6)  # the guard ships off (#406); this room has it on
    _pa, ma = w.agent("a")
    for i in range(6):
        w.agent_says(ma, f"hop {i}")
    room = w.store.room_by_id(w.room.id)
    assert room.paused and room.paused_reason == "loop guard"
    assert w.resolved(s.id)[0]["status"] == "paused"
    assert result(w, park) == {}
