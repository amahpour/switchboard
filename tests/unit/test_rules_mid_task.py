"""Mid-task delivery (DESIGN.md §8.2, §9): while an agent is
working, only priority messages (the human's, and @mentions of it) are delivered,
and only through hooks, a Codex steer or (bypass Claude) the inbox socket. Never
chatter, never keystrokes, and never counted against the wake budget."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import FakeClock
from engine_world import World
from test_claude_adapter import claude
from test_codex_adapter import TID, attach, codex
from test_cursor_adapter import cursor
from test_devin_adapter import devin

from switchboard.config import Config
from switchboard.models import Push


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
        ("codex", "PostToolUse", {"ok": True, "sid": TID, "tool": "Bash"}),
        ("cursor", "postToolUse", {"ok": True}),
        ("cursor", "postToolUseFailure", {"ok": False}),
        ("devin", "PostToolUse", {"ok": True, "tool": "read"}),
    ],
)
def test_priority_rides_the_next_hook_and_chatter_never_does(
    w: World, harness: str, event: str, kw: dict
) -> None:
    if harness == "claude":
        p, m, _c = claude(w, status="busy", attached=False, registry=None)
    elif harness == "codex":
        p, m = codex(w, status="busy", proof=False)  # no steer path: PostToolUse context
        attach(w, view="busy")
    elif harness == "cursor":
        p, m = cursor(w, status="busy")
    else:
        p, m = devin(w)
    _pp, peer = w.agent("peer")
    chat = w.agent_says(peer, "chatter")
    ment = w.agent_says(peer, f"@{m.screen_name} a question", mentions=(m.screen_name,))
    hum = w.human("from alice")
    assert not [a for a in w.take() if isinstance(a, Push)]
    out = w.hook(p, event, **kw)
    assert out is not None and out.kind == "context"
    assert ids(out.text) == [hum.id, ment.id] and chat.id not in ids(out.text)
    b = w.store.get_batch(out.batch_id)
    assert b.kind == "priority" and not b.budget_counted and b.path in ("hook_ctx", "hook_ups")
    assert w.delivery(m, chat)["state"] == "pending"


def test_codex_mid_task_is_a_steer_and_bypass_claude_is_the_inbox(w: World) -> None:
    px, mx = codex(w, status="busy")
    attach(w, view="busy")
    pc, mc, _conn = claude(w, "claude-2", status="busy", mode="bypass", registry="busy")
    _pp, peer = w.agent("peer")
    w.agent_says(peer, "chatter only")
    assert not [a for a in w.take() if isinstance(a, Push)]
    w.human("stop and add a test")
    got = {a.path: a for a in w.take() if isinstance(a, Push)}
    assert set(got) == {"steer", "inbox"}
    for push in got.values():
        b = w.store.get_batch(push.batch_id)
        assert b.kind == "priority" and not b.budget_counted and "chatter only" not in push.text


def test_a_prompting_claude_gets_hook_context_not_the_inbox_mid_task(w: World) -> None:
    p, m, _conn = claude(w, status="busy", mode="prompting", registry="busy")
    msg = w.human("mid-task")
    assert not [a for a in w.take() if isinstance(a, Push)]  # never a frame that could straddle a prompt
    out = w.hook(p, "PostToolUse", ok=True)
    assert out is not None and f"id={msg.id} " in out.text


def test_waiting_on_an_approval_prompt_gets_nothing(w: World) -> None:
    p, m, _c = claude(w, status="waiting-approval", attached=False, registry=None)
    w.human("held while the prompt is open")
    assert w.hook(p, "PostToolUse", ok=True) is None
    assert not [a for a in w.take() if isinstance(a, Push)]


def test_no_keystrokes_anywhere() -> None:
    """The product never types into a terminal: tmux appears only in the live test harness."""
    root = Path(__file__).resolve().parents[2] / "src" / "switchboard"
    for f in root.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "send-keys" not in text and "tmux" not in text.lower(), f
