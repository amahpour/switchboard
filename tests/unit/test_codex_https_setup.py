"""Codex over HTTPS keeps its Stop hook open only if its MCP server entry says so (DESIGN.md §49.4,
#449): Codex 0.158.0 ends the hook's call after the server's ``tool_timeout_sec``, five minutes unless set,
whatever the hook's own ``timeout`` is. The plugin can't carry the entry (§49.5), so the setup that people
copy (docs/USAGE.md, and Settings → Agents) has to. This ties both to the plugin's Stop hook."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def stop_hook() -> dict:
    hooks = json.loads((ROOT / "plugins/codex/hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    [group] = hooks["Stop"]
    [hook] = group["hooks"]
    return hook


def usage_entry() -> dict:
    """The ``[mcp_servers.switchboard]`` block of the Codex setup in docs/USAGE.md, parsed."""
    text = (ROOT / "docs/USAGE.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```toml\n(\[mcp_servers\.switchboard\]\n.*?)```", text, re.S)
    assert len(blocks) == 1, "USAGE.md has one Codex server block"
    return tomllib.loads(blocks[0])["mcp_servers"]["switchboard"]


def test_the_codex_setup_in_usage_outlasts_the_stop_hooks_wait() -> None:
    """Fails if USAGE's copy-and-paste Codex entry has no ``tool_timeout_sec``, or one that ends the
    Stop hook's call before the hook's wait does: Codex would stop listening after a turn."""
    hook = stop_hook()
    wait = int(hook["input"]["wait_s"])
    got = usage_entry().get("tool_timeout_sec")
    assert isinstance(got, int) and got > wait, f"tool_timeout_sec {got!r} must exceed wait_s {wait}"
    assert got >= hook["timeout"], "and the hook's own timeout, or that is what the hook can't use"


def test_the_codex_setup_in_settings_has_the_timeout_usage_has() -> None:
    """Settings → Agents shows the entry with the person's own address. Fails if the page's copy has
    no timeout, or a different one from USAGE's (the e2e test reads what the page really shows)."""
    js = (ROOT / "src/switchboard/web/static/app.js").read_text(encoding="utf-8")
    shown = re.findall(r"tool_timeout_sec = (\d+)", js)
    assert shown == [str(usage_entry()["tool_timeout_sec"])], shown
