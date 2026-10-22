"""Live Claude Code over HTTPS (DESIGN.md §12.4, §49.5). Test-only; the product never emits these flags.

``claude --model haiku --plugin-dir <checkout>/plugins/claude``: the plugin from the checkout, its two
settings (the server's address and the agent token) in a 0600 ``--settings`` file under the run's temp
directory (``pluginConfigs`` of ``switchboard@inline``, the name Claude Code gives a ``--plugin-dir``
plugin), user settings off (``--setting-sources project,local``) and the account's claude.ai connectors
off, so the only MCP server is switchboard's. Permission mode ``manual`` with allow rules for switchboard's
tools and the scratch scripts: nothing runs unattended that the rules don't name, and a prompt that does
appear is declined with Esc, never approved. The login is the real home's; the CLI writes its own
``~/.claude.json`` entries for the scratch folder (reported, not failed).
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from . import screens
from .run import Run
from .tmuxdrv import REAL_HOME

REPO = Path(__file__).resolve().parents[3]
TOOLS = "mcp__plugin_switchboard_switchboard__*"
MODEL = "haiku"
# the scratch script, however the model spells its path (a subagent used the absolute one)
ALLOW = [TOOLS, "Bash(sh *hold.sh *)", "Bash(bash *hold.sh *)", "Bash(echo *)"]
# the cross-session messaging tools: a test session must never be able to message another session
DENY = ["SendMessage", "ListAgents"]


def plugin_dir() -> Path:
    """The plugin under test: this checkout's, or (to try a change, or to see a test fail against a broken
    plugin) the folder ``SWITCHBOARD_LIVE_CLAUDE_PLUGIN`` names."""
    return Path(os.environ.get("SWITCHBOARD_LIVE_CLAUDE_PLUGIN") or REPO / "plugins" / "claude")


UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def settings_json(server_url: str, token: str | None) -> dict[str, Any]:
    """The ``--settings`` file: the plugin's options for the ``--plugin-dir`` plugin (its address, and its
    token unless there is none: a session that signs in through the browser types no token, §49.11), and
    the permission rules."""
    options = {"server_url": server_url, **({"token": token} if token else {})}
    return {
        "pluginConfigs": {"switchboard@inline": {"options": options}},
        "permissions": {"allow": list(ALLOW), "deny": list(DENY)},
    }


def argv(plugin: Path, settings: Path, resume: bool) -> list[str]:
    return [
        "claude", "--model", MODEL, "--effort", "low", "--setting-sources", "project,local",
        "--plugin-dir", str(plugin), "--settings", str(settings),
        "--permission-mode", "manual", *(["--continue"] if resume else []),
    ]  # fmt: skip


def project_dir_name(ws: Path) -> str:
    """The folder under ``~/.claude/projects`` that holds a workspace's transcripts."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(ws))


class ClaudeRun(Run):
    harness = "claude"
    tier = "claude:https"
    binaries = ("claude",)
    resume_needs_prompt = False  # SessionStart reports a resume and starts the listener (#412)

    @property
    def claude_home(self) -> Path:
        """Where Claude Code keeps its transcripts and the plugin's data: the real home's ``.claude`` (a
        throwaway config directory, for a run that signs in, is another)."""
        return Path(REAL_HOME) / ".claude"

    def prepare(self) -> None:
        self.settings = self.base / "claude-settings.json"
        self.write_private(self.settings, json.dumps(settings_json(self.broker.url, self.token)))

    def argv(self, resume: bool) -> list[str]:
        return argv(plugin_dir(), self.settings, resume)

    def env(self) -> dict[str, str]:
        return {"DISABLE_AUTOUPDATER": "1", "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}

    def ready_step(self, screen: str) -> bool:
        key = screens.claude_dialog(screen)
        if key == "FAIL":
            raise AssertionError("a bypass-mode warning dialog appeared; it is never answered:\n" + screen)
        if key is not None:
            if key == "Enter":
                assert str(self.ws) in screen or "sb-live-" in screen, (
                    "the trust dialog is for an unexpected folder"
                )
            self.tmux.key(self.session, key)
            return False
        return screens.claude_ready(screen)

    def cost_note(self) -> str:
        """Tokens the session's transcript records: the CLI's own account of what it used."""
        d = self.claude_home / "projects" / project_dir_name(self.ws)
        tot = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        turns = 0
        for f in d.glob("*.jsonl"):
            for line in f.read_text(errors="replace").splitlines():
                try:
                    u = json.loads(line).get("message", {}).get("usage")
                except ValueError:
                    continue
                if isinstance(u, dict):
                    turns += 1
                    for k in tot:
                        tot[k] += int(u.get(k) or 0)
        return (
            f"{MODEL}: {turns} model calls, {tot['input_tokens']} input, {tot['output_tokens']} output, "
            f"{tot['cache_read_input_tokens']} cache-read tokens"
        )

    def scrub(self) -> None:
        """Remove what this run left in the real home: its transcripts (a folder named for the scratch
        directory) and the plugin's markers and log lines for its sessions, which the plugin writes under
        ``~/.claude/plugins/data`` (``--plugin-dir`` plugins share ``switchboard-inline``)."""
        d = self.claude_home / "projects" / project_dir_name(self.ws)
        if d.parent.name != "projects" or "-sb-live-claude-" not in d.name:
            return  # only ever the folder of this run's own scratch directory
        sessions = [f.stem for f in d.glob("*.jsonl")]
        shutil.rmtree(d, ignore_errors=True)
        data = self.claude_home / "plugins" / "data" / "switchboard-inline"
        for sid in sessions:
            if not UUID.fullmatch(sid):
                continue
            for f in data.glob(f"*{sid}*"):
                f.unlink(missing_ok=True)
        log = data / "listen.log"
        if sessions and log.exists():
            keep = [
                x
                for x in log.read_text(errors="replace").splitlines()
                if not any(sid in x for sid in sessions)
            ]
            if keep:
                log.write_text("\n".join(keep) + "\n")
            else:
                log.unlink(missing_ok=True)

    def input_text(self, screen: str) -> str:
        return screens.claude_input_text(screen)

    def wait_subagent_done(self, timeout: float = 240) -> None:
        """Claude Code prints 'Agent "<task>" finished' when a background agent reports back; the session then
        takes a turn on it, so wait for that and for the session to settle."""
        self.wait(lambda: self.flag("sub2.done"), timeout, "the subagent's last shell command to end")

        def finished() -> bool:
            seen = self.tmux.capture(self.session, history=300)
            return screens.claude_agent_finished(seen) and self.settled()

        self.wait(finished, timeout, "the background subagent to finish and the session to settle")

    def start_subagent(self) -> None:
        self.type(self.subagent_prompt())

    def subagent_prompt(self) -> str:
        return (
            "Start one subagent with the Agent tool, in the background (run_in_background true). Give it this"
            ' task: "Run exactly this Bash command from the current directory: sh hold.sh sub1. When it has'
            ' finished, run exactly this Bash command: sh hold.sh sub2. Then reply with the word subdone."'
            " After starting it, run nothing yourself and do not wait for it: reply with one word: started."
        )
