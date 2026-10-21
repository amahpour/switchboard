"""What a hook over HTTPS may be answered with, and what it may relay (DESIGN.md §7.3, §11, §49.4).

A harness's hook (the plugins' hooks, §49.5) reports its session to ``/mcp``, and the answer is
written here by the broker (#380's decision 5): only the shapes in the fixed table below, and ``{}``
(nothing) for everything else. The table is the same one the hook script of an earlier release
printed from (§7.3, retired in 0.38.0 with the script, #385). It has no way to answer with a
permission decision, a tool-input rewrite, or a ``decision`` on any event but a Stop; no event is
handled that a harness could use to approve something; and a batch is never cut: over the harness's
context limit the reply is nothing, and the batch is not acknowledged.

The relay side is the same allowlist the hook script applied: only a few identifier-shaped
fields of a hook's input, cut to a length, and the batch tokens (``yk:b<id>.<mac>``) found in a
switchboard tool's own result, which is how an offered batch is confirmed.

Tested by ``tests/unit/test_hook_invariants.py`` (every event and a fuzz of replies print nothing or
an allowed shape).
"""

from __future__ import annotations

import re
from typing import Any

# the batch tokens in a tool's result, and how much of a result is scanned for them
TOKEN_RE = re.compile(r"yk:b(\d{1,12})\.([0-9a-f]{8})")
FIELD_SCAN_MAX = 256 * 1024
MAX_TOKENS = 20
# the model name a hook reports (Claude SessionStart, Codex, Cursor): only a short
# identifier-shaped value is relayed, for the report (DESIGN.md §12.6). An "@" may carry a version
# (Vertex: claude-...@20250514) but never a domain, so nothing email-shaped gets through.
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}(?:@[A-Za-z0-9_:+-]{1,32})?")

# Which events may print context, per harness, in that harness's spelling (DESIGN.md §7.3).
CONTEXT_EVENTS = {
    "claude": ("SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure"),
    "codex": ("PostToolUse",),
    "devin": ("PostToolUse",),
    "cursor": ("postToolUse", "postToolUseFailure"),
}
# ...and which may print a continue: the Stop of Devin and Cursor (Claude's Stop is its listener,
# and Codex's block is written by ``reply_for``).
CONTINUE_EVENTS = {
    "devin": ("Stop",),
    "cursor": ("stop",),
}
CONTEXT_MAX = {"claude": 10000, "codex": 5000, "devin": 6000, "cursor": 8000}


def clip(value: Any, n: int = 128) -> str | None:
    """A relayed string, cut to ``n`` characters; None when it isn't a non-empty string."""
    return value[:n] if isinstance(value, str) and value else None


def model_name(value: Any) -> str | None:
    """A reported model name, only when it looks like one."""
    return value if isinstance(value, str) and len(value) <= 64 and MODEL_RE.fullmatch(value) else None


def render(harness: str, event: str, out: Any) -> dict[str, Any] | None:
    """The only shapes a hook's answer can take (DESIGN.md §7.3); None answers nothing."""
    if not isinstance(out, dict):
        return None
    kind = out.get("kind")
    text = out.get("text")
    if not isinstance(text, str) or not text:
        return None
    if kind == "context" and event in CONTEXT_EVENTS.get(harness, ()):
        if len(text) > CONTEXT_MAX.get(harness, 5000):
            # Never cut: a cut batch would be acked as if it had all been seen. Answer nothing and
            # don't ack; the broker fits batches to this limit, so this is only a safety net (the
            # offer expires, retries).
            return None
        if harness == "cursor":
            return {"additional_context": text}
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    if kind == "continue" and event in CONTINUE_EVENTS.get(harness, ()):
        if harness == "devin":
            return {"decision": "block", "reason": text}
        if harness == "cursor":
            return {"followup_message": text}
    return None


def reply_for(harness: str, event: str, out: Any) -> dict[str, Any] | None:
    """A hook's answer, written by the broker (#380's decision 5): the table's own shapes for
    context (``render``), and Codex's Stop ``decision: block`` for a follow-up. Every answer is
    checked against those exact shapes; anything else answers nothing."""
    if not isinstance(out, dict):
        return None
    text = out.get("text")
    if not isinstance(text, str) or not text:
        return None
    if out.get("kind") == "continue" and harness == "codex" and event == "Stop":
        reply: Any = {"decision": "block", "reason": text}
    else:
        reply = render(harness, event, out)
    if harness == "cursor" and reply in ({"additional_context": text}, {"followup_message": text}):
        return reply
    return reply if _shape_ok(reply, event, text) else None


def _shape_ok(reply: Any, event: str, text: str) -> bool:
    """Exactly one of the shapes a hook over HTTPS may answer with, carrying the batch's text."""
    if reply == {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}:
        return True
    return reply == {"decision": "block", "reason": text} and event == "Stop"
