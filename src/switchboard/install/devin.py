"""``switchboard install devin`` (DESIGN.md §9.7, §9.5).

- MCP server: ``~/.config/devin/mcp_config.json`` ``mcpServers.switchboard``
  (an entry of that name that doesn't run switchboard's MCP server is refused).
- Hooks: ``~/.config/devin/config.json`` ``hooks``, in the Claude shape
  (``[{"matcher": "", "hooks": [{type, command, timeout}]}]``), for
  SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop (timeout 30)
  and SessionEnd (10). PermissionRequest is never registered: a Devin hook
  can approve there.
- ``permissions.allow`` gains exactly switchboard's eight tool names
  (``mcp__switchboard__join`` ... ``mcp__switchboard__away``), per the decision on
  FINDINGS §14.3: Devin prompts for every unapproved MCP call, and a wildcard
  would also approve the tools of any other server named ``switchboard``. Nothing
  else is added to any allow list.
- Never written: ``read_config_from``, trust, permission modes, sandbox keys,
  or any other allow rule. Devin's import of ``~/.claude`` hooks is handled at
  run time: switchboard's Claude hooks see Devin's env and exit at once.

``switchboard uninstall devin`` (``unplan``) removes ``mcpServers.switchboard`` when it
runs switchboard's MCP server for this home, switchboard's hooks (any version) and
exactly the eight ``mcp__switchboard__*`` allow names; a ``hooks``,
``permissions.allow`` or ``permissions`` the removal empties is dropped. The
allow names are shared by every switchboard home, so they stay (with a note) while
the MCP entry or hooks of another switchboard home remain.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from switchboard.install.common import (
    FileEdit,
    InstallError,
    Plan,
    compact,
    deep,
    dump_json,
    hook_command,
    json_removal,
    load_json_obj,
    mcp_argv,
    mcp_home,
    other_hook_homes,
    refuse_foreign_mcp,
    remove_hook_groups,
    safe_text,
    set_hook_groups,
    tilde,
)
from switchboard.install.cursor import remove_mcp

EVENTS: tuple[tuple[str, int], ...] = (
    ("SessionStart", 10),
    ("UserPromptSubmit", 10),
    ("PreToolUse", 10),
    ("PostToolUse", 10),
    ("Stop", 30),
    ("SessionEnd", 10),
)
TOOLS = ("join", "leave", "who", "say", "read", "wait", "pass", "away")
ALLOW = tuple(f"mcp__switchboard__{t}" for t in TOOLS)


def mcp_entry(python: str, home: str) -> dict[str, Any]:
    argv = mcp_argv(python, home)
    return {"command": argv[0], "args": argv[1:]}


def hook_events(python: str, home: str, sha12: str) -> dict[str, tuple[str, int]]:
    return {e: (hook_command(python, home, sha12, "devin", e), t) for e, t in EVENTS}


def set_allow(data: dict[str, Any]) -> list[str]:
    """Add exactly switchboard's eight tool names to ``permissions.allow`` (nothing else)."""
    perms = data.get("permissions")
    if perms is None:
        perms = data["permissions"] = {}
    if not isinstance(perms, dict):
        raise InstallError(
            '"permissions" in ~/.config/devin/config.json is not an object; fix it by hand first'
        )
    allow = perms.get("allow")
    if allow is None:
        allow = perms["allow"] = []
    if not isinstance(allow, list):
        raise InstallError('"permissions.allow" is not a list; fix it by hand first')
    add = [n for n in ALLOW if n not in allow]
    allow.extend(add)
    return [f"  + permissions.allow: {compact(add)}"] if add else []


def set_mcp(data: dict[str, Any], entry: dict[str, Any]) -> list[str]:
    servers = data.get("mcpServers")
    if servers is None:
        servers = data["mcpServers"] = {}
    if not isinstance(servers, dict):
        raise InstallError(
            '"mcpServers" in ~/.config/devin/mcp_config.json is not an object; fix it by hand first'
        )
    have = servers.get("switchboard")
    refuse_foreign_mcp("~/.config/devin/mcp_config.json", have)
    if have == entry:
        return []
    servers["switchboard"] = dict(entry)
    return [f"  {'~' if have is not None else '+'} mcpServers.switchboard: {compact(entry)}"]


def remove_allow(data: dict[str, Any]) -> list[str]:
    """Remove exactly switchboard's eight tool names from ``permissions.allow``;
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
    """``switchboard uninstall devin``: the inverse of ``plan``."""
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
        # the eight names serve every switchboard home: keep them while another home's install remains
        others = sorted(
            {*other_hook_homes(dump_json(d), home), *filter(None, [_other_mcp_home(mcp_after, home)])}
        )
        allow = (d.get("permissions") or {}).get("allow") if isinstance(d.get("permissions"), dict) else None
        if not others:
            return lines + remove_allow(d)
        if isinstance(allow, list) and any(n in ALLOW for n in allow):
            p.notes.append(
                f"{tilde(cj, user_home)}: switchboard's eight allow names stay: the switchboard install for"
                f" another home ({', '.join(safe_text(o) for o in others)}) still uses them"
            )
        return lines

    json_removal(p, cj, user_home, home, remove)
    return p


def plan(user_home: Path, python: str, home: str, sha12: str, *, run_commands: bool = True) -> Plan:
    p = Plan("devin")
    base = user_home / ".config" / "devin"
    mj = base / "mcp_config.json"
    before, data = load_json_obj(mj)
    new = deep(data)
    lines = set_mcp(new, mcp_entry(python, home))
    p.edits.append(
        FileEdit(
            path=mj,
            before=before,
            after=dump_json(new) if lines else (before or ""),
            display=lines,
            label=tilde(mj, user_home),
        )
    )
    cj = base / "config.json"
    cbefore, cdata = load_json_obj(cj)
    cnew = deep(cdata)
    clines = set_hook_groups(cnew, hook_events(python, home, sha12), home)
    allow_lines = set_allow(cnew)
    clines += allow_lines
    p.edits.append(
        FileEdit(
            path=cj,
            before=cbefore,
            after=dump_json(cnew) if clines else (cbefore or ""),
            display=clines,
            label=tilde(cj, user_home),
        )
    )
    if allow_lines:
        p.notes.append(
            "permissions.allow pre-approves switchboard's eight tools in Devin: they run without an"
            " approval prompt (nothing else is allowlisted)"
        )
    p.notes.append(
        "hooks and the MCP server take effect in new Devin sessions; they are inert until a"
        " session joins a room"
    )
    p.notes.append("an idle Devin agent listens in wait(); interject by typing, then Enter on an empty line")
    return p


def print_args(python: str, home: str, sha12: str, workspace: Path | None = None) -> dict[str, Any]:
    """Project-local files for one workspace: switchboard's hooks, the eight allow names
    and the MCP entry. The launcher adds its own isolation (e.g. read_config_from)."""
    cfg: dict[str, Any] = {}
    set_hook_groups(cfg, hook_events(python, home, sha12), home)
    set_allow(cfg)
    return {
        "argv": [],
        "env": {},
        "files": {
            ".devin/config.json": dump_json(cfg),
            ".devin/mcp_config.json": json.dumps(
                {"mcpServers": {"switchboard": mcp_entry(python, home)}}, indent=2
            )
            + "\n",
        },
        "notes": ["put these files in the workspace (project-local Devin config); nothing was written"],
    }
