"""``switchboard install cursor`` (DESIGN.md §9.7, §9.4).

- MCP server: ``~/.cursor/mcp.json`` ``mcpServers.switchboard = {command, args}``
  (the same command as every harness: the venv's Python, never ``uv run``).
  An entry of that name that doesn't run switchboard's MCP server is refused.
- Hooks: ``~/.cursor/hooks.json`` ``{"version": 1, "hooks": {...}}`` for
  sessionStart, beforeSubmitPrompt, postToolUse, postToolUseFailure, stop and
  sessionEnd. The stop hook long-polls the broker (a *park*), so it gets an
  explicit ``timeout`` (``stop_park_s + 60``; Cursor's default of 60 s kills a
  longer park and loses what it held) and ``loop_limit: null`` (switchboard's own
  wake budget bounds the follow-ups), and its command carries
  ``--max-wait stop_park_s + 30``. The other events get ``timeout: 10``.
- An older switchboard hook for this home is replaced; everything else is kept.
- Cursor doesn't reload ``hooks.json`` in a running CLI session: it takes
  effect in new sessions. Cursor also imports ``~/.claude`` hooks; switchboard's
  Claude hooks see Cursor's payload (``cursor_version``) and exit at once.
- Never written: permissions, allow rules, MCP approvals,
  trust, sandbox settings.

``switchboard uninstall cursor`` (``unplan``) removes ``mcpServers.switchboard`` when
it runs switchboard's MCP server for this home, and switchboard's hook entries (any
version) from ``~/.cursor/hooks.json``; an event array the removal empties is
dropped, while ``version`` and the ``hooks``/``mcpServers`` objects stay (the
files keep their documented shape).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from switchboard.config import Config, ConfigError
from switchboard.config import load as load_config
from switchboard.install.common import (
    FileEdit,
    InstallError,
    Plan,
    compact,
    deep,
    dump_json,
    foreign_mcp_note,
    hook_command,
    hook_view,
    is_switchboard_hook,
    json_removal,
    load_json_obj,
    mcp_argv,
    mcp_home,
    mcp_view,
    refuse_foreign_mcp,
    tilde,
)

FAST_EVENTS = ("sessionStart", "beforeSubmitPrompt", "postToolUse", "postToolUseFailure", "sessionEnd")
ORDER = ("sessionStart", "beforeSubmitPrompt", "postToolUse", "postToolUseFailure", "stop", "sessionEnd")
TIMEOUT_S = 10
STOP_TIMEOUT_EXTRA_S = 60  # the hook's own limit, above the park
STOP_WAIT_EXTRA_S = 30  # the hook waits this much longer than the broker's park


def config_for(home: str) -> Config:
    from switchboard.paths import Paths

    try:
        return load_config(Paths.from_home(home))
    except ConfigError as e:
        raise InstallError(f"can't read {home}/config.toml: {e}") from None
    except OSError:
        return Config()


def mcp_entry(python: str, home: str) -> dict[str, Any]:
    argv = mcp_argv(python, home)
    return {"command": argv[0], "args": argv[1:]}


def hook_entries(python: str, home: str, sha12: str, cfg: Config | None = None) -> dict[str, dict[str, Any]]:
    """event -> the one hook entry switchboard owns for it."""
    park = int((cfg or Config()).cursor.stop_park_s)
    out: dict[str, dict[str, Any]] = {}
    for e in ORDER:
        if e == "stop":
            out[e] = {"command": hook_command(python, home, sha12, "cursor", e, max_wait=park + STOP_WAIT_EXTRA_S),
                      "timeout": park + STOP_TIMEOUT_EXTRA_S, "loop_limit": None}
        else:
            out[e] = {"command": hook_command(python, home, sha12, "cursor", e), "timeout": TIMEOUT_S}
    return out


def set_cursor_hooks(data: dict[str, Any], entries: dict[str, dict[str, Any]], home: str) -> list[str]:
    """Cursor-shaped hooks: ``hooks.<event> = [{command, timeout[, loop_limit]}]``.
    An older switchboard entry for this home is replaced; other entries are kept."""
    lines: list[str] = []
    if "version" not in data:
        data["version"] = 1
    hooks = data.get("hooks")
    if hooks is None:
        hooks = data["hooks"] = {}
    if not isinstance(hooks, dict):
        raise InstallError('"hooks" in ~/.cursor/hooks.json is not an object; fix it by hand first')
    for event, want in entries.items():
        arr = hooks.get(event)
        if arr is None:
            arr = hooks[event] = []
        if not isinstance(arr, list):
            raise InstallError(f'"hooks.{event}" is not a list; fix it by hand first')
        if any(isinstance(h, dict) and h == want for h in arr):
            continue
        kept = []
        for h in arr:
            if isinstance(h, dict) and is_switchboard_hook(h.get("command"), home):
                lines.append(f"  - hooks.{event}: an older switchboard hook")
                continue
            kept.append(h)
        kept.append(dict(want))
        hooks[event] = kept
        lines.append(f"  + hooks.{event}[{len(kept) - 1}]: {compact(want)}")
    return lines


def set_mcp(data: dict[str, Any], entry: dict[str, Any]) -> list[str]:
    servers = data.get("mcpServers")
    if servers is None:
        servers = data["mcpServers"] = {}
    if not isinstance(servers, dict):
        raise InstallError('"mcpServers" in ~/.cursor/mcp.json is not an object; fix it by hand first')
    have = servers.get("switchboard")
    refuse_foreign_mcp("~/.cursor/mcp.json", have)
    if have == entry:
        return []
    servers["switchboard"] = dict(entry)
    return [f"  {'~' if have is not None else '+'} mcpServers.switchboard: {compact(entry)}"]


def remove_cursor_hooks(data: dict[str, Any], home: str) -> list[str]:
    """Undo ``set_cursor_hooks``: drop switchboard's entries for this home (any version)
    and any event array that leaves empty; ``version`` and ``hooks`` stay."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return []
    lines: list[str] = []
    for event in list(hooks):
        arr = hooks[event]
        if not isinstance(arr, list):
            continue
        ours = [i for i, h in enumerate(arr) if isinstance(h, dict) and is_switchboard_hook(h.get("command"), home)]
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
    """``switchboard uninstall cursor``: the inverse of ``plan``."""
    p = Plan("cursor", verb="uninstall")
    mj = user_home / ".cursor" / "mcp.json"
    json_removal(p, mj, user_home, home, lambda d: remove_mcp(d, home, tilde(mj, user_home), "cursor", p.notes))
    json_removal(p, user_home / ".cursor" / "hooks.json", user_home, home, lambda d: remove_cursor_hooks(d, home))
    return p


