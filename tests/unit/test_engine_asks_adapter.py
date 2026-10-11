"""The engine asks a session's adapter what its hooks mean, not its harness name (#169,
DESIGN.md §9.1): which hooks set the session id and the approval mode, which SessionEnd
takes it offline, which hook starts a background subagent, and how a Stop re-arms. A harness
added with its own adapter needs no edit to the engine.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock
from engine_world import World

from switchboard.adapters import REMOTE_CODEX, PullAdapter
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


def test_each_harness_answers_as_the_engine_did_by_name(w: World) -> None:
    """The move changes nothing: the harnesses the engine named before #169 answer yes."""
    ads = w.engine.adapters
    clear = HookEvent(harness="claude", event="SessionEnd", t=0.0, reason="clear")
    assert {h for h, a in ads.items() if a.reports_session_id} == {"claude", "devin", "test"}
    assert {h for h, a in ads.items() if a.reports_approval_mode} == {"claude", "codex", REMOTE_CODEX}
    assert {h for h, a in ads.items() if not a.ends_session(clear)} == {"claude"}
    sub = HookEvent(harness="devin", event="PreToolUse", t=0.0, subagent_bg=True)
    assert {h for h, a in ads.items() if a.starts_subagent(sub)} == {"devin"}
    assert {h for h, a in ads.items() if a.rearm_caps() != (0, 0)} == {"devin"}
    dv = w.cfg.devin
    assert ads["devin"].rearm_caps() == (dv.rearm_max_per_prompt, dv.rearm_max_per_hour)
