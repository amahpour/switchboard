"""The denylist of strings switchboard must never emit (DESIGN.md §11).

This module is the ONLY place under ``src/switchboard`` where these strings may
appear; ``tests/unit/test_guardrails_static.py`` greps every other source file
for them. Code that builds outgoing configs, argv lists or RPC params can use
``find_forbidden()`` as a last-line runtime check.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Harness trust / inbound-acceptance switches switchboard never writes.
FORBIDDEN_CONFIG = (
    "hooks.state",
    "trusted_hash",
    '"trust": true',
    "crossSessionInbound",
)

# CLI flags switchboard never passes to any harness.
FORBIDDEN_FLAGS = (
    "--dangerously",
    "--approve-for-me",
    "--approve-mcps",
    "--trust",
)

# Codex override fields and sandbox widening keys switchboard never sends.
FORBIDDEN_CODEX = (
    "approvalPolicy",
    "sandboxPolicy",
    "network_access",
    "thread/resume",
)

# Capabilities switchboard's MCP server never declares.
FORBIDDEN_CAPABILITIES = ("claude/channel",)

# Hook output keys that could approve or rewrite a tool call.
FORBIDDEN_HOOK_OUTPUT = (
    "permissionDecision",
    "updatedInput",
    "updated_mcp_tool_output",
)

ALL_FORBIDDEN: tuple[str, ...] = (
    FORBIDDEN_CONFIG
    + FORBIDDEN_FLAGS
    + FORBIDDEN_CODEX
    + FORBIDDEN_CAPABILITIES
    + FORBIDDEN_HOOK_OUTPUT
)

# Every Codex ``turn/start`` / ``turn/steer`` / ``thread/resume`` field that
# changes a thread's settings (FINDINGS §4a; the TurnStartParams schema of
# codex-cli 0.156.1). Any of them persists on the user's session. switchboard's
# RPC client builds params from exact key allowlists; this list is a second,
# independent check on the keys (never on message text). Not in ALL_FORBIDDEN:
# words like "cwd" and "model" appear legitimately elsewhere.
CODEX_OVERRIDE_FIELDS = (
    "cwd",
    "runtimeWorkspaceRoots",
    "approvalPolicy",
    "approvalsReviewer",
    "sandboxPolicy",
    "permissions",
    "model",
    "serviceTier",
    "serviceTierForTurn",
    "effort",
    "summary",
    "personality",
    "collaborationMode",
    "environments",
    "disabledPluginIds",
    "outputSchema",
    "toolOutput",
    "turnTrigger",
    "additionalContext",
    "baseInstructions",
    "developerInstructions",
    "config",
    "modelProvider",
)

# Regexes used by the static test; they also catch spacing variants.
FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(re.escape(s)) for s in ALL_FORBIDDEN if s != '"trust": true'
) + (re.compile(r'"trust"\s*:\s*true', re.I),)


def find_forbidden(obj: Any) -> list[str]:
    """Return the forbidden strings present in ``obj`` (str, or JSON-able data)."""
    text = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return [p.pattern for p in FORBIDDEN_PATTERNS if p.search(text)]