def plan(user_home: Path, python: str, home: str, sha12: str, *, run_commands: bool = True) -> Plan:
    cfg = config_for(home)
    p = Plan("cursor")
    mj = user_home / ".cursor" / "mcp.json"
    before, data = load_json_obj(mj)
    new = deep(data)
    lines = set_mcp(new, mcp_entry(python, home))
    p.edits.append(FileEdit(path=mj, before=before, after=dump_json(new) if lines else (before or ""),
                            display=lines, label=tilde(mj, user_home)))
    hj = user_home / ".cursor" / "hooks.json"
    hbefore, hdata = load_json_obj(hj)
    hnew = deep(hdata)
    hlines = set_cursor_hooks(hnew, hook_entries(python, home, sha12, cfg), home)
    p.edits.append(FileEdit(path=hj, before=hbefore, after=dump_json(hnew) if hlines else (hbefore or ""),
                            display=hlines, label=tilde(hj, user_home)))
    p.notes.append("Cursor doesn't reload hooks.json in a running agent: this takes effect in new `agent`"
                   " sessions; the hooks are inert until a session joins a room")
    p.notes.append("idle Cursor sessions are reached by a parked stop hook (tier cursor:stop-park, provisional:"
                   " parks longer than ~40 s are unproven until the Cursor re-test)")
    p.notes.append("switchboard's tools may ask for approval in Cursor (no allow rule is written)")
    return p


def print_args(python: str, home: str, sha12: str, workspace: Path | None = None) -> dict[str, Any]:
    """Project-local files for one test workspace: only switchboard's MCP entry and hooks."""
    cfg = config_for(home)
    hooks: dict[str, Any] = {}
    set_cursor_hooks(hooks, hook_entries(python, home, sha12, cfg), home)
    return {
        "argv": [],
        "env": {},
        "files": {
            ".cursor/mcp.json": json.dumps({"mcpServers": {"switchboard": mcp_entry(python, home)}}, indent=2) + "\n",
            ".cursor/hooks.json": dump_json(hooks),
        },
        "notes": ["put these files in the workspace (project-local Cursor config); nothing was written"],
    }
