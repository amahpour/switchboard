"""The live Codex launch profile (DESIGN.md §0, §12.4). Test-only; the product never emits these flags.

Codex runs on the user's **real** ``CODEX_HOME`` (its login is used as-is;
``auth.json`` is never copied, linked or moved) but never touches the user's
shared daemon: the test starts a **private** ``codex app-server --listen
unix://<short tmp path>`` with ``cwd`` = the scratch workspace and ``-c``
overrides only, and attaches the TUI with ``codex --remote unix://<sock>``.
A plain ``codex`` or ``codex queue`` is never run (with ``daemon_auto_start``
on, either could start the shared daemon with the test's env).

Overrides: approvals on request, a workspace-write sandbox, every MCP server
and plugin in the user's config disabled (names read at runtime, never
hard-coded), apps off, switchboard's MCP entry from ``install codex
--print-args``, project trust for the scratch workspace (so its
``.codex/hooks.json`` loads), ``hooks.state`` from ``codex_trust.py`` (the
project hooks trusted at their current hash, every other hook disabled), and
``tui.model_availability_nux`` pinned high so the TUI doesn't bump its
counter in the user's ``config.toml``.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from harness.tmuxdrv import REAL_HOME

CODEX_HOME = Path(REAL_HOME) / ".codex"
MODEL = "gpt-6-luna"
EFFORT = "low"
NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
# per launch: no "Update available" dialog at TUI start (M7: 0.157.0 appeared and the TUI waited on it)
NO_UPDATE_CHECK = ["-c", "check_for_update_on_startup=false"]


def user_config() -> dict[str, Any]:
    """The user's Codex config, parsed in memory (never printed or copied)."""
    with open(CODEX_HOME / "config.toml", "rb") as f:
        return tomllib.load(f)


def toml_str(s: str) -> str:
    return json.dumps(s)


def model_slugs() -> list[str]:
    try:
        d = json.loads((CODEX_HOME / "models_cache.json").read_text())
        return [m["slug"] for m in d.get("models", []) if isinstance(m, dict) and isinstance(m.get("slug"), str)]
    except (OSError, ValueError, KeyError):
        return []


def disable_overrides() -> list[str]:
    """-c pairs that turn off every user MCP server and plugin (names read at runtime)."""
    cfg = user_config()
    out: list[str] = []
    for name in (cfg.get("mcp_servers") or {}):
        if not NAME_RE.fullmatch(name):
            raise RuntimeError("an MCP server name the -c parser can't take; add it by hand")
        out += ["-c", f"mcp_servers.{name}.enabled=false"]
    plugins = cfg.get("plugins") or {}
    if plugins:
        table = ",".join(f"{toml_str(n)}={{enabled=false}}" for n in plugins)
        out += ["-c", f"plugins={{{table}}}"]
    out += ["-c", "features.apps=false"]
    return out


def nux_override() -> list[str]:
    cfg = user_config()
    names = set(model_slugs()) | set(((cfg.get("tui") or {}).get("model_availability_nux") or {}))
    names |= {MODEL, "gpt-5.5"}
    table = ",".join(f"{toml_str(n)}=99" for n in sorted(names))
    return ["-c", f"tui.model_availability_nux={{{table}}}"]


def project_trust(ws: Path) -> list[str]:
    paths = {str(ws), os.path.realpath(ws)}
    table = ",".join(f"{toml_str(p)}={{trust_level=\"trusted\"}}" for p in sorted(paths))
    return ["-c", f"projects={{{table}}}"]


def base_overrides(ws: Path, print_args: dict[str, Any]) -> list[str]:
    """Everything but hooks.state (computed from these by codex_trust.py)."""
    argv = print_args["argv"]
    assert argv[0] == "-c" and argv[1].startswith("mcp_servers.switchboard=")
    return (["-c", 'approval_policy="on-request"', "-c", 'sandbox_mode="workspace-write"']
            + disable_overrides() + ["-c", argv[1]] + project_trust(ws) + nux_override())


def app_server_argv(codex_bin: str, sock: str, overrides: list[str], hooks_state: str) -> list[str]:
    return [codex_bin, "app-server", "--listen", f"unix://{sock}", *overrides, "-c", f"hooks.state={hooks_state}"]


def tui_argv(codex_bin: str, sock: str) -> list[str]:
    """A ``--remote`` TUI sends its **own** config's approval and sandbox policy with
    ``thread/start`` (which may be never + danger-full-access), so it gets them too."""
    return [codex_bin, "--remote", f"unix://{sock}", "-a", "on-request", "-s", "workspace-write", "-m", MODEL,
            "-c", f'model_reasoning_effort="{EFFORT}"', *NO_UPDATE_CHECK]
