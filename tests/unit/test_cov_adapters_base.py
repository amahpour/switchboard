"""The adapter interface's defaults and the adapters of sessions over HTTPS, called directly
(DESIGN.md §9.1, §9.5, §9.6, §49.4): the base ``Adapter`` answers every harness question with its
conservative default, ``PullAdapter`` routes an unknown harness to a sink, a pull or nowhere, and the
Claude Code, Codex, Cursor, Devin and test-agent verdicts for the events their engine-level tests
don't reach."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest

from switchboard.adapters import (
    HTTPS_CLAUDE,
    HTTPS_CODEX,
    HTTPS_CURSOR,
    HTTPS_DEVIN,
    adapter_for,
    build_adapters,
)
from switchboard.adapters.base import HOOK_CONTEXT_EVENTS, INLINE_PATHS, Adapter, Caps, PullAdapter
from switchboard.adapters.https import (
    CLAUDE_TIER,
    CODEX_TIER,
    CURSOR_TIER,
    DEVIN_TIER,
    DEVIN_TOOL_PREFIX,
    DEVIN_WAIT_CAP_S,
    HttpsClaudeAdapter,
    HttpsCodexAdapter,
    HttpsCursorAdapter,
    HttpsDevinAdapter,
    rearm_text,
)
from switchboard.adapters.testagent import ACK_MODES, TestAgentAdapter
from switchboard.config import Config
from switchboard.models import HookEvent, Participant, Release

CFG = Config(human_name="alice")
WAKE = Release(items=(), kind="wake", counted=True, reason="human")
PRIO = Release(items=(), kind="priority", counted=False, reason="human")
SINK = SimpleNamespace(path="wait", id=41)
WAIT_TOOL = f"{DEVIN_TOOL_PREFIX}wait"


def part(**kw: Any) -> Participant:
    """A participant row with neutral defaults (no store needed: routing is pure)."""
    base: dict[str, Any] = {f.name: None for f in dataclasses.fields(Participant)}
    base.update(
        id=7,
        harness="unknown",
        session_key="unknown@laptop:s1",
        host="laptop",
        status="idle",
        approval_mode="prompting",
        boundary_seq=0,
        gen_tainted=False,
        rearms_in_gen=0,
        created_at=1.0,
    )
    base.update(kw)
    return Participant(**base)


def ev(event: str, harness: str = "test", **kw: Any) -> HookEvent:
    return HookEvent(harness=harness, event=event, **kw)


# ------------------------------------------------------------------ base
def test_the_base_adapter_defaults_are_the_conservative_answers() -> None:
    a = Adapter(CFG)
    p = part()
    assert a.harness == "unknown"
    # an unknown harness: the delivery batch limit and a 50 s wait cap
    assert a.caps(p) == Caps(ctx_max_chars=CFG.delivery.batch_max_chars, wait_cap_s=50)
    assert a.caps().inline_paths == INLINE_PATHS
    assert a.tier(p) == ("mcp-only", None)
    assert a.join_conflict(object(), "t", "laptop") is None
    assert a.credential_error(p, {}) is None
    assert a.join_guidance(p, "#build") == (
        'switchboard can\'t wake this session on its own. Call read("#build") to check for messages,'
        ' or wait("#build", 50) to block until one arrives.'
    )
    assert a.context_events(p) == frozenset()
    # /catchup's session id (DESIGN.md §9.1, issue #171): a harness with no adapter of its
    # own keeps no history-tool id, whatever the row otherwise says
    assert a.history_session_id(part(session_id="s1"), CFG) == (None, "a session of an unknown harness")
    assert a.park_s(p, ev("Stop")) is None
    assert a.stop_continues(p) is False
    assert a.redeliver_on_stop(p, ev("Stop")) is True
    assert a.closes_waits(p, ev("PostToolUse")) is False
    assert a.is_wait_pre(ev("PreToolUse")) is False
    assert a.pull_confirms("wait", "tu-1", ev("PostToolUse")) is True
    assert a.continue_verdict(p, True, ev("Stop")) is None
    assert a.confirm_window_s() == 180.0
    assert a.stop_wait_lost(30.0) is None
    assert a.ends_session(ev("SessionEnd")) is True
    assert a.rearm_caps() == (0, 0)
    with pytest.raises(NotImplementedError, match="unknown has no Stop re-arm"):
        a.rearm_text("#build", 50)
    with pytest.raises(NotImplementedError):
        a.route(p, WAKE, None, 0.0)
    with pytest.raises(ValueError, match="can't be told apart"):
        a.session_key("laptop", None)


def test_caps_come_from_each_harness_config_capped_by_what_the_broker_prints() -> None:
    ads = build_adapters(CFG)
    assert ads[HTTPS_CODEX].caps() == Caps(CFG.codex.ctx_max_chars, CFG.codex.wait_cap_s)
    assert ads[HTTPS_CURSOR].caps() == Caps(CFG.cursor.ctx_max_chars, CFG.cursor.wait_cap_s)
    # Devin over HTTPS ends a wait before its client sends the call again (§49.7)
    assert ads[HTTPS_DEVIN].caps() == Caps(
        CFG.devin.ctx_max_chars, min(CFG.devin.wait_cap_s, DEVIN_WAIT_CAP_S)
    )
    # a config over the broker's own print limit is cut down to it (a batch is never cut after the fact)
    big = CFG.replace(cursor=dataclasses.replace(CFG.cursor, ctx_max_chars=50_000))
    assert HttpsCursorAdapter(big).caps().ctx_max_chars == 8000


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


# ------------------------------------------------------------- test agent
def test_the_scripted_test_agent_adapter() -> None:
    a = TestAgentAdapter(CFG)
    p = part(harness="test", session_key="test:bot", session_id="k1", host="")
    assert ACK_MODES == ("next_call", "immediate", "never")
    # keyed by the test session header, not by a process (§9.6)
    assert a.session_key("", None, test_session="bot") == "test:bot"
    assert a.session_key("fpga-pi", None, test_session="bot") == "test@fpga-pi:bot"
    with pytest.raises(ValueError, match="needs a session key"):
        a.session_key("", None, test_session="bad key")
    with pytest.raises(ValueError, match="needs a session key"):
        a.session_key("", None)
    assert a.tier(p) == ("mcp-only", None)
    assert a.context_events(p) == HOOK_CONTEXT_EVENTS["test"]
    assert [a.pull_ack(p, m) for m in (None, "immediate", "never", "odd")] == [
        "next_call",
        "immediate",
        "never",
        "next_call",
    ]
    # /catchup never gets a test session's id, whatever the row says (issue #171)
    assert a.history_session_id(p, CFG) == (None, "a test session")
    assert a.join_guidance(p, "#build") == (
        'scripted test agent: call read("#build") or wait("#build", 50); mid-task items can also arrive'
        " as synthetic hook context."
    )
    r = a.route(p, WAKE, SINK, 0.0)
    assert (r.kind, r.path, r.sink_id) == ("sink", "wait", 41)
    assert a.route(p, PRIO, None, 0.0).kind == "pull"
    r = a.route(p, WAKE, None, 0.0)
    assert (r.kind, r.reason) == ("none", "not waiting")


# -------------------------------------------- Claude Code, Codex, Cursor: the Stop hook that waits
@pytest.mark.parametrize(
    ("cls", "harness", "tier"),
    [
        (HttpsClaudeAdapter, "claude", CLAUDE_TIER),
        (HttpsCodexAdapter, "codex", CODEX_TIER),
        (HttpsCursorAdapter, "cursor", CURSOR_TIER),
    ],
)
def test_a_listening_adapters_routes_tier_and_park(cls: type, harness: str, tier: str) -> None:
    a = cls(CFG)
    p = part(harness=harness)
    assert a.tier(p) == (tier, "over HTTPS: woken at stop")
    assert a.context_events(p) == HOOK_CONTEXT_EVENTS[harness]
    r = a.route(p, WAKE, SINK, 0.0)
    assert (r.kind, r.sink_id) == ("sink", 41)
    assert a.route(p, PRIO, None, 0.0).kind == "pull"
    r = a.route(p, WAKE, None, 0.0)
    assert r.kind == "none" and "no Stop hook waiting" in (r.reason or "")
    assert "Stop hook waits on switchboard" in a.join_guidance(p, "#build")
    # a Stop that says how long it may wait parks that long less a margin, up to about a day; one that
    # doesn't (a status report) or has less than the margin answers at once
    assert a.park_s(p, ev("Stop", harness)) is None
    assert a.park_s(p, ev("Stop", harness, max_wait_s=630.0)) == 625.0
    assert a.park_s(p, ev("Stop", harness, max_wait_s=10_000_000.0)) == 86400.0
    assert a.park_s(p, ev("Stop", harness, max_wait_s=9.0)) is None
    assert a.park_s(p, ev("PostToolUse", harness, max_wait_s=630.0)) is None


def test_session_keys_come_from_what_each_harnesss_calls_name() -> None:
    assert HttpsClaudeAdapter(CFG).session_key("laptop", "sess-1") == "claude@laptop:sess-1"
    assert HttpsCursorAdapter(CFG).session_key("laptop", "conv-1") == "cursor@laptop:conv-1"
    assert HttpsCodexAdapter(CFG).session_key("laptop", "thr-1") == "codex@laptop:thr-1"
    assert HttpsDevinAdapter(CFG).session_key("laptop", "slug-1") == "devin@laptop:slug-1"
    for a in (
        HttpsClaudeAdapter(CFG),
        HttpsCursorAdapter(CFG),
        HttpsCodexAdapter(CFG),
        HttpsDevinAdapter(CFG),
    ):
        with pytest.raises(ValueError):
            a.session_key("laptop", None)


def test_the_stop_continuation_verdicts_of_claude_code_and_cursor() -> None:
    for a in (HttpsClaudeAdapter(CFG), HttpsCursorAdapter(CFG)):
        p = part(harness=a.harness)
        v = a.continue_verdict
        # a follow-up its hook printed (acked) is confirmed by the turn it starts or continues
        assert v(p, True, ev("UserPromptSubmit", a.harness)) == "confirm"
        assert v(p, False, ev("UserPromptSubmit", a.harness)) is None
        for event in ("PostToolUse", "PostToolUseFailure", "Stop"):
            assert v(p, True, ev(event, a.harness)) == "confirm" and v(p, False, ev(event, a.harness)) is None
        for event in ("SessionEnd", "SessionStart"):
            assert v(p, True, ev(event, a.harness)) == "session_end"
        assert v(p, True, ev("PreCompact", a.harness)) is None  # says nothing either way
        assert a.confirm_window_s() == 180.0
    # /clear keeps a Claude Code session (the same id goes on), any other end takes it offline
    claude = HttpsClaudeAdapter(CFG)
    assert claude.ends_session(ev("SessionEnd", "claude", reason="clear")) is False
    assert claude.ends_session(ev("SessionEnd", "claude", reason="logout")) is True
    assert "named no session" in _error(lambda: claude.session_key("laptop", None))


def _error(fn: Any) -> str:
    with pytest.raises(ValueError) as e:
        fn()
    return str(e.value)


def test_codex_over_https_a_prompt_before_the_followup_ran_was_the_person() -> None:
    a = HttpsCodexAdapter(CFG)
    p = part(harness="codex", session_key="codex@laptop:t1", gen="t1")
    # a follow-up's turn goes on under the parent turn id: a new prompt means the person pressed Esc
    assert a.continue_verdict(p, True, ev("UserPromptSubmit", "codex")) == "human_prompt"
    assert a.continue_verdict(p, True, ev("PostToolUse", "codex")) == "confirm"
    # a subagent's tool hook carries the session's id with another turn's: another thread's
    assert a.other_thread(p, ev("PostToolUse", "codex", gen="sub")) is True
    assert a.other_thread(p, ev("PostToolUse", "codex", gen="t1")) is False
    assert a.other_thread(p, ev("PostToolUse", "codex")) is False
    assert a.other_thread(part(harness="codex"), ev("PostToolUse", "codex", gen="sub")) is False
    assert a.other_thread(p, ev("Stop", "codex", gen="sub")) is False
    # a thread's calls must name it; another machine's session of the same thread conflicts
    assert "threadId" in _error(lambda: a.session_key("laptop", None))
    assert a.credential_error(p, {"thread_id": "t1"}) is None
    assert "another Codex thread" in (a.credential_error(p, {"thread_id": "t2"}) or "")
    assert "another Codex thread" in (a.credential_error(p, {}) or "")
    assert "not listening" in (a.stop_wait_lost(301.0) or "") and "5 min" in (a.stop_wait_lost(301.0) or "")


def test_codex_anchors_its_turn_at_the_first_switchboard_tool_hook() -> None:
    a = HttpsCodexAdapter(CFG)
    p = part(harness="codex")
    assert a.anchors_turn(p, ev("PostToolUse", "codex", tool="mcp__switchboard__join")) is True
    assert a.anchors_turn(p, ev("PostToolUse", "codex", tool="Bash")) is False
    assert a.anchors_turn(p, ev("Stop", "codex", tool="mcp__switchboard__join")) is False


# ------------------------------------------------------------------ devin
def devin_part(**kw: Any) -> Participant:
    return part(**{"harness": "devin", "session_key": "devin@laptop:s1", **kw})


def test_devin_routes_a_sink_first_then_the_taint_then_the_pull() -> None:
    a = HttpsDevinAdapter(CFG)
    p = devin_part()
    tainted = devin_part(gen_tainted=True)
    assert (a.route(tainted, WAKE, SINK, 0.0).kind, a.route(tainted, WAKE, SINK, 0.0).sink_id) == ("sink", 41)
    assert a.route(tainted, PRIO, None, 0.0).reason == a.taint_why
    assert a.route(p, PRIO, None, 0.0).kind == "pull"
    r = a.route(p, WAKE, None, 0.0)
    assert r.kind == "none" and "re-arm is used up" in (r.reason or "")
    assert a.context_events(tainted) == frozenset() and a.redeliver_on_stop(tainted, ev("Stop")) is False
    assert a.stop_continues(p) is True and a.wait_drop_means_gone() is True
    assert a.tier(p) == (DEVIN_TIER, "over HTTPS: listening through wait()")
    assert a.context_events(p) == HOOK_CONTEXT_EVENTS["devin"]
    assert f'wait("#build", {DEVIN_WAIT_CAP_S})' in a.join_guidance(p, "#build")
    assert rearm_text("#build", 600).startswith("[switchboard] (from switchboard, not your user)")
    assert a.rearm_text("#build", 600) == rearm_text("#build", 600)
    assert a.starts_subagent(ev("PreToolUse", "devin", subagent_bg=True)) is True
    assert a.starts_subagent(ev("PostToolUse", "devin", subagent_bg=True)) is False


def test_devin_pull_confirmation_needs_the_wait_calls_own_successful_post() -> None:
    a = HttpsDevinAdapter(CFG)
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
    a = HttpsDevinAdapter(CFG)
    p = devin_part()
    assert a.is_wait_pre(ev("PreToolUse", "devin", tool=WAIT_TOOL, tool_use_id="tu-1")) is True
    assert a.is_wait_pre(ev("PreToolUse", "devin", tool=WAIT_TOOL)) is False
    assert a.closes_waits(p, ev("PostToolUse", "devin", tool=WAIT_TOOL)) is False
    # a tool of someone else's server finishing says nothing about the wait (#493: the plugin reports
    # every tool, and a background subagent's carry the main agent's session)
    for other in ("Bash", "exec", "mcp__github__create_issue", None):
        assert a.closes_waits(p, ev("PostToolUse", "devin", tool=other)) is False, other
    # but anything else the session does ends it: a call of switchboard's own, a tool about to run, a turn
    assert a.closes_waits(p, ev("PostToolUse", "devin", tool=f"{DEVIN_TOOL_PREFIX}say")) is True
    assert a.closes_waits(p, ev("PreToolUse", "devin", tool="Bash")) is True
    assert a.closes_waits(p, ev("PreToolUse", "devin", tool=f"{DEVIN_TOOL_PREFIX}say")) is True
    assert a.closes_waits(p, ev("Stop", "devin")) is True
    assert a.closes_waits(p, ev("UserPromptSubmit", "devin")) is True


def test_devin_block_verdicts() -> None:
    a = HttpsDevinAdapter(CFG)
    p = devin_part()
    assert a.continue_verdict(p, True, ev("UserPromptSubmit", "devin")) == "new_prompt"
    assert a.continue_verdict(p, True, ev("SessionEnd", "devin")) == "session_end"
    assert a.continue_verdict(p, True, ev("SessionStart", "devin")) == "session_end"
    assert a.continue_verdict(p, True, ev("PostToolUse", "devin")) == "confirm"
    assert a.continue_verdict(p, False, ev("PostToolUse", "devin")) is None
    assert a.confirm_window_s() == a.block_confirm_s


def test_devin_history_session_id_is_whatever_its_hooks_report() -> None:
    """HttpsDevinAdapter.history_session_id (DESIGN.md §9.1, issue #171): /catchup's session id
    for a Devin session is its own slug, with no quirk of its own."""
    a = HttpsDevinAdapter(CFG)
    assert a.history_session_id(devin_part(session_id="brisk-otter-1"), CFG) == ("brisk-otter-1", "")
    assert a.history_session_id(devin_part(session_id=None), CFG) == (None, "no devin session id known yet")


def test_the_engine_builds_one_adapter_per_https_harness_and_the_test_agent() -> None:
    assert set(build_adapters(CFG)) == {
        HTTPS_CLAUDE,
        HTTPS_CODEX,
        HTTPS_CURSOR,
        HTTPS_DEVIN,
        "unknown",
        "test",
    }


def test_a_session_is_served_by_the_adapter_of_its_harness() -> None:
    """Each harness has its ``@https`` adapter, the scripted test agent its own, and a harness with no
    adapter the ``unknown`` one. Fails if a harness gets another's rules (a Codex wake for a Claude
    session) or the test agent gets a real harness's."""
    ads = build_adapters(CFG)
    assert adapter_for(ads, "claude") is ads[HTTPS_CLAUDE]
    assert adapter_for(ads, "devin") is ads[HTTPS_DEVIN]
    assert adapter_for(ads, "test") is ads["test"]
    assert adapter_for(ads, "gemini") is ads["unknown"]


@pytest.mark.parametrize("harness", ["claude", "codex", "cursor"])
def test_catchups_session_id_is_the_one_the_sessions_key_carries(harness: str) -> None:
    """/catchup (§26) names a session by the harness's own id: for a session over HTTPS, the id its
    calls name, which is in its session key. Fails if a session's row and key disagree and the id is
    passed on anyway."""
    a = build_adapters(CFG)[f"{harness}@https"]
    key = f"{harness}@laptop:id-1"
    assert a.history_session_id(part(harness=harness, session_key=key, session_id="id-1"), CFG) == (
        "id-1",
        "",
    )
    why = f"no {harness} session id known yet"
    assert a.history_session_id(part(harness=harness, session_key=key, session_id=None), CFG) == (None, why)
    assert a.history_session_id(part(harness=harness, session_key=key, session_id="id-2"), CFG) == (None, why)
    assert a.history_session_id(part(harness=harness, session_key="nonsense", session_id="id-1"), CFG) == (
        None,
        why,
    )
