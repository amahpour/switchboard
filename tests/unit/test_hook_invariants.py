"""A hook can only be answered with the §7.3 shapes, whatever the engine hands the broker (DESIGN.md
§7.3, §11): the table in ``broker/hook_replies.py`` is the only place a reply is written, so the
invariants of the hook script it replaced are checked on it, event by event and over fuzzed replies."""

from __future__ import annotations

import json
import random
import re
from typing import Any

import pytest

from switchboard.broker import hook_replies as hr

HARNESSES = ("claude", "codex", "cursor", "devin")
# the events a harness registers (the plugins' hooks, §49.5) and ones it must never be answered on
EVENTS = sorted(
    {e for evs in hr.CONTEXT_EVENTS.values() for e in evs}
    | {e for evs in hr.CONTINUE_EVENTS.values() for e in evs}
    | {
        "Stop",
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
            return event in hr.CONTEXT_EVENTS["cursor"]
        if set(printed) == {"followup_message"}:
            return event == "stop"
        return False
    if printed == ctx and isinstance(ctx["hookSpecificOutput"]["additionalContext"], str):
        return event in hr.CONTEXT_EVENTS[harness]
    if printed.keys() == {"decision", "reason"} and printed["decision"] == "block" and event == "Stop":
        return harness in ("devin", "codex")  # Devin's continue, and Codex's follow-up (the listener's)
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
        yield out


@pytest.mark.parametrize("harness", HARNESSES)
def test_every_event_and_fuzzed_reply_answers_nothing_or_an_allowed_shape(harness: str) -> None:
    """For every event, including ones no harness registers, and replies of every shape (wrong kinds,
    missing or oversized text, extra keys that look like permission decisions), the answer is nothing
    or one of the table's shapes: context only on the events the table lists, a ``decision`` only as a
    ``block`` on a Stop, no permission, tool-input rewrite or pre-event answer. Fails if a reply can
    carry anything the table doesn't allow, or reach an event it doesn't list."""
    rnd = random.Random(sum(map(ord, harness)))
    for event in EVENTS:
        for out in replies(rnd):
            printed = hr.reply_for(harness, event, out)
            if printed is None:
                continue
            assert allowed(harness, event, printed), (harness, event, out, printed)
            flat = json.dumps(printed)
            for k in FORBIDDEN_KEYS - {"decision"}:
                assert f'"{k}"' not in flat
            if '"decision"' in flat:
                assert harness in ("devin", "codex") and event == "Stop" and printed["decision"] == "block"
            ev = event.lower()
            assert not re.search(r"pre|permission|notification|interrupt|end", ev), (harness, event)
            if "submit" in ev:
                assert harness == "claude" and event == "UserPromptSubmit"


def test_render_table_directly() -> None:
    assert hr.render("claude", "PostToolUse", {"kind": "context", "text": "x"}) == {
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "x"}
    }
    assert hr.render("claude", "Stop", {"kind": "continue", "text": "x"}) is None
    assert hr.render("codex", "UserPromptSubmit", {"kind": "context", "text": "x"}) is None
    assert hr.render("devin", "Stop", {"kind": "continue", "text": "x"}) == {
        "decision": "block",
        "reason": "x",
    }
    assert hr.render("devin", "UserPromptSubmit", {"kind": "context", "text": "x"}) is None
    assert hr.render("cursor", "stop", {"kind": "continue", "text": "x"}) == {"followup_message": "x"}
    assert hr.render("cursor", "sessionStart", {"kind": "context", "text": "x"}) is None
    # at the limit: printed whole; over it: nothing (never a cut batch that gets acked)
    assert (
        len(hr.render("cursor", "postToolUse", {"kind": "context", "text": "y" * 8000})["additional_context"])
        == 8000
    )
    assert hr.render("cursor", "postToolUse", {"kind": "context", "text": "y" * 8001}) is None
    assert (
        len(
            hr.render("codex", "PostToolUse", {"kind": "context", "text": "y" * 5000})["hookSpecificOutput"][
                "additionalContext"
            ]
        )
        == 5000
    )
    for h, n in hr.CONTEXT_MAX.items():
        ev = "postToolUse" if h == "cursor" else "PostToolUse"
        assert hr.render(h, ev, {"kind": "context", "text": "y" * (n + 1)}) is None


def test_the_table_has_no_event_that_could_approve_or_decide() -> None:
    """No event of the table is a permission request or a notification, and a continue is only ever
    answered on a Stop (§7.3, §11)."""
    for evs in (*hr.CONTEXT_EVENTS.values(), *hr.CONTINUE_EVENTS.values()):
        assert not any("permission" in e.lower() or "notification" in e.lower() for e in evs)
    for evs in hr.CONTINUE_EVENTS.values():
        assert all(e.lower() == "stop" for e in evs)


def test_the_relayed_fields_are_cut_and_checked() -> None:
    assert hr.clip("a" * 200, 32) == "a" * 32 and hr.clip("", 32) is None and hr.clip(5) is None
    assert hr.model_name("claude-opus-4@20250514") == "claude-opus-4@20250514"
    for bad in ("a@b.example.com", "x" * 65, "", None, 7, "has space"):
        assert hr.model_name(bad) is None, bad
    assert [m.groups() for m in hr.TOKEN_RE.finditer("see yk:b12.0a1b2c3d and yk:b3.ffffffff")] == [
        ("12", "0a1b2c3d"),
        ("3", "ffffffff"),
    ]
