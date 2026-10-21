"""The engine asks a session's adapter what its hooks mean, not its harness name (#169,
DESIGN.md §9.1): which hooks set the session id and the approval mode, which SessionEnd
takes it offline, which hook starts a background subagent, how a Stop re-arms, which prompt is
input into the running turn, which hooks are a subagent's, how a pull answer is confirmed, and
whose status its sinks give. A harness added with its own adapter needs no edit to the engine.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock
from engine_world import KEY, World

from switchboard import envelope
from switchboard.adapters import HTTPS_CLAUDE, HTTPS_CODEX, HTTPS_DEVIN, PullAdapter
from switchboard.adapters.base import HOOK_CONTEXT_EVENTS
from switchboard.adapters.https import steered
from switchboard.models import HookEvent, Participant


class OtherAdapter(PullAdapter):
    """Claude's answers, on an adapter the engine's old lists never named: it stands in
    for the unknown harness (the schema takes only the harnesses it knows)."""

    reports_session_id = True
    reports_approval_mode = True

    def ends_session(self, ev: HookEvent) -> bool:
        return ev.reason != "clear"


class ListeningAdapter(PullAdapter):
    """Devin's answers, on the unknown harness: subagents that taint their prompt, and a
    Stop that re-arms once per prompt with its own text."""

    def starts_subagent(self, ev: HookEvent) -> bool:
        return ev.ev == "PreToolUse" and ev.subagent_bg

    def stop_continues(self, p: Participant) -> bool:
        return True

    def rearm_caps(self) -> tuple[int, int]:
        return 1, 5

    def rearm_text(self, room: str, wait_s: int) -> str:
        return f"listen in {room}"


class SteeredAdapter(PullAdapter):
    """Codex's answers, on the unknown harness: steered input."""

    def is_mid_turn(self, p: Participant, ev: HookEvent, was_idle: bool) -> bool:
        return steered(p, ev, was_idle)


class ScriptedAdapter(PullAdapter):
    """The test agent's answers, on the unknown harness: its status from its sinks, and
    pull answers confirmed at once."""

    sink_status = True

    def pull_ack(self, p: Participant, mode: str | None) -> str:
        return "immediate"


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock)


@pytest.fixture
def other(w: World) -> World:
    w.engine.adapters["unknown"] = OtherAdapter("unknown", w.cfg)
    return w


@pytest.fixture
def listening(w: World) -> World:
    w.engine.adapters["unknown"] = ListeningAdapter("unknown", w.cfg)
    return w


def test_an_adapter_that_reports_them_gets_its_session_id_and_approval_mode(other: World) -> None:
    """Fails if the engine keeps its own list of harnesses that report them: "unknown" is
    on no list, so its session id and approval mode would be dropped."""
    w = other
    p, _m = w.agent("bot", harness="unknown", status="idle", hooks=True)
    w.hook(p, "UserPromptSubmit", gen="g1", sid="sess-2", permission_mode="bypassPermissions")
    assert w.p(p).session_id == "sess-2"
    assert w.p(p).approval_mode == "bypass"


def test_an_adapter_that_keeps_the_session_at_a_clear_keeps_it_online(other: World) -> None:
    w = other
    p, _m = w.agent("bot", harness="unknown", status="busy", hooks=True)
    w.hook(p, "Stop")
    w.hook(p, "SessionEnd", reason="clear")
    assert w.p(p).status == "idle"
    w.hook(p, "SessionEnd", reason="logout")
    assert w.p(p).status == "offline"


def test_the_default_adapter_takes_neither_and_any_session_end_ends_it(w: World) -> None:
    """An unknown harness: its hooks' claims about a session id or an approval mode are
    not taken (it fails closed, ``unknown``), and a SessionEnd ends it whatever the reason."""
    p, _m = w.agent("bot", harness="unknown", status="busy", hooks=True)
    w.hook(p, "UserPromptSubmit", gen="g1", sid="sess-2", permission_mode="bypassPermissions")
    assert w.p(p).session_id == "sid-bot"
    assert w.p(p).approval_mode == "unknown"
    w.hook(p, "SessionEnd", reason="clear")
    assert w.p(p).status == "offline"


