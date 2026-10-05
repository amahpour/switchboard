"""The Devin adapter with a FakeClock (DESIGN.md §9.5, §8.7): the wait loop and its
two-phase ack (the wait() call's own PostToolUse, success and tool_use_id),
supersede, orphaned waits, PostToolUse context, the subagent taint (no context,
no continue), the Stop block with a wake batch, and the budgeted re-arm."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock
from engine_world import KEY, World

from switchboard import envelope
from switchboard.adapters.devin import BLOCK_CONFIRM_S, TAINT_WHY, WAIT_TOOL, DevinAdapter
from switchboard.config import Config
from switchboard.models import Release


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def ad(w: World) -> DevinAdapter:
    a = w.engine.adapters["devin"]
    assert isinstance(a, DevinAdapter)
    return a


def devin(w: World, name: str = "devin-1", status: str = "busy") -> tuple[Any, Any]:
    return w.agent(name, harness="devin", status=status, hooks=True)


def tokens(b_id: int, m: Any) -> tuple[tuple[int, str], ...]:
    t = envelope.TOKEN_RE.fullmatch(envelope.batch_token(KEY, b_id, m.id))
    return ((int(t.group(1)), t.group(2)),)


def open_wait(w: World, p: Any, m: Any, *, tuid: str | None = "call_w1", wid: str = "w1") -> Any:
    if tuid is not None:
        w.hook(p, "PreToolUse", tool=WAIT_TOOL, tool_use_id=tuid)
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), wid, 600)
    w.actions += acts
    return sink


def wait_post(
    w: World, p: Any, m: Any, bid: int, *, tuid: str = "call_w1", ok: bool = True, tool: str = WAIT_TOOL
) -> Any:
    return w.hook(p, "PostToolUse", tool=tool, tool_use_id=tuid, ok=ok, tokens=tokens(bid, m))


# ------------------------------------------------------------------ basics
def test_tier_context_and_route(w: World, clock: FakeClock) -> None:
    a = ad(w)
    p, _m = devin(w)
    assert a.tier(w.p(p)) == ("devin:wait-loop", None)
    assert a.context_events(w.p(p)) == frozenset({"PostToolUse"})
    prio = Release(items=(), kind="priority", counted=False, reason="human")
    wake = Release(items=(), kind="wake", counted=True, reason="human")
    assert a.route(w.p(p), prio, None, clock.now()).kind == "pull"
    assert a.route(w.p(p), wake, None, clock.now()).kind == "none"
    pt = w.store.update_participant(p.id, gen_tainted=1)
    assert a.context_events(pt) == frozenset()
    r = a.route(pt, prio, None, clock.now())
    assert r.kind == "none" and r.reason == TAINT_WHY


def test_posttooluse_context_for_priority_items(w: World) -> None:
    p, m = devin(w)
    msg = w.human("mid-task note")
    out = w.hook(p, "PostToolUse", tool="read", ok=True)
    assert out is not None and out.kind == "context" and f"id={msg.id}" in out.text
    w.engine.on_hook_ack(out.batch_id, out.ack)
    assert w.delivery(m, msg)["state"] == "in_context"


# --------------------------------------------------------------- wait loop
def test_wait_answer_is_confirmed_only_by_its_own_successful_posttooluse(w: World, clock: FakeClock) -> None:
    p, m = devin(w)
    sink = open_wait(w, p, m, tuid="call_A")
    assert sink.meta["tool_use_id"] == "call_A"
    msg = w.human("wake up")
    res = w.resolved(sink.id)[-1]
    assert res["status"] == "messages"
    bid = res["batch_id"]
    b = w.store.get_batch(bid)
    assert (b.path, b.wake_kind, b.budget_counted) == ("wait", "wait_return", True)
    clock.advance(0.2)
    wait_post(w, p, m, bid, tuid="call_B")  # another call's PostToolUse: no
    wait_post(w, p, m, bid, tuid="call_A", ok=False)  # failed (cancelled): no
    assert w.store.get_batch(bid).state == "offered"
    clock.advance(0.3)
    wait_post(w, p, m, bid, tuid="call_A")
    b = w.store.get_batch(bid)
    assert b.state == "confirmed" and b.turn_start_at == clock.now()
    assert w.delivery(m, msg)["state"] == "in_context"
    clock.advance(0.9)
    w.hook(p, "PreToolUse", tool="mcp__switchboard__say", tool_use_id="call_S")
    b = w.store.get_batch(bid)
    assert b.first_action_at == clock.now()
    assert w.store.count_events("first_action") == 1


def test_a_token_in_another_tools_output_does_not_confirm_a_wait(w: World, clock: FakeClock) -> None:
    p, m = devin(w)
    sink = open_wait(w, p, m, tuid="call_A")
    w.human("x")
    bid = w.resolved(sink.id)[-1]["batch_id"]
    clock.advance(0.5)
    wait_post(w, p, m, bid, tuid="call_X", tool="exec")  # e.g. the model echoed the token
    b = w.store.get_batch(bid)
    assert b.state == "expired" and b.confirmed_at is None  # and the agent moved on: it comes back


def test_a_newer_wait_supersedes_the_older_one(w: World) -> None:
    p, m = devin(w)
    s1 = open_wait(w, p, m, tuid="c1", wid="w1")
    s2 = open_wait(w, p, m, tuid="c2", wid="w2")
    assert w.resolved(s1.id)[-1]["status"] == "superseded"
    msg = w.human("to the newest wait")
    res = w.resolved(s2.id)[-1]
    assert res["status"] == "messages" and f"id={msg.id}" in res["text"]


def test_an_orphaned_wait_is_closed_by_the_next_hook_and_loses_nothing(w: World, clock: FakeClock) -> None:
    p, m = devin(w)
    sink = open_wait(w, p, m)
    clock.advance(5)
    # the human interrupted (no cancel reaches the MCP server) and typed
    w.hook(p, "UserPromptSubmit", gen="p2", t=clock.now())
    assert w.resolved(sink.id)[-1]["status"] == "superseded"
    msg = w.human("hello again")
    assert w.delivery(m, msg)["state"] == "pending"  # nobody swallowed it
    assert w.engine.sinks.for_participant(p.id) == []


def test_an_answer_given_to_an_orphan_expires_at_the_next_hook(w: World, clock: FakeClock) -> None:
    p, m = devin(w)
    sink = open_wait(w, p, m)
    msg = w.human("into the orphan")
    bid = w.resolved(sink.id)[-1]["batch_id"]
    clock.advance(2)
    w.hook(p, "PreToolUse", tool="read", tool_use_id="call_R", t=clock.now())
    b = w.store.get_batch(bid)
    assert b.state == "expired" and b.expire_reason == "hook:PreToolUse"
    assert w.delivery(m, msg)["state"] == "pending"


def test_hooks_that_started_before_the_wait_leave_it_open(w: World, clock: FakeClock) -> None:
    p, m = devin(w)
    t_before = clock.now()
    clock.advance(1)
    sink = open_wait(w, p, m)
    w.hook(p, "PostToolUse", tool="read", ok=True, t=t_before)  # a late hook of an earlier tool
    assert w.engine.sinks.get(sink.id).open


# ------------------------------------------------------------------- taint
def test_a_background_subagent_taints_the_prompt(w: World) -> None:
    p, m = devin(w)
    w.hook(p, "UserPromptSubmit", gen="p1")
    w.hook(p, "PreToolUse", tool="run_subagent", subagent_bg=True)
    assert w.p(p).gen_tainted
    msg = w.human("do not leak into the subagent")
    assert w.hook(p, "PostToolUse", tool="read", ok=True) is None  # no context
    seq = w.p(p).boundary_seq
    assert w.hook(p, "Stop") is None  # no continue: it would continue the subagent
    assert w.p(p).status == "busy" and w.p(p).boundary_seq == seq  # untouched
    assert w.delivery(m, msg)["state"] == "pending"
    assert w.engine.parked_reason(m.id) == TAINT_WHY
    w.hook(p, "UserPromptSubmit", gen="p2")  # the next prompt clears it
    assert not w.p(p).gen_tainted


def test_a_background_subagent_that_outlives_its_prompt_stays_tainted(w: World, clock: FakeClock) -> None:
    """Its hooks carry the prompt id it started in (F§6 5.2), after the human's next
    prompt too: no context and no Stop continue for them (a continue would continue
    the subagent), and they don't touch the main agent's status or wait()."""
    p, m = devin(w)
    w.hook(p, "UserPromptSubmit", gen="prompt-1")
    w.hook(p, "PreToolUse", gen="prompt-1", tool="run_subagent", subagent_bg=True)
    assert w.hook(p, "Stop", gen="prompt-1") is None
    clock.advance(1)
    w.hook(p, "UserPromptSubmit", gen="prompt-2")  # the human's next prompt; the subagent runs on
    assert not w.p(p).gen_tainted
    sink = open_wait(w, p, m, tuid="call_main")  # the main agent listens
    assert w.engine.sinks.get(sink.id).open
    seq, status = w.p(p).boundary_seq, w.p(p).status
    clock.advance(1)
    w.store.set_paused(w.room.id, True, "hold")  # keep the message out of the open wait()
    msg = w.human("please do X")
    w.store.set_paused(w.room.id, False)
    assert w.hook(p, "PostToolUse", gen="prompt-1", tool="read", ok=True) is None  # the subagent: no context
    assert w.delivery(m, msg)["state"] == "pending"
    assert w.hook(p, "Stop", gen="prompt-1") is None  # the subagent's Stop: no block, no re-arm
    assert w.store.count_events("rearm") == 0
    assert w.engine.sinks.get(sink.id).open  # the main agent's wait() is not an orphan
    assert (w.p(p).boundary_seq, w.p(p).status) == (seq, status)
    # the main agent's own hooks (prompt-2) work as usual
    out = w.hook(p, "PostToolUse", gen="prompt-2", tool="read", ok=True)
    assert out is not None and out.kind == "context" and f"id={msg.id}" in out.text


def test_a_newer_prompt_that_reuses_a_tainted_id_stays_tainted(w: World) -> None:
    p, _m = devin(w)
    w.hook(p, "UserPromptSubmit", gen="p1")
    w.hook(p, "PreToolUse", gen="p1", tool="run_subagent", subagent_bg=True)
    w.hook(p, "UserPromptSubmit", gen="p2")
    w.hook(p, "UserPromptSubmit", gen="p1")
    assert w.p(p).gen_tainted


# ------------------------------------------------------------ Stop block
def test_stop_returns_a_block_with_the_pending_wake(w: World, clock: FakeClock) -> None:
    p, m = devin(w)
    _q, qm = w.agent("codex-1", harness="test")
    h = w.human("please review")
    peer = w.agent_says(qm, "PEER-SECRET @devin-1", mentions=("devin-1",))
    budget = w.store.room_by_id(w.room.id).budget_remaining
    out = w.hook(p, "Stop")
    assert out is not None and out.kind == "continue" and out.batch_id and out.ack
    assert f"id={h.id}" in out.text and "please review" in out.text
    assert f"id={peer.id}" in out.text and "PEER-SECRET" not in out.text  # user role: stubbed
    b = w.store.get_batch(out.batch_id)
    assert (b.path, b.wake_kind, b.budget_counted) == ("stop_block", "stop_cont", True)
    assert w.store.room_by_id(w.room.id).budget_remaining == budget - 1
    assert w.p(p).status == "busy"
    w.engine.on_hook_ack(out.batch_id, out.ack)
    assert w.store.get_batch(out.batch_id).state == "offered"
    clock.advance(1.1)
    w.hook(p, "PreToolUse", tool="mcp__switchboard__say", tool_use_id="c9", t=clock.now())
    b = w.store.get_batch(out.batch_id)
    assert b.state == "confirmed" and b.first_action_at == clock.now()
    assert w.delivery(m, h)["state"] == "in_context"


def test_a_block_or_rearm_no_hook_ever_follows_leaves_the_member_parked(w: World, clock: FakeClock) -> None:
    """The continue was never acted on (e.g. an interrupt deferred it): no turn ran,
    so the member goes back to idle and shows parked instead of busy for good."""
    p, m = devin(w)
    h = w.human("x")
    out = w.hook(p, "Stop")
    w.engine.on_hook_ack(out.batch_id, out.ack)
    assert w.p(p).status == "busy"
    clock.advance(BLOCK_CONFIRM_S + 1)
    w.actions += w.engine.tick()
    assert w.store.get_batch(out.batch_id).expire_reason == "no_hook"
    assert w.p(p).status == "idle" and w.delivery(m, h)["state"] == "pending"
    assert w.engine.parked_reason(m.id) is not None
    # a re-arm that is never acted on, likewise
    q, qm = devin(w, "devin-2")
    w.hook(q, "UserPromptSubmit", gen="q1")
    out = w.hook(q, "Stop")
    assert out is not None and out.batch_id is None and w.p(q).status == "busy"
    clock.advance(BLOCK_CONFIRM_S - 1)
    w.engine.tick()
    assert w.p(q).status == "busy"
    clock.advance(2)
    w.actions += w.engine.tick()
    assert w.p(q).status == "idle"
    # ... but a re-arm the agent acts on (its wait() call) keeps the turn's status
    w.hook(q, "UserPromptSubmit", gen="q2")
    w.hook(q, "Stop")
    clock.advance(1)
    w.hook(q, "PreToolUse", tool=WAIT_TOOL, tool_use_id="c1")
    clock.advance(BLOCK_CONFIRM_S + 1)
    w.engine.tick()
    assert w.p(q).status == "busy"


def test_no_first_action_when_the_turn_ends_without_a_tool_call(w: World, clock: FakeClock) -> None:
    """A wake answered in text only: the next PreToolUse (the re-armed wait()) is
    not that wake's first action."""
    p, m = devin(w)
    sink = open_wait(w, p, m, tuid="c1")
    w.human("fyi, no reply needed")
    bid = w.resolved(sink.id)[-1]["batch_id"]
    clock.advance(0.5)
    wait_post(w, p, m, bid, tuid="c1")
    assert w.store.get_batch(bid).turn_start_at == clock.now()
    clock.advance(1)
    w.hook(p, "Stop")  # answered in text; the Stop re-arms
    clock.advance(20)
    w.hook(p, "PreToolUse", tool=WAIT_TOOL, tool_use_id="c2")
    assert w.store.get_batch(bid).first_action_at is None
    assert w.store.count_events("first_action") == 0


