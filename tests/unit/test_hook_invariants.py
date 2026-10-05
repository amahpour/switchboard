"""The hook can only print the §7.3 shapes, whatever the broker replies (DESIGN.md §7.3, §11)."""

from __future__ import annotations

import io
import json
import random
import re
from typing import Any

import pytest

from switchboard.hook import switchboard_hook as hk

EVENTS = sorted(
    {e for evs in hk.HANDLED.values() for e in evs}
    | {
        "PreToolUse",
        "PermissionRequest",
        "Notification",
        "SubagentStop",
        "preToolUse",
        "afterAgentResponse",
        "sessionStart",
        "beforeSubmitPrompt",
        "Interrupt",
        "SessionEnd",
        "sessionEnd",
        "PreCompact",
    }
)
FORBIDDEN_KEYS = {
    "permissionDecision",
    "updatedInput",
    "behavior",
    "updated_mcp_tool_output",
    "decision",
    "continue",
    "suppressOutput",
}


def allowed(harness: str, event: str, printed: dict[str, Any]) -> bool:
    ctx = {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": printed.get("hookSpecificOutput", {}).get("additionalContext"),
        }
    }
    if harness == "cursor":
        if set(printed) == {"additional_context"}:
            return event in hk.CONTEXT_EVENTS["cursor"]
        if set(printed) == {"followup_message"}:
            return event == "stop"
        return False
    if printed == ctx and isinstance(ctx["hookSpecificOutput"]["additionalContext"], str):
        return event in hk.CONTEXT_EVENTS[harness]
    if harness == "devin" and printed.keys() == {"decision", "reason"}:
        return event == "Stop" and printed["decision"] == "block"
    return False


def replies(rnd: random.Random):
    kinds = ["context", "continue", "approve", "allow", "block", None, 3, "", "deny"]
    extras = [
        {},
        {"permissionDecision": "allow"},
        {"decision": "approve"},
        {"updatedInput": {"x": 1}},
        {"behavior": "allow"},
        {"hookSpecificOutput": {"permissionDecision": "allow"}},
    ]
    for _ in range(40):
        out: Any = {"kind": rnd.choice(kinds), "text": rnd.choice(["t", "", None, "x" * 20000, 5])}
        out.update(rnd.choice(extras))
        if rnd.random() < 0.1:
            out = rnd.choice([None, "str", [1], {"kind": "context"}])
        yield {
            "out": out,
            "batch_id": rnd.choice([1, None, "x"]),
            "ack": rnd.choice(["a" * 32, None]),
            **rnd.choice(extras),
        }


@pytest.mark.parametrize("harness", sorted(hk.HANDLED))
def test_every_event_and_fuzzed_reply_prints_nothing_or_an_allowed_shape(harness, monkeypatch) -> None:
    rnd = random.Random(hash(harness) & 0xFFFF)
    for event in EVENTS:
        for reply in replies(rnd):
            monkeypatch.setattr(hk, "ask_broker", lambda *a, _r=reply, **k: (_r, None))
            out = io.StringIO()
            payload = {"hook_event_name": event, "session_id": "s"}
            if harness == "cursor":
                payload["cursor_version"] = "2026.09.23"
            env = {"DEVIN_PROJECT_DIR": "/ws"} if harness == "devin" else {}
            hk.run(
                ["--home", "/tmp/x", "--harness", harness, "--event", event],
                io.StringIO(json.dumps(payload)),
                out,
                env,
            )
            text = out.getvalue()
            if not text:
                continue
            printed = json.loads(text)
            assert allowed(harness, event, printed), (harness, event, reply, printed)
            flat = json.dumps(printed)
            for k in FORBIDDEN_KEYS - {"decision"}:
                assert f'"{k}"' not in flat
            if '"decision"' in flat:
                assert harness == "devin" and event == "Stop" and printed["decision"] == "block"
            ev = event.lower()
            assert not re.search(r"pre|permission|notification|interrupt|end", ev), (harness, event)
            if "submit" in ev:
                assert harness == "claude" and event == "UserPromptSubmit"


def test_render_table_directly() -> None:
    assert hk.render("claude", "PostToolUse", {"kind": "context", "text": "x"}) == {
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "x"}
    }
    assert hk.render("claude", "Stop", {"kind": "continue", "text": "x"}) is None
    assert hk.render("codex", "UserPromptSubmit", {"kind": "context", "text": "x"}) is None
    assert hk.render("devin", "Stop", {"kind": "continue", "text": "x"}) == {
        "decision": "block",
        "reason": "x",
    }
    assert hk.render("devin", "UserPromptSubmit", {"kind": "context", "text": "x"}) is None
    assert hk.render("cursor", "stop", {"kind": "continue", "text": "x"}) == {"followup_message": "x"}
    assert hk.render("cursor", "sessionStart", {"kind": "context", "text": "x"}) is None
    # at the limit: printed whole; over it: nothing (never a cut batch that gets acked)
    assert (
        len(hk.render("cursor", "postToolUse", {"kind": "context", "text": "y" * 8000})["additional_context"])
        == 8000
    )
    assert hk.render("cursor", "postToolUse", {"kind": "context", "text": "y" * 8001}) is None
    assert (
        len(
            hk.render("codex", "PostToolUse", {"kind": "context", "text": "y" * 5000})["hookSpecificOutput"][
                "additionalContext"
            ]
        )
        == 5000
    )
    for h, n in hk.CONTEXT_MAX.items():
        ev = "postToolUse" if h == "cursor" else "PostToolUse"
        assert hk.render(h, ev, {"kind": "context", "text": "y" * (n + 1)}) is None


def test_permission_request_is_never_handled() -> None:
    for evs in hk.HANDLED.values():
        assert not any("permission" in e.lower() for e in evs)