def test_an_adapter_whose_hook_starts_a_subagent_taints_that_prompt(listening: World) -> None:
    """Fails if the engine taints only a session it knows by name as Devin's: the prompt
    would stay clean, and a Stop that may be the subagent's would get a continue."""
    w = listening
    p, _m = w.agent("bot", harness="unknown", status="idle", hooks=True)
    w.hook(p, "UserPromptSubmit", gen="g1")
    w.hook(p, "PreToolUse", gen="g1", tool="run_subagent", subagent_bg=True)
    assert w.p(p).gen_tainted
    assert w.hook(p, "Stop", gen="g1") is None


def test_a_stop_re_arms_with_the_adapters_text_and_cap(listening: World) -> None:
    """Fails if the re-arm's limits and text come from Devin's config and module rather
    than the session's adapter: the text would be Devin's, and a second re-arm would go."""
    w = listening
    p, _m = w.agent("bot", harness="unknown", status="idle", hooks=True)
    w.hook(p, "UserPromptSubmit", gen="g1")
    out = w.hook(p, "Stop", gen="g1")
    assert out is not None and out.kind == "continue" and out.text == "listen in #build"
    w.hook(p, "PostToolUse", gen="g1", ok=True)
    assert w.hook(p, "Stop", gen="g1") is None  # one per prompt


def test_input_into_the_running_turn_is_not_a_new_turn(w: World, clock: FakeClock) -> None:
    """Fails if only a session named Codex's can have mid-turn input: the prompt would end
    the turn and take back the pull answer the agent is reading."""
    w.engine.adapters["unknown"] = SteeredAdapter("unknown", w.cfg)
    p, m = w.agent("bot", harness="unknown", status="idle", hooks=True)
    w.hook(p, "UserPromptSubmit", gen="t1")
    w.human("x")
    _t, bid, *_ = w.engine.pull(w.p(p), w.m(m), "read", 20)
    clock.advance(1)
    w.hook(p, "UserPromptSubmit", gen="t1")  # steered into turn t1
    assert w.store.get_batch(bid).state == "offered"
    w.hook(p, "UserPromptSubmit", gen="t2")  # a new turn
    assert w.store.get_batch(bid).state == "expired"


class SharedIdAdapter(PullAdapter):
    """Codex over HTTPS's answer to a subagent, on the unknown harness: a tool hook of another turn
    than the session's current one is the subagent's."""

    def context_events(self, p: Participant) -> frozenset[str]:
        return HOOK_CONTEXT_EVENTS["claude"]

    def other_thread(self, p: Participant, ev: HookEvent) -> bool:
        return (
            ev.ev in ("PostToolUse", "PostToolUseFailure")
            and bool(ev.gen)
            and bool(p.gen)
            and ev.gen != p.gen
        )


@pytest.mark.parametrize("event", ["PostToolUse", "PostToolUseFailure"])
def test_a_hook_the_adapter_says_is_another_threads_changes_nothing(
    w: World, clock: FakeClock, event: str
) -> None:
    """A subagent's tool hook carries the session's id (#446). The engine asks the adapter, and a
    hook it calls another thread's is ignored whole: no context for the subagent, no confirmation of an
    answer the session holds, no status, no ``last_seen``. The session's own next hook gets it all.
    Fails if the engine knows only the harnesses it was written for, or lets such a hook through."""
    w.engine.adapters["unknown"] = SharedIdAdapter("unknown", w.cfg)
    p, m = w.agent("bot", harness="unknown", status="idle", hooks=True)
    w.hook(p, "UserPromptSubmit", gen="t1")
    w.human("hello")
    _t, bid, *_ = w.engine.pull(w.p(p), w.m(m), "read", 20)
    w.human("@bot look at the build")
    seen = w.p(p).last_seen
    clock.advance(5)
    w.take()
    sub = w.hook(
        p, event, gen="t-sub", tool="Bash", tool_use_id="exec-1", ok=True, tokens=tokens_of(w, bid, m)
    )
    assert sub is None and w.take() == []
    assert w.store.get_batch(bid).state == "offered"  # the subagent's report confirms nothing
    assert w.p(p).last_seen == seen and w.p(p).status == "busy"
    own = w.hook(
        p,
        "PostToolUse",
        gen="t1",
        tool="mcp__x__read",
        tool_use_id="exec-2",
        ok=True,
        tokens=tokens_of(w, bid, m),
    )
    assert w.store.get_batch(bid).state == "confirmed"
    assert own is not None and "look at the build" in own.text