def test_a_block_not_acted_on_before_a_new_prompt_expires(w: World) -> None:
    p, m = devin(w)
    h = w.human("x")
    out = w.hook(p, "Stop")
    w.engine.on_hook_ack(out.batch_id, out.ack)
    w.hook(p, "UserPromptSubmit", gen="p9")
    b = w.store.get_batch(out.batch_id)
    assert b.state == "expired" and b.expire_reason == "new_prompt"
    assert w.delivery(m, h)["state"] == "pending"


def test_stop_rearms_the_wait_loop_within_budget_and_the_per_prompt_cap(w: World) -> None:
    p, _m = devin(w)
    w.hook(p, "UserPromptSubmit", gen="p1")
    budget = w.store.room_by_id(w.room.id).budget_remaining
    for n in (1, 2):
        out = w.hook(p, "Stop")
        assert out is not None and out.kind == "continue" and out.batch_id is None
        assert out.text.startswith("[switchboard]") and 'wait("#build", 600)' in out.text
        assert w.p(p).rearms_in_gen == n
    assert w.store.room_by_id(w.room.id).budget_remaining == budget - 2
    assert w.store.count_events("rearm") == 2
    assert w.hook(p, "Stop") is None  # rearm_max_per_prompt reached
    w.hook(p, "UserPromptSubmit", gen="p2")
    assert w.hook(p, "Stop") is not None


