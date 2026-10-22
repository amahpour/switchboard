"""Live Cursor Agent over HTTPS (DESIGN.md §12.4, §49.6). Test-only; the product never emits these flags.

The scratch project carries Cursor's project-level config, made from this checkout's plugin
(``plugins/cursor``): ``.cursor/mcp.json`` is the plugin's ``mcp.json`` with its two variables read from
the environment, and ``.cursor/hooks.json`` is the plugin's hook file with the plugin's path filled in.
The address and the token reach both the server entry and the hooks through the child's environment, never
a file or a command line. Cursor runs with a **temp $HOME**, so the user's own ``~/.cursor`` (servers, hooks,
plugins) is never read or written; its login is the real ``~/.config/cursor`` (``XDG_CONFIG_HOME``), the
only thing of the real home it is given. The project's server is approved with ``agent mcp enable``, and
the workspace is trusted per launch with ``--trust`` for the scratch folder only. Approving the server is not
approving its tools: Cursor asks "Run this MCP tool?" for each call, so the project's ``.cursor/cli.json``
allows switchboard's eleven tools by name and the scratch script (``sh hold.sh``), as the Claude Code and
Devin profiles do, and nothing else; any other prompt is declined with Esc. The model is ``auto``, the only
one a free account has.

Cursor's quota may be used up (the account here had none until 2026-10-22): the connection test needs no
quota, and everything that asks the model for something is skipped, with the reason, when the account
can't make a call (``unavailable``).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import screens
from .run import Run
from .tmuxdrv import REAL_HOME, clean_env

REPO = Path(__file__).resolve().parents[3]
MODEL = "auto"
SERVER = "switchboard-https"
TOOLS = ("join", "leave", "who", "say", "read", "wait", "pass", "away", "claim", "review", "status")
# ``Mcp(<server>:<tool>)`` is the entry Cursor's own "Allowlist MCP Tool" writes; ``Shell(<command>)`` matches
# a command that starts with those words, so this is the scratch script and not every ``sh``
ALLOW = [f"Mcp({SERVER}:switchboard_{t})" for t in TOOLS] + ["Shell(sh hold.sh)"]


def plugin_dir() -> Path:
    return Path(os.environ.get("SWITCHBOARD_LIVE_CURSOR_PLUGIN") or REPO / "plugins" / "cursor")


def project_mcp(plugin: Path) -> dict:
    """``.cursor/mcp.json``: the plugin's server entry, its variables read from the environment."""
    text = (plugin / "mcp.json").read_text()
    text = text.replace("${SWITCHBOARD_URL}", "${env:SWITCHBOARD_URL}")
    return json.loads(text.replace("${SWITCHBOARD_AGENT_TOKEN}", "${env:SWITCHBOARD_AGENT_TOKEN}"))


def project_hooks(plugin: Path) -> dict:
    """``.cursor/hooks.json``: the plugin's hook file with ``${CURSOR_PLUGIN_ROOT}`` filled in."""
    return json.loads(
        (plugin / "hooks" / "hooks.json").read_text().replace("${CURSOR_PLUGIN_ROOT}", str(plugin))
    )


def project_cli() -> dict:
    """``.cursor/cli.json``: this project's permissions only. Cursor merges them over the user's, which here
    (a temp $HOME) are Cursor's defaults."""
    return {"permissions": {"allow": list(ALLOW), "deny": []}}


def argv(resume: bool) -> list[str]:
    # --trust: the workspace is the run's own scratch repository, trusted for this launch only
    return ["agent", "--model", MODEL, "--trust", *(["--continue"] if resume else [])]


def unavailable(output: str) -> str | None:
    """Why a one-word call to Cursor's model failed, in a line, or None when it did not fail."""
    if screens.quota_exhausted(output):
        return "the account has hit its usage limit"
    low = output.lower()
    if "not logged in" in low or "authentication" in low or "log in" in low:
        return "Cursor is not logged in (`agent login`)"
    return None


class CursorRun(Run):
    harness = "cursor"
    tier = "cursor:https"
    binaries = ("agent",)
    quit_command = "/quit"
    input_marker = "→"
    # A message for a busy Cursor comes as hook context after its next tool call, any tool's (#493): the
    # plugin's postToolUse hooks cover every tool. Before that they matched switchboard's own tools only and a
    # shell command ran none, so the message waited for the next switchboard call (path ``say``, seen live on
    # 2026-10-22, #394). ``Run.mid_paths`` is ``{"hook_ctx"}``; a unit test holds it to what the hooks do.
    helpers = ("typescript",)  # Cursor's own language-server helper (an npx node process) outlives it

    def prepare(self) -> None:
        self.cursor_home = self.base / "cursor-home"
        self.cursor_home.mkdir(mode=0o700)
        plugin = plugin_dir()
        cfg = self.ws / ".cursor"
        cfg.mkdir()
        (cfg / "mcp.json").write_text(json.dumps(project_mcp(plugin), indent=1))
        (cfg / "hooks.json").write_text(json.dumps(project_hooks(plugin), indent=1))
        (cfg / "cli.json").write_text(json.dumps(project_cli(), indent=1))

    def env(self) -> dict[str, str]:
        return {
            "HOME": str(self.cursor_home),
            "XDG_CONFIG_HOME": str(Path(REAL_HOME) / ".config"),
            "SWITCHBOARD_URL": self.broker.url,
            "SWITCHBOARD_AGENT_TOKEN": self.token,
        }

    def cli(self, *args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["agent", *args], env=clean_env(self.path, **self.env()), cwd=self.ws, capture_output=True,
            text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )  # fmt: skip

    def enable_server(self) -> None:
        """Approve the project's server (what the TUI would ask a person)."""
        r = self.cli("mcp", "enable", SERVER)
        assert r.returncode == 0 and "approved" in r.stdout.lower(), (r.stdout + r.stderr)[-300:]

    def listed_tools(self) -> list[str]:
        r = self.cli("mcp", "list-tools", SERVER)
        assert r.returncode == 0, (r.stdout + r.stderr)[-400:]
        return [line[2:].split(" ")[0] for line in r.stdout.splitlines() if line.startswith("- ")]

    def model_unavailable(self) -> str | None:
        """None when the account can make a call; else why not. One word of output, in print mode."""
        try:
            r = self.cli("-p", "--trust", "--model", MODEL, "reply with the word ok", timeout=90)
        except subprocess.TimeoutExpired:
            return "a one-word call to Cursor's model timed out"
        out = r.stdout + r.stderr
        why = unavailable(out)
        if why:
            return why
        if r.returncode != 0 or "ok" not in out.lower():
            return f"a one-word call to Cursor's model failed: {out.strip()[:120]!r}"
        return None

    def argv(self, resume: bool) -> list[str]:
        return argv(resume)

    def ready_step(self, screen: str) -> bool:
        assert not screens.quota_exhausted(screen), "Cursor's usage limit is hit:\n" + screen
        return "Add a follow-up" in screen or "Plan, search, build anything" in screen or "→" in screen

    def start_subagent(self) -> None:
        raise NotImplementedError
