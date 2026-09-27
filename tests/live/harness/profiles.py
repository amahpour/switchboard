"""Live launch profiles (DESIGN.md §12.4). Test-only; the product never emits these flags.

Claude: ``claude --model haiku --setting-sources project,local --strict-mcp-config
--permission-mode default --allowedTools <switchboard's 8 tools>`` plus the
``switchboard install claude --print-args`` flags (``--mcp-config``, ``--settings``),
with ``DISABLE_AUTOUPDATER=1`` in a clean env. The per-launch settings file also
carries the test's own permission rules (a few harmless Bash commands allowed;
the cross-session messaging tools denied, so a test session can never message
another session) and, for fixture recording, a raw-payload recorder hook.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Any

SWITCHBOARD_TOOLS = [f"mcp__switchboard__{t}" for t in ("join", "leave", "who", "say", "read", "wait", "pass", "away")]
TEST_ALLOW = ["Bash(echo *)", "Bash(sleep *)", "Bash(false)"]
TEST_DENY = ["SendMessage", "ListAgents"]
RECORD_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
                 "Stop", "SessionEnd", "Notification", "PermissionRequest")
RAWREC = Path(__file__).resolve().parent / "rawrec.py"


def recorder_command(event: str, out: Path) -> str:
    """A hook command that appends the raw payload to ``out`` and prints nothing (exit 0)."""
    inner = " ".join(shlex.quote(x) for x in (sys.executable, "-I", "-S", str(RAWREC), event, str(out)))
    return f"/bin/sh -c {shlex.quote(inner + '; exit 0')}"


def claude_settings(print_args: dict[str, Any], record_to: Path | None) -> dict[str, Any]:
    """switchboard's hooks from --print-args, plus the test profile's rules and recorder."""
    argv = print_args["argv"]
    settings = json.loads(argv[argv.index("--settings") + 1])
    settings["permissions"] = {"allow": list(TEST_ALLOW), "deny": list(TEST_DENY)}
    if record_to is not None:
        hooks = settings.setdefault("hooks", {})
        for ev in RECORD_EVENTS:
            group = {"hooks": [{"type": "command", "command": recorder_command(ev, record_to), "timeout": 5}]}
            if ev in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest"):
                group["matcher"] = "*"
            hooks.setdefault(ev, []).append(group)
    return settings


def claude_mcp_config(print_args: dict[str, Any]) -> dict[str, Any]:
    argv = print_args["argv"]
    return json.loads(argv[argv.index("--mcp-config") + 1])


def claude_argv(claude_bin: str, mcp_config: Path, settings: Path, *extra: str) -> list[str]:
    return [claude_bin, "--model", "haiku", "--setting-sources", "project,local", "--strict-mcp-config",
            "--permission-mode", "default", "--allowedTools", ",".join(SWITCHBOARD_TOOLS),
            "--mcp-config", str(mcp_config), "--settings", str(settings), *extra]


# ------------------------------------------------------------------ Devin (M5)
DEVIN_READ_CONFIG_FROM = ("agents_standard", "cursor", "windsurf", "claude", "copilot", "opencode", "zed")
# test-only extra allow: the context scenario reads three scratch files
DEVIN_TEST_ALLOW = ["read"]


def devin_config(print_args: dict[str, Any]) -> dict[str, Any]:
    """The workspace's ``.devin/config.json``: switchboard's hooks and its eight allow
    names from --print-args, plus the test's isolation (nothing imported from
    Claude/Cursor/... configs) and one harmless allow rule."""
    cfg = json.loads(print_args["files"][".devin/config.json"])
    cfg["read_config_from"] = {k: False for k in DEVIN_READ_CONFIG_FROM}
    cfg.setdefault("permissions", {}).setdefault("allow", []).extend(DEVIN_TEST_ALLOW)
    return cfg


def devin_argv(devin_bin: str) -> list[str]:
    # swe-1-6-slow (the model the test account had); workspace trust off so no trust entry is written
    return [devin_bin, "--model", "swe-1-6-slow", "--respect-workspace-trust", "false"]


# ------------------------------------------------------------------ Cursor (M5)
CURSOR_TEST_ALLOW = [f"Mcp(switchboard:{t})" for t in ("join", "leave", "who", "say", "read", "wait", "pass", "away")]


def cursor_cli_json() -> dict[str, Any]:
    """The workspace's ``.cursor/cli.json`` (test-only): switchboard's tools allowed per project."""
    return {"permissions": {"allow": list(CURSOR_TEST_ALLOW) + ["Shell(echo)", "Shell(sleep)"], "deny": []}}


def cursor_argv(agent_bin: str) -> list[str]:
    """Test-only launch flags, as in the M0 Cursor experiments: ``--model auto``
    (named models unavailable to the test account); workspace trust and the project's
    MCP server are accepted per launch for the scratch workspace only (the product
    never passes these)."""
    return [agent_bin, "--model", "auto", "--trust", "--approve-mcps"]