def test_rearms_are_capped_per_hour_so_prompt_stop_cycles_cant_drain_the_room(
    w: World, clock: FakeClock
) -> None:
    """The per-prompt count resets at every prompt; forged prompt/Stop cycles from
    the agent's tree still can't spend more than rearm_max_per_hour of the room's budget."""
    p, _m = devin(w)
    cap = w.cfg.devin.rearm_max_per_hour
    budget = w.store.room_by_id(w.room.id).budget_remaining
    n = 0
    for i in range(40):
        w.hook(p, "UserPromptSubmit", gen=f"g{i}")
        for _ in range(3):
            n += w.hook(p, "Stop", gen=f"g{i}") is not None
    assert n == cap and 0 < cap < budget
    assert w.store.room_by_id(w.room.id).budget_remaining == budget - cap
    clock.advance(3601)
    w.hook(p, "UserPromptSubmit", gen="later")
    assert w.hook(p, "Stop", gen="later") is not None  # the window moved on


def test_no_rearm_when_paused_out_of_budget_or_off(w: World, tmp_path: Path, clock: FakeClock) -> None:
    p, _m = devin(w)
    w.store.set_paused(w.room.id, True, "test")
    assert w.hook(p, "Stop") is None
    w.store.set_paused(w.room.id, False)
    w.store.set_budget(w.room.id, 0)
    assert w.hook(p, "Stop") is None
    (tmp_path / "b").mkdir()
    w2 = World(
        tmp_path / "b", clock, Config().replace(devin=dataclasses.replace(Config().devin, rearm=False))
    )
    q, _qm = w2.agent("devin-2", harness="devin", status="busy", hooks=True)
    assert w2.hook(q, "Stop") is None


def test_devin_never_gets_a_decision_other_than_block() -> None:
    """The hook's output table: PreToolUse/PermissionRequest print nothing for Devin."""
    from switchboard.hook.switchboard_hook import render

    for ev in ("PreToolUse", "PermissionRequest", "UserPromptSubmit", "SessionStart", "SessionEnd"):
        assert render("devin", ev, {"kind": "continue", "text": "x"}) is None
        assert render("devin", ev, {"kind": "context", "text": "x"}) is None
    assert render("devin", "Stop", {"kind": "continue", "text": "x"}) == {"decision": "block", "reason": "x"}


def test_caps(w: World) -> None:
    p, _m = devin(w)
    caps = ad(w).caps(w.p(p))
    assert caps.wait_cap_s == 600 and caps.ctx_max_chars == 6000
    assert "stop_block" not in caps.inline_paths and "wait" in caps.inline_paths
