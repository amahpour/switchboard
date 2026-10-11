"""The engine asks a session's adapter what its hooks mean, not its harness name (#169,
DESIGN.md §9.1): which hooks set the session id and the approval mode, and which SessionEnd
takes it offline. A harness added with its own adapter needs no edit to the engine.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock
from engine_world import World

from switchboard.adapters import REMOTE_CODEX, PullAdapter
from switchboard.models import HookEvent


class OtherAdapter(PullAdapter):
    """Claude's answers, on an adapter the engine's old lists never named: it stands in
    for the unknown harness (the schema takes only the harnesses it knows)."""

    reports_session_id = True
    reports_approval_mode = True

    def ends_session(self, ev: HookEvent) -> bool:
        return ev.reason != "clear"


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock)


@pytest.fixture
def other(w: World) -> World:
    w.engine.adapters["unknown"] = OtherAdapter("unknown", w.cfg)
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


def test_each_harness_answers_as_the_engine_did_by_name(w: World) -> None:
    """The move changes nothing: the harnesses the engine named before #169 answer yes."""
    ads = w.engine.adapters
    clear = HookEvent(harness="claude", event="SessionEnd", t=0.0, reason="clear")
    assert {h for h, a in ads.items() if a.reports_session_id} == {"claude", "devin", "test"}
    assert {h for h, a in ads.items() if a.reports_approval_mode} == {"claude", "codex", REMOTE_CODEX}
    assert {h for h, a in ads.items() if not a.ends_session(clear)} == {"claude"}
