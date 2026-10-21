"""``switchboard uninstall cursor`` (DESIGN.md §9.7, §9.4).

``unplan`` removes what ``switchboard install cursor`` used to write (retired in 0.38.0, #385):
``mcpServers.switchboard`` in ``~/.cursor/mcp.json`` when it runs switchboard's MCP server for this home,
and switchboard's hook entries (any version) from ``~/.cursor/hooks.json``; an event array the removal
empties is dropped, while ``version`` and the ``hooks``/``mcpServers`` objects stay (the files keep their
documented shape).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from switchboard.install.common import (
    Plan,
    compact,
    foreign_mcp_note,
    hook_view,
    is_switchboard_hook,
    json_removal,
    mcp_home,
    mcp_view,
    tilde,
)


def remove_cursor_hooks(data: dict[str, Any], home: str) -> list[str]:
    """Undo install's hook entries: drop switchboard's entries for this home (any version)
    and any event array that leaves empty; ``version`` and ``hooks`` stay."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return []
    lines: list[str] = []
    for event in list(hooks):
        arr = hooks[event]
        if not isinstance(arr, list):
            continue
        ours = [
            i
            for i, h in enumerate(arr)
            if isinstance(h, dict) and is_switchboard_hook(h.get("command"), home)
        ]
        if not ours:
            continue
        lines += [f"  - hooks.{event}[{i}]: {compact(hook_view(arr[i]))}" for i in ours]
        kept = [h for i, h in enumerate(arr) if i not in ours]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    return lines


def remove_mcp(data: dict[str, Any], home: str, where: str, harness: str, notes: list[str]) -> list[str]:
    """Drop ``mcpServers.switchboard`` if it runs switchboard's MCP server for this home
    (``mcpServers`` itself stays); otherwise leave it and say why."""
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or "switchboard" not in servers:
        return []
    entry = servers["switchboard"]
    if mcp_home(entry) != home:
        notes.append(foreign_mcp_note(where, entry, harness))
        return []
    del servers["switchboard"]
    return [f"  - mcpServers.switchboard: {compact(mcp_view(entry))}"]


def unplan(user_home: Path, home: str, *, run_commands: bool = True) -> Plan:
    """``switchboard uninstall cursor``: what ``switchboard install cursor`` used to write, removed."""
    p = Plan("cursor", verb="uninstall")
    mj = user_home / ".cursor" / "mcp.json"
    json_removal(
        p, mj, user_home, home, lambda d: remove_mcp(d, home, tilde(mj, user_home), "cursor", p.notes)
    )
    json_removal(
        p, user_home / ".cursor" / "hooks.json", user_home, home, lambda d: remove_cursor_hooks(d, home)
    )
    return p
