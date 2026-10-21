"""Mid-task delivery (DESIGN.md §8.2, §9): while an agent is
working, only priority messages (the human's, and @mentions of it) are delivered,
and only as hook context after a tool call. Never chatter, never keystrokes, and never
counted against the wake budget."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import FakeClock
from engine_agents import fills, session
from engine_world import World

from switchboard.config import Config


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def ids(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"^- id=(\d+) ", text, re.M)]


@pytest.mark.parametrize(
    "harness,event,kw",
    [
        ("claude", "PostToolUse", {"ok": True}),
        ("claude", "PostToolUseFailure", {"ok": False}),
        ("claude", "UserPromptSubmit", {"gen": "g2"}),
        ("codex", "PostToolUse", {"ok": True, "tool": "Bash"}),
        ("cursor", "postToolUse", {"ok": True}),
        ("cursor", "postToolUseFailure", {"ok": False}),
        ("devin", "PostToolUse", {"ok": True, "tool": "read"}),
    ],
)
def test_priority_rides_the_next_hook_and_chatter_never_does(
    w: World, harness: str, event: str, kw: dict
) -> None:
    p, m = session(w, f"{harness}-1", harness, status="busy")
    _pp, peer = w.agent("peer")
    chat = w.agent_says(peer, "chatter")
    ment = w.agent_says(peer, f"@{m.screen_name} a question", mentions=(m.screen_name,))
    hum = w.human("from alice")
    assert not fills(w)
    out = w.hook(p, event, **kw)
    assert out is not None and out.kind == "context"
    assert ids(out.text) == [hum.id, ment.id] and chat.id not in ids(out.text)
    b = w.store.get_batch(out.batch_id)
    assert b.kind == "priority" and not b.budget_counted and b.path in ("hook_ctx", "hook_ups")
    assert w.delivery(m, chat)["state"] == "pending"


def test_a_busy_session_is_handed_nothing_but_a_hook_answer(w: World) -> None:
    """Nothing reaches a busy session of any harness except as the answer to its own hook (§49.4):
    a human message sits until its next tool call, whatever the approval mode. Fails if a message
    is pushed to it (a ``ResolveSink`` for a wait it never made, a park it doesn't have)."""
    sessions = [
        session(w, "codex-1", "codex", status="busy"),
        session(w, "claude-2", "claude", status="busy", mode="bypass"),
    ]
    _pp, peer = w.agent("peer")
    w.agent_says(peer, "chatter only")
    assert not fills(w)
    msg = w.human("stop and add a test")
    assert not fills(w)
    for p, _m in sessions:
        out = w.hook(p, "PostToolUse", ok=True, tool="Bash")
        assert out is not None and out.kind == "context" and f"id={msg.id} " in out.text
        assert "chatter only" not in out.text
        b = w.store.get_batch(out.batch_id)
        assert b.kind == "priority" and not b.budget_counted


def test_waiting_on_an_approval_prompt_gets_nothing(w: World) -> None:
    p, m = session(w, status="waiting-approval")
    w.human("held while the prompt is open")
    assert w.hook(p, "PostToolUse", ok=True) is None
    assert not fills(w)


def test_no_keystrokes_anywhere() -> None:
    """The product never types into a terminal: tmux appears only in the live test harness."""
    root = Path(__file__).resolve().parents[2] / "src" / "switchboard"
    for f in root.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "send-keys" not in text and "tmux" not in text.lower(), f
