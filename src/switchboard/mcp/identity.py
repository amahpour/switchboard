"""Harness detection for ``switchboard mcp`` (DESIGN.md §6.2).

``detect`` is a pure function over runtime signals. Its verdict is advisory:
the broker re-checks the MCP server's ancestry itself (§5.3). It only ever
reads *presence* of the Claude session variables and compares the socket
path with the registry file; the messaging token is never read here.

Rules, in order:
1. ``--harness test`` gives test (the broker refuses it outside test mode).
2. initialize ``clientInfo.name == "Cursor"`` gives cursor.
3. a parent argv with ``devin`` and ``acp`` gives devin.
4. a parent argv that matches codex gives codex.
5. claude needs ``CLAUDECODE == "1"``, a parent argv that matches claude, and
   ``<sessions_dir>/<ppid>.json`` whose ``messagingSocketPath`` equals
   ``$CLAUDE_CODE_MESSAGING_SOCKET``. Env alone never makes a session Claude.
6. otherwise unknown (tier mcp-only).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from switchboard.broker.peer import AGENT_MATCHERS, claude_registry_socket

CLAUDE_ENV_PREFIX = "CLAUDE_CODE_MESSAGING_"


def env_leak(env: Mapping[str, str], harness: str) -> bool:
    """A non-Claude harness whose env still carries Claude session variables
    (e.g. a Codex daemon started from a Claude shell, FINDINGS §4a)."""
    if harness == "claude":
        return False
    return "CLAUDECODE" in env or any(k.startswith(CLAUDE_ENV_PREFIX) for k in env)


def _matches(harness: str, argv: str) -> bool:
    return any(p.search(argv or "") for p in AGENT_MATCHERS[harness])


def detect(
    env: Mapping[str, str],
    client_info: Mapping[str, Any] | None,
    parent_argv: str,
    *,
    harness_flag: str | None = None,
    ppid: int | None = None,
    sessions_dir: str = "~/.claude/sessions",
) -> tuple[str, dict[str, Any]]:
    """(harness, evidence) for this MCP server process."""
    ci_name = str((client_info or {}).get("name") or "")
    argv0 = os.path.basename((parent_argv or "").split(" ", 1)[0]) if parent_argv else ""

    def result(h: str, rule: str) -> tuple[str, dict[str, Any]]:
        return h, {"rule": rule, "client": ci_name[:40], "parent": argv0[:40], "env_leak": env_leak(env, h)}

    if harness_flag == "test":
        return result("test", "flag")
    if ci_name == "Cursor":
        return result("cursor", "clientInfo")
    if _matches("devin", parent_argv):
        return result("devin", "parent:devin-acp")
    if _matches("codex", parent_argv):
        return result("codex", "parent:codex")
    if env.get("CLAUDECODE") == "1" and _matches("claude", parent_argv) and ppid:
        sock = env.get("CLAUDE_CODE_MESSAGING_SOCKET")
        reg = claude_registry_socket(sessions_dir, ppid)
        if sock and reg and sock == reg:
            return result("claude", "claude:env+parent+registry")
        return result("unknown", "claude:registry-mismatch")
    return result("unknown", "none")
