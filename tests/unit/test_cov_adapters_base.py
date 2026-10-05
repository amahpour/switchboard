"""The adapter interface's defaults and the small hook-only adapters, called directly
(DESIGN.md §9.1, §9.4-§9.6): the base ``Adapter`` answers every harness question
with its conservative default, ``PullAdapter`` routes an unknown harness to a
sink, a pull or nowhere, and the Cursor, Devin and test-agent verdicts for the
events their engine-level tests don't reach."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest

from switchboard.adapters import build_adapters
from switchboard.adapters.base import HOOK_CONTEXT_EVENTS, INLINE_PATHS, Adapter, Caps, PullAdapter, SendError
from switchboard.adapters.cursor import FOLLOWUP_CONFIRM_S, UNBOUND_WHY, CursorAdapter
from switchboard.adapters.devin import BLOCK_CONFIRM_S, TAINT_WHY, WAIT_TOOL, DevinAdapter, rearm_text
from switchboard.adapters.testagent import ACK_MODES, TestAgentAdapter
from switchboard.config import Config
from switchboard.models import Batch, HookEvent, Participant, Release

CFG = Config(human_name="alice")
WAKE = Release(items=(), kind="wake", counted=True, reason="human")
PRIO = Release(items=(), kind="priority", counted=False, reason="human")
SINK = SimpleNamespace(path="wait", id=41)


def part(**kw: Any) -> Participant:
    """A participant row with neutral defaults (no store needed: routing is pure)."""
    base: dict[str, Any] = {f.name: None for f in dataclasses.fields(Participant)}
    base.update(
        id=7,
        harness="unknown",
        session_key="unknown:s1",
        bind_state="bound",
        thread_proof=False,
        status="idle",
        approval_mode="default",
        env_leak=False,
        boundary_seq=0,
        gen_tainted=False,
        rearms_in_gen=0,
        unconfirmed_followups=0,
        push_expiries=0,
        created_at=1.0,
    )
    base.update(kw)
    return Participant(**base)


def ev(event: str, harness: str = "test", **kw: Any) -> HookEvent:
    return HookEvent(harness=harness, event=event, **kw)


def batch(**kw: Any) -> Batch:
    base: dict[str, Any] = {f.name: None for f in dataclasses.fields(Batch)}
    base.update(
        id=3, membership_id=1, path="wait", kind="wake", budget_counted=True, state="offered", created_at=1.0
    )
    base.update(kw)
    return Batch(**base)


# ------------------------------------------------------------------ base
def test_send_error_truncates_its_reason_and_marks_reroutes() -> None:
    e = SendError("x" * 200)
    assert e.reason == "x" * 80 and e.counted is True and str(e) == "x" * 200
    assert SendError("turn ended", counted=False).counted is False


def test_the_base_adapter_defaults_are_the_conservative_answers() -> None:
    a = Adapter(CFG)
    p = part()
    b = batch()
    assert a.harness == "unknown" and a.serial_push is False
    # an unknown harness: the delivery batch limit and a 50 s wait cap
    assert a.caps(p) == Caps(ctx_max_chars=CFG.delivery.batch_max_chars, wait_cap_s=50)
    assert a.caps().inline_paths == INLINE_PATHS
    assert a.tier(p) == ("mcp-only", None)
    assert a.conn_tier(SimpleNamespace(mcp_pid=1), p) == ("mcp-only", None)
    assert a.on_joined(p, "n" * 16, True) is None
    assert a.defer_end(p) is False
    assert a.on_mcp_hello(SimpleNamespace(harness="unknown"), [p]) is None
    assert a.status_summary() is None
    assert a.join_guidance(p, "#build") == (
        'switchboard can\'t wake this session on its own. Call read("#build") to check for messages,'
        ' or wait("#build", 50) to block until one arrives.'
    )
    assert a.context_events(p) == frozenset()
    assert a.on_hook(p, ev("Stop")) is None
    assert a.park_s(p, ev("Stop")) is None
    assert a.stop_continues(p) is False
    assert a.redeliver_on_stop(p, ev("Stop")) is True
    assert a.closes_waits(p, ev("PostToolUse")) is False
    assert a.is_wait_pre(ev("PreToolUse")) is False
    assert a.pull_confirms("wait", "tu-1", ev("PostToolUse")) is True
    assert a.continue_verdict(p, 3, True, ev("Stop")) is None
    assert a.confirm_window_s() == 180.0
    assert a.expire_due(p, b, 10.0) is None
    assert a.push_expired(p, b, "offline", 10.0) is None


async def test_the_base_adapter_has_no_transport_and_nothing_to_start() -> None:
    a = Adapter(CFG)
    with pytest.raises(NotImplementedError, match="unknown has no push transport"):
        await a.send(part(), batch(), "hi")
    assert await a.start(object()) is None
    assert await a.stop() is None


def test_caps_come_from_each_harness_config_capped_by_the_hook_script() -> None:
    ads = build_adapters(CFG)
    assert ads["codex"].caps() == Caps(CFG.codex.ctx_max_chars, CFG.codex.wait_cap_s)
    assert ads["cursor"].caps() == Caps(CFG.cursor.ctx_max_chars, CFG.cursor.wait_cap_s)
    assert ads["devin"].caps() == Caps(CFG.devin.ctx_max_chars, CFG.devin.wait_cap_s)
    # a config over the hook script's own print limit is cut down to it (the script never cuts)
    big = CFG.replace(cursor=dataclasses.replace(CFG.cursor, ctx_max_chars=50_000))
    assert CursorAdapter(big).caps().ctx_max_chars == 8000


def test_pull_adapter_routes_to_a_sink_a_pull_or_nowhere() -> None:
    a = PullAdapter("unknown", CFG)
    p = part()
    assert a.harness == "unknown"
    assert a.tier(p) == ("mcp-only", None)
    assert a.context_events(p) == frozenset()
    r = a.route(p, WAKE, SINK, 0.0)
    assert (r.kind, r.path, r.sink_id) == ("sink", "wait", 41)
    r = a.route(p, PRIO, None, 0.0)
    assert (r.kind, r.reason) == ("pull", "next tool call")
    r = a.route(p, WAKE, None, 0.0)
    assert r.kind == "none" and "call wait() or poke it" in (r.reason or "")


def test_pull_adapter_takes_a_codex_thread_id_on_trust_only_as_unverified() -> None:
    a = PullAdapter("codex", CFG)
    assert a.tier(part(harness="codex")) == ("mcp-only", "unverified thread")
    assert a.context_events(part(harness="codex")) == frozenset()


# ------------------------------------------------------------- test agent
def test_the_scripted_test_agent_adapter() -> None:
    a = TestAgentAdapter(CFG)
    p = part(harness="test", session_key="test:bot")
    assert ACK_MODES == ("next_call", "immediate", "never")
    assert a.tier(p) == ("mcp-only", None)
    assert a.context_events(p) == HOOK_CONTEXT_EVENTS["test"]
    assert a.join_guidance(p, "#build") == (
        'scripted test agent: call read("#build") or wait("#build", 50); mid-task items can also arrive'
        " as synthetic hook context."
    )
    r = a.route(p, WAKE, SINK, 0.0)
    assert (r.kind, r.path, r.sink_id) == ("sink", "wait", 41)
    assert a.route(p, PRIO, None, 0.0).kind == "pull"
    r = a.route(p, WAKE, None, 0.0)
    assert (r.kind, r.reason) == ("none", "not waiting")


# ----------------------------------------------------------------- cursor
def cursor_part(**kw: Any) -> Participant:
    return part(**{"harness": "cursor", "session_key": "cursor:conv-1", **kw})


def test_cursor_routes_an_open_wait_to_its_sink_even_unbound() -> None:
    a = CursorAdapter(CFG)
    pending = cursor_part(bind_state="pending", session_key="cursor:agent:123@1.00")
    r = a.route(pending, WAKE, SINK, 0.0)
    assert (r.kind, r.sink_id) == ("sink", 41)
    assert a.route(pending, WAKE, None, 0.0).reason == UNBOUND_WHY
    assert 'wait("#build", 50)' in a.join_guidance(pending, "#build")


def test_cursor_park_needs_a_completed_stop_and_enough_hook_budget() -> None:
    a = CursorAdapter(CFG)
    p = cursor_part()
    assert a.park_s(p, ev("stop", "cursor", status="completed", max_wait_s=None)) is None
    assert a.park_s(p, ev("stop", "cursor", status="aborted", max_wait_s=630.0)) is None
    assert a.park_s(p, ev("stop", "cursor", status="completed", max_wait_s=630.0)) == 600.0
    # the park ends PARK_MARGIN_S before the hook's own budget; under a second is no park
    assert a.park_s(p, ev("stop", "cursor", status="completed", max_wait_s=60.0)) == 30.0
    assert a.park_s(p, ev("stop", "cursor", status="completed", max_wait_s=30.5)) is None


def test_cursor_follow_up_verdicts_for_every_kind_of_hook() -> None:
    a = CursorAdapter(CFG)
    p = cursor_part()
    v = a.continue_verdict
    assert v(p, 0, True, ev("beforeSubmitPrompt", "cursor")) == "human_prompt"
    assert v(p, 0, True, ev("sessionEnd", "cursor")) == "session_end"
    assert v(p, 0, True, ev("sessionStart", "cursor")) == "session_end"
    assert v(p, 0, True, ev("stop", "cursor", loop_count=1)) == "confirm"
    assert v(p, 0, False, ev("stop", "cursor", loop_count=1)) is None
    assert v(p, 0, True, ev("stop", "cursor", loop_count=0)) == "loop_reset"
    assert v(p, None, True, ev("stop", "cursor", loop_count=1)) == "loop_reset"
    assert v(p, 0, True, ev("postToolUseFailure", "cursor")) == "confirm"
    assert v(p, 0, False, ev("postToolUse", "cursor")) is None
    # a hook the follow-up protocol doesn't know says nothing either way
    assert v(p, 0, True, ev("preCompact", "cursor")) is None
    assert a.confirm_window_s() == FOLLOWUP_CONFIRM_S
    assert a.redeliver_on_stop(p, ev("stop", "cursor", status="error")) is False


def test_cursor_tiers_and_routes_once_bound() -> None:
    a = CursorAdapter(CFG)
    p = cursor_part()
    assert a.tier(None) == ("mcp-only", "binding")
    assert a.tier(p) == ("cursor:stop-park", "provisional")
    assert a.context_events(p) == HOOK_CONTEXT_EVENTS["cursor"]
    assert a.route(p, PRIO, None, 0.0).kind == "pull"
    assert "not parked" in (a.route(p, WAKE, None, 0.0).reason or "")
    missed = cursor_part(unconfirmed_followups=CFG.cursor.max_unconfirmed_followups)
    assert a.tier(missed) == ("cursor:stop-park", "provisional, degraded")
    assert "degraded" in (a.route(missed, WAKE, None, 0.0).reason or "")
    assert a.park_s(missed, ev("stop", "cursor", status="completed", max_wait_s=630.0)) is None


def test_cursor_is_never_degraded_when_the_miss_limit_is_off() -> None:
    off = CFG.replace(cursor=dataclasses.replace(CFG.cursor, max_unconfirmed_followups=0))
    a = CursorAdapter(off)
    p = cursor_part(unconfirmed_followups=99)
    assert a.degraded(p) is False and a.tier(p) == ("cursor:stop-park", "provisional")


# ------------------------------------------------------------------ devin
def devin_part(**kw: Any) -> Participant:
    return part(**{"harness": "devin", "session_key": "devin:s1", **kw})


def test_devin_routes_a_sink_first_then_the_taint_then_the_pull() -> None:
    a = DevinAdapter(CFG)
    p = devin_part()
    tainted = devin_part(gen_tainted=True)
    assert (a.route(tainted, WAKE, SINK, 0.0).kind, a.route(tainted, WAKE, SINK, 0.0).sink_id) == ("sink", 41)
    assert a.route(tainted, PRIO, None, 0.0).reason == TAINT_WHY
    assert a.route(p, PRIO, None, 0.0).kind == "pull"
    r = a.route(p, WAKE, None, 0.0)
    assert r.kind == "none" and "re-arm is used up" in (r.reason or "")
    assert a.context_events(tainted) == frozenset() and a.redeliver_on_stop(tainted, ev("Stop")) is False
    assert a.stop_continues(p) is True
    assert a.tier(p) == ("devin:wait-loop", None) and a.context_events(p) == HOOK_CONTEXT_EVENTS["devin"]
    assert 'wait("#build", 600)' in a.join_guidance(p, "#build")
    assert rearm_text("#build", 600).startswith("[switchboard] (from switchboard, not your user)")


def test_devin_pull_confirmation_needs_the_wait_calls_own_successful_post() -> None:
    a = DevinAdapter(CFG)
    ok = ev("PostToolUse", "devin", tool=WAIT_TOOL, ok=True, tool_use_id="tu-1")
    # anything but a wait() answer is confirmed by its token alone
    assert a.pull_confirms("read", "tu-1", ev("Stop", "devin")) is True
    assert a.pull_confirms("wait", "tu-1", ok) is True
    assert a.pull_confirms("wait", None, ok) is True
    assert a.pull_confirms("wait", "tu-2", ok) is False
    assert a.pull_confirms("wait", "tu-1", ev("PreToolUse", "devin", tool=WAIT_TOOL, ok=True)) is False
    assert (
        a.pull_confirms("wait", "tu-1", ev("PostToolUse", "devin", tool="mcp__switchboard__read", ok=True))
        is False
    )
    assert a.pull_confirms("wait", "tu-1", ev("PostToolUse", "devin", tool=WAIT_TOOL, ok=False)) is False


def test_devin_wait_pre_and_orphaned_waits() -> None:
    a = DevinAdapter(CFG)
    p = devin_part()
    assert a.is_wait_pre(ev("PreToolUse", "devin", tool=WAIT_TOOL, tool_use_id="tu-1")) is True
    assert a.is_wait_pre(ev("PreToolUse", "devin", tool=WAIT_TOOL)) is False
    assert a.closes_waits(p, ev("PostToolUse", "devin", tool=WAIT_TOOL)) is False
    assert a.closes_waits(p, ev("PostToolUse", "devin", tool="Bash")) is True
    assert a.closes_waits(p, ev("Stop", "devin")) is True


def test_devin_block_verdicts() -> None:
    a = DevinAdapter(CFG)
    p = devin_part()
    assert a.continue_verdict(p, None, True, ev("UserPromptSubmit", "devin")) == "new_prompt"
    assert a.continue_verdict(p, None, True, ev("SessionEnd", "devin")) == "session_end"
    assert a.continue_verdict(p, None, True, ev("SessionStart", "devin")) == "session_end"
    assert a.continue_verdict(p, None, True, ev("PostToolUse", "devin")) == "confirm"
    assert a.continue_verdict(p, None, False, ev("PostToolUse", "devin")) is None
    assert a.confirm_window_s() == BLOCK_CONFIRM_S
