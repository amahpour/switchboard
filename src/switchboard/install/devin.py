"""``switchboard uninstall devin`` (DESIGN.md §9.7, §9.5).

``unplan`` removes what ``switchboard install devin`` used to write (retired in 0.38.0, #385):
``mcpServers.switchboard`` in ``~/.config/devin/mcp_config.json`` when it runs switchboard's MCP server
for this home, switchboard's hooks (any version) from ``~/.config/devin/config.json``, and exactly the nine
``mcp__switchboard__*`` allow names; a ``hooks``, ``permissions.allow`` or ``permissions`` the removal
empties is dropped. The allow names are shared by every switchboard home, so they stay (with a note) while
the MCP entry or hooks of another switchboard home remain.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from switchboard.install.common import (
    FileEdit,
    Plan,
    compact,
    dump_json,
    json_removal,
    mcp_home,
    other_hook_homes,
    remove_hook_groups,
    safe_text,
    tilde,
)
from switchboard.install.cursor import remove_mcp

TOOLS = ("join", "leave", "who", "say", "read", "wait", "pass", "away", "review")
ALLOW = tuple(f"mcp__switchboard__{t}" for t in TOOLS)


def remove_allow(data: dict[str, Any]) -> list[str]:
    """Remove exactly switchboard's nine tool names from ``permissions.allow``;
    drop ``allow`` / ``permissions`` only if that empties them."""
    perms = data.get("permissions")
    if not isinstance(perms, dict) or not isinstance(perms.get("allow"), list):
        return []
    allow = perms["allow"]
    gone = [n for n in allow if n in ALLOW]
    if not gone:
        return []
    kept = [n for n in allow if n not in ALLOW]
    if kept:
        perms["allow"] = kept
    else:
        del perms["allow"]
        if not perms:
            del data["permissions"]
    return [f"  - permissions.allow: {compact(list(dict.fromkeys(gone)))}"]


def _other_mcp_home(text: str | None, home: str) -> str | None:
    """The home of a switchboard MCP entry for another home left in ``mcp_config.json`` text."""
    try:
        data = json.loads(text) if text and text.strip() else {}
    except ValueError:
        return None
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    other = mcp_home(servers.get("switchboard")) if isinstance(servers, dict) else None
    return other if other != home else None


def unplan(user_home: Path, home: str, *, run_commands: bool = True) -> Plan:
    """``switchboard uninstall devin``: what ``switchboard install devin`` used to write, removed."""
    p = Plan("devin", verb="uninstall")
    base = user_home / ".config" / "devin"
    mj = base / "mcp_config.json"
    json_removal(
        p, mj, user_home, home, lambda d: remove_mcp(d, home, tilde(mj, user_home), "devin", p.notes)
    )
    mcp_after = p.edits[-1].after if isinstance(p.edits[-1], FileEdit) else None
    cj = base / "config.json"

    def remove(d: dict[str, Any]) -> list[str]:
        lines = remove_hook_groups(d, home, drop_empty_hooks=True)[0]
        # the nine names serve every switchboard home: keep them while another home's install remains
        others = sorted(
            {*other_hook_homes(dump_json(d), home), *filter(None, [_other_mcp_home(mcp_after, home)])}
        )
        allow = (d.get("permissions") or {}).get("allow") if isinstance(d.get("permissions"), dict) else None
        if not others:
            return lines + remove_allow(d)
        if isinstance(allow, list) and any(n in ALLOW for n in allow):
            p.notes.append(
                f"{tilde(cj, user_home)}: switchboard's nine allow names stay: the switchboard install for"
                f" another home ({', '.join(safe_text(o) for o in others)}) still uses them"
            )
        return lines

    json_removal(p, cj, user_home, home, remove)
    return p
