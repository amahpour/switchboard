"""md5s of the user's own harness config before and after a live run (DESIGN.md §0).

A change to any FAIL file fails the run. INFO files are written by the CLIs themselves (folder trust,
caches, session lists) and are only reported. Every live run goes through a temp home for switchboard and,
for Codex, a temp ``CODEX_HOME``; the real home is read for the CLIs' logins only.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .tmuxdrv import REAL_HOME

FAIL = (
    ".codex/config.toml",
    ".codex/hooks.json",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".cursor/mcp.json",
    ".cursor/hooks.json",
    ".config/devin/config.json",
    ".config/devin/mcp_config.json",
    ".switchboard/config.toml",
)
INFO = (".claude.json", ".cursor/cli-config.json", ".claude/plugins/installed_plugins.json")


def md5(p: Path) -> str | None:
    try:
        return hashlib.md5(p.read_bytes()).hexdigest()
    except OSError:
        return None


def snapshot(home: str | Path = REAL_HOME) -> dict[str, str | None]:
    root = Path(home)
    return {rel: md5(root / rel) for rel in FAIL + INFO}


def compare(before: dict[str, str | None], after: dict[str, str | None]) -> tuple[list[str], list[str]]:
    """(changed FAIL files, changed INFO files), as ``~``-relative names."""
    fail = [f"~/{k}" for k in FAIL if before.get(k) != after.get(k)]
    info = [f"~/{k}" for k in INFO if before.get(k) != after.get(k)]
    return fail, info
