"""``switchboard uninstall claude`` (DESIGN.md §9.7).

``unplan`` removes what ``switchboard install claude`` used to write (retired in 0.38.0, #385: agents
connect over HTTPS now, DESIGN.md §49): switchboard's hooks for this home from
``~/.claude/settings.json`` (any hook version; a group, event array or ``hooks`` object the removal
empties is dropped) and, with ``claude mcp remove --scope user switchboard``, the user-scope MCP entry
that runs switchboard's MCP server for this home. The user-scope entry is read from ``~/.claude.json``
(read-only); one that doesn't run switchboard's MCP server for this home is left alone, with a note.
The command is never run with ``--user-home``, so never in tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from switchboard.install.common import (
    CommandEdit,
    Plan,
    foreign_mcp_note,
    json_removal,
    mcp_home,
    remove_hook_groups,
)


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


def unplan(user_home: Path, home: str, *, run_commands: bool = True) -> Plan:
    """``switchboard uninstall claude``: what ``switchboard install claude`` used to write, removed."""
    p = Plan("claude", verb="uninstall")
    path = user_home / ".claude" / "settings.json"
    json_removal(
        p, path, user_home, home, lambda data: remove_hook_groups(data, home, drop_empty_hooks=True)[0]
    )
    rm = ["claude", "mcp", "remove", "--scope", "user", "switchboard"]
    have = registered_entry(user_home)
    if have is not None and mcp_home(have) == home:
        p.edits.append(
            CommandEdit(
                argv=rm, display=" ".join(rm), note="" if run_commands else "not run with --user-home"
            )
        )
    else:
        p.edits.append(CommandEdit(argv=rm, display="claude mcp switchboard (user scope)", changed=False))
        if have is not None:
            p.notes.append(foreign_mcp_note("~/.claude.json (user scope)", have, "claude"))
    return p