def test_with_no_current_turn_known_a_hook_counts_as_the_sessions_own(w: World) -> None:
    """A session that joined mid-turn has no turn yet, so nothing can tell its hooks from a subagent's:
    they count as its own, as before (#446). Fails if an unknown turn is treated as a stranger's."""
    w.engine.adapters["unknown"] = SharedIdAdapter("unknown", w.cfg)
    p, _m = w.agent("bot", harness="unknown", status="idle", hooks=True)
    assert w.p(p).gen is None
    w.human("@bot hello there")
    out = w.hook(p, "PostToolUse", gen="t1", tool="Bash", tool_use_id="exec-1", ok=True)
    assert w.p(p).status == "busy" and out is not None and "hello there" in out.text


def tokens_of(w: World, bid: int, m: object) -> tuple[tuple[int, str], ...]:
    t = envelope.TOKEN_RE.fullmatch(envelope.batch_token(KEY, bid, m.id))  # type: ignore[attr-defined]
    assert t is not None
    return ((int(t.group(1)), t.group(2)),)


def test_an_adapter_can_take_its_status_from_sinks_and_confirm_pulls_at_once(w: World) -> None:
    """Fails if only a session named the test agent's gets either: with hooks seen, its
    status would follow its hooks, and a pull answer would wait for one."""
    w.engine.adapters["unknown"] = ScriptedAdapter("unknown", w.cfg)
    p, m = w.agent("bot", harness="unknown", status="busy", hooks=True)
    w.human("x")
    _t, bid, *_ = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert w.store.get_batch(bid).state == "confirmed"
    sink, _acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 30)
    assert w.p(p).status == "idle"  # an open wait() is idle
    w.engine.sink_timeout(sink.id)
    assert w.p(p).status == "busy"  # anything else is busy


def test_each_harness_answers_as_the_engine_did_by_name(w: World) -> None:
    """The move changes nothing: the harnesses the engine named before #169 answer yes. Devin over
    HTTPS (§49.7) has the wait loop, so a test that left it out would not notice the loop quietly
    dropping out."""
    ads = w.engine.adapters
    clear = HookEvent(harness="claude", event="SessionEnd", t=0.0, reason="clear")
    assert {h for h, a in ads.items() if a.reports_session_id} == {HTTPS_DEVIN, "test"}
    # over HTTPS (§49.4) the mode comes with each hook, and /clear keeps a Claude session
    assert {h for h, a in ads.items() if a.reports_approval_mode} == {HTTPS_CLAUDE, HTTPS_CODEX}
    assert {h for h, a in ads.items() if not a.ends_session(clear)} == {HTTPS_CLAUDE}
    sub = HookEvent(harness="devin", event="PreToolUse", t=0.0, subagent_bg=True)
    assert {h for h, a in ads.items() if a.starts_subagent(sub)} == {HTTPS_DEVIN}
    assert {h for h, a in ads.items() if a.rearm_caps() != (0, 0)} == {HTTPS_DEVIN}
    dv = w.cfg.devin
    assert ads[HTTPS_DEVIN].rearm_caps() == (dv.rearm_max_per_prompt, dv.rearm_max_per_hour)
    assert {h for h, a in ads.items() if a.sink_status} == {"test"}
    p = w.p(w.agent("bot")[0])
    assert {h for h, a in ads.items() if a.pull_ack(p, None) != "hooks"} == {"test"}
    assert [ads["test"].pull_ack(p, m) for m in (None, "next_call", "immediate", "never", "odd")] == [
        "next_call",
        "next_call",
        "immediate",
        "never",
        "next_call",
    ]
