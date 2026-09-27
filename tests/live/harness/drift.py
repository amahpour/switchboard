"""md5s of user-level harness config before and after a live run (DESIGN.md §0).

A change to any FAIL file fails the run; INFO files are written by the CLIs
themselves (workspace trust, caches) and are only reported.
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Any

from harness.tmuxdrv import REAL_HOME

FAIL = (".codex/config.toml", ".codex/hooks.json", ".claude/settings.json", ".cursor/mcp.json",
        ".cursor/hooks.json", ".config/devin/config.json", ".config/devin/mcp_config.json")
INFO = (".claude.json", ".cursor/cli-config.json")


def md5(p: Path) -> str | None:
    try:
        return hashlib.md5(p.read_bytes()).hexdigest()
    except OSError:
        return None


def snapshot() -> dict[str, str | None]:
    home = Path(REAL_HOME)
    return {rel: md5(home / rel) for rel in FAIL + INFO}


def compare(before: dict[str, str | None], after: dict[str, str | None]) -> tuple[list[str], list[str]]:
    """(changed FAIL files, changed INFO files), as ~-relative names."""
    fail = [f"~/{k}" for k in FAIL if before.get(k) != after.get(k)]
    info = [f"~/{k}" for k in INFO if before.get(k) != after.get(k)]
    return fail, info


# The Codex TUI itself writes these tables of ~/.codex/config.toml (NUX
# counters, model-migration notices, FINDINGS §15); a change there is logged.
# A change anywhere else fails a live Codex run.
CODEX_TUI_TABLES = ("notice", "tui")


def codex_config() -> dict[str, Any] | None:
    """~/.codex/config.toml, parsed in memory only (it may hold other servers' secrets: never printed)."""
    try:
        with open(Path(REAL_HOME) / ".codex" / "config.toml", "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def codex_config_diff(before: dict[str, Any] | None, after: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    """(changed top-level keys outside the TUI's own tables, changed TUI tables). Names only."""
    if before is None or after is None:
        return (["<unreadable>"] if before != after else []), []
    keys = set(before) | set(after)
    bad = sorted(k for k in keys if k not in CODEX_TUI_TABLES and before.get(k) != after.get(k))
    tui = sorted(k for k in keys if k in CODEX_TUI_TABLES and before.get(k) != after.get(k))
    return bad, tui
