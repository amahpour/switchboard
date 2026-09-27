"""``switchboard install claude`` (DESIGN.md §9.7).

- MCP server: ``claude mcp add-json --scope user switchboard <json>`` (a
  CommandEdit shown in the diff; never run with ``--user-home``, so never in
  tests). The current user-scope entry is read from ``~/.claude.json``
  (read-only): the same entry means "no changes"; a different one is removed
  with ``claude mcp remove`` first, since ``add-json`` refuses an existing name.
  An entry named switchboard that doesn't run switchboard's MCP server is
  refused (InstallError), never replaced.
- Hooks: appended to ``~/.claude/settings.json`` for SessionStart,
  UserPromptSubmit, PostToolUse, PostToolUseFailure, Stop and SessionEnd.
  PermissionRequest is never registered.
- Never written: trust, permission modes, allow rules, inbound-acceptance keys.

``switchboard uninstall claude`` (``unplan``) removes switchboard's hooks for this home
from ``~/.claude/settings.json`` (any hook version; a group, event array or
``hooks`` object the removal empties is dropped) and runs ``claude mcp remove
--scope user switchboard`` only when the registered entry runs switchboard's MCP
server for this home.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from switchboard.install.common import (
    CommandEdit,
    FileEdit,
    Plan,
    deep,
    dump_json,
    foreign_mcp_note,
    hook_command,
    json_removal,
    load_json_obj,
    mcp_argv,
    mcp_home,
    refuse_foreign_mcp,
    remove_hook_groups,
    set_hook_groups,
    tilde,
)

EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop", "SessionEnd")
TIMEOUT_S = 10


def mcp_entry(python: str, home: str) -> dict[str, Any]:
    argv = mcp_argv(python, home)
    return {"type": "stdio", "command": argv[0], "args": argv[1:]}


def hook_events(python: str, home: str, sha12: str) -> dict[str, tuple[str, int]]:
    return {e: (hook_command(python, home, sha12, "claude", e), TIMEOUT_S) for e in EVENTS}


def registered_entry(user_home: Path) -> dict[str, Any] | None:
    """The user-scope ``mcpServers.switchboard`` in ``~/.claude.json``, read-only.

    Only that one entry is looked at; nothing else in the file is kept,
    printed or written (switchboard changes it only through ``claude mcp``).
    """
    try:
        data = json.loads((user_home / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    e = servers.get("switchboard") if isinstance(servers, dict) else None
    return e if isinstance(e, dict) else None


def same_entry(have: dict[str, Any], want: dict[str, Any]) -> bool:
    return (have.get("type", "stdio") == want["type"] and have.get("command") == want["command"]
            and have.get("args") == want["args"])


def plan(user_home: Path, python: str, home: str, sha12: str, *, run_commands: bool = True) -> Plan:
    p = Plan("claude")
    want = mcp_entry(python, home)
    entry = json.dumps(want, separators=(",", ":"))
    argv = ["claude", "mcp", "add-json", "--scope", "user", "switchboard", entry]
    note = "" if run_commands else "not run with --user-home"
    have = registered_entry(user_home)
    refuse_foreign_mcp("~/.claude.json (user scope)", have)
    if have is not None and same_entry(have, want):
        p.edits.append(CommandEdit(argv=argv, display="claude mcp switchboard (user scope)", changed=False))
    else:
        if have is not None:
            # `claude mcp add-json` refuses an existing name: remove the old entry first
            rm = ["claude", "mcp", "remove", "--scope", "user", "switchboard"]
            p.edits.append(CommandEdit(argv=rm, display=" ".join(rm),
                                       note=note or "replaces an older switchboard entry"))
        p.edits.append(CommandEdit(argv=argv, display=" ".join(argv[:6]) + f" '{entry}'", note=note))
    path = user_home / ".claude" / "settings.json"
    before, data = load_json_obj(path)
    new = deep(data)
    lines = set_hook_groups(new, hook_events(python, home, sha12), home)
    # unchanged hooks: keep the file byte-for-byte (after == before, "no changes")
    after = dump_json(new) if lines else (before or "")
    p.edits.append(FileEdit(path=path, before=before, after=after, display=lines,
                            label=tilde(path, user_home)))
    p.notes.append("hooks take effect in new Claude Code sessions; they are inert until a session joins a room")
    p.notes.append("switchboard's tools may ask for approval in Claude Code (no allow rule is written)")
    return p


def unplan(user_home: Path, home: str, *, run_commands: bool = True) -> Plan:
    """``switchboard uninstall claude``: the inverse of ``plan``."""
    p = Plan("claude", verb="uninstall")
    path = user_home / ".claude" / "settings.json"
    json_removal(p, path, user_home, home,
                 lambda data: remove_hook_groups(data, home, drop_empty_hooks=True)[0])
    rm = ["claude", "mcp", "remove", "--scope", "user", "switchboard"]
    have = registered_entry(user_home)
    if have is not None and mcp_home(have) == home:
        p.edits.append(CommandEdit(argv=rm, display=" ".join(rm),
                                   note="" if run_commands else "not run with --user-home"))
    else:
        p.edits.append(CommandEdit(argv=rm, display="claude mcp switchboard (user scope)", changed=False))
        if have is not None:
            p.notes.append(foreign_mcp_note("~/.claude.json (user scope)", have, "claude"))
    return p


def print_args(python: str, home: str, sha12: str, workspace: Path | None = None) -> dict[str, Any]:
    """Per-launch flags for one ``claude`` run: only switchboard's MCP entry and hooks."""
    argv = mcp_argv(python, home)
    mcp_config = {"mcpServers": {"switchboard": {"command": argv[0], "args": argv[1:]}}}
    settings: dict[str, Any] = {}
    set_hook_groups(settings, hook_events(python, home, sha12), home)
    return {
        "argv": ["--mcp-config", json.dumps(mcp_config), "--settings", json.dumps(settings)],
        "env": {},
        "files": {},
        "notes": ["pass these flags to `claude`; nothing was written"],
    }
