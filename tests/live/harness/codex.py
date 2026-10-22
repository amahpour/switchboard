"""Live Codex over HTTPS (DESIGN.md §12.4, §49.5). Test-only; the product never emits these flags.

Codex runs on a **temp ``CODEX_HOME``** that holds a copy of the login's ``auth.json`` and nothing else of
the user's: no config, no plugins, no hooks, no MCP servers. Into it go the plugin from this checkout
(installed from a throwaway marketplace with a ``local`` source, as before #462; ``codex plugin add``), the
user's own ``[mcp_servers.switchboard]`` entry the plugin's hooks name (§49.5; the token comes from the
child's environment, ``bearer_token_env_var``), trust for the scratch folder and for the plugin's hooks
(``hooks.state``, hashes read from Codex's own ``hooks/list`` of a private stdio app-server, never the
shared daemon). The copy of the login is mode 0600, never printed, and deleted when the run ends: Codex
may rotate a refresh token in its copy, which is why ``auth.json`` is copied only for a throwaway home.
Approvals ``on-request`` and a ``workspace-write`` sandbox, never ``never`` or ``danger-full-access``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import screens
from .run import Run
from .tmuxdrv import REAL_HOME

REPO = Path(__file__).resolve().parents[3]
MODEL = "gpt-6-luna"
EFFORT = "low"
# off: the account's connectors and remote plugins, and the tools a switchboard test never uses
OFF_FEATURES = (
    "apps",
    "remote_plugin",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "image_generation",
)
NO_UPDATE_CHECK = ["-c", "check_for_update_on_startup=false"]
MARKETPLACE = {
    "name": "switchboard",
    "interface": {"displayName": "switchboard"},
    "plugins": [
        {
            "name": "switchboard",
            "source": {"source": "local", "path": "./plugins/codex"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Developer Tools",
        }
    ],
}
_RESUME_ID = re.compile(r"codex resume ([0-9a-f]{8}-[0-9a-f-]{27})")


def toml_str(s: str) -> str:
    return json.dumps(s)


def config_toml(server_url: str, ws: Path, hooks: list[dict[str, Any]], extra: str = "") -> str:
    """Everything the run adds to the temp home's ``config.toml`` after the CLI wrote the marketplace and the
    plugin: the server the plugin's hooks name (§49.5), trust for the scratch folder, trust for each of the
    plugin's hooks at its current hash, and the features that stay off."""
    out = [
        extra,
        "[mcp_servers.switchboard]",
        f"url = {toml_str(server_url + '/mcp')}",
        'bearer_token_env_var = "SWITCHBOARD_AGENT_TOKEN"',
        "tool_timeout_sec = 86400",  # longer than the Stop hook's wait (§49.4, #449)
        "",
        f"[projects.{toml_str(str(ws))}]",
        'trust_level = "trusted"',
        "",
        "[features]",
        *[f"{f} = false" for f in OFF_FEATURES],
    ]
    for h in hooks:
        out += ["", f"[hooks.state.{toml_str(h['key'])}]", f"trusted_hash = {toml_str(h['currentHash'])}"]
    return "\n".join(out) + "\n"


def plugin_dir() -> Path:
    """The plugin under test: this checkout's, or the folder ``SWITCHBOARD_LIVE_CODEX_PLUGIN`` names."""
    return Path(os.environ.get("SWITCHBOARD_LIVE_CODEX_PLUGIN") or REPO / "plugins" / "codex")


def argv(ws: Path, thread: str | None, resume: bool) -> list[str]:
    """Approvals on request and a workspace-write sandbox, whatever the user's own config says."""
    first = ["codex"]
    if resume:
        first += ["resume", thread] if thread else ["resume", "--last"]
    return [
        *first, "-m", MODEL, "-c", f'model_reasoning_effort="{EFFORT}"', *NO_UPDATE_CHECK,
        "-a", "on-request", "-s", "workspace-write", "-C", str(ws),
    ]  # fmt: skip


def resume_id(screen: str) -> str | None:
    """The thread id Codex prints when it quits ("codex resume <id>")."""
    m = _RESUME_ID.search(screen)
    return m.group(1) if m else None


class CodexRun(Run):
    harness = "codex"
    tier = "codex:https"
    binaries = ("codex",)
    quit_command = "/quit"
    input_marker = "›"
    has_subagents = True

    def prepare(self) -> None:
        self.codex_home = self.base / "codex-home"
        self.codex_home.mkdir(mode=0o700)
        real_auth = Path(REAL_HOME) / ".codex" / "auth.json"
        if not real_auth.exists():
            raise AssertionError("Codex is not logged in (no ~/.codex/auth.json); run `codex login` first")
        auth = self.codex_home / "auth.json"
        self.write_private(auth, real_auth.read_text())
        market = self.base / "marketplace"
        (market / ".agents" / "plugins").mkdir(parents=True)
        (market / "plugins").mkdir()
        shutil.copytree(plugin_dir(), market / "plugins" / "codex")
        (market / ".agents" / "plugins" / "marketplace.json").write_text(json.dumps(MARKETPLACE))
        for args in (
            ["plugin", "marketplace", "add", str(market)],
            ["plugin", "add", "switchboard@switchboard"],
        ):
            r = subprocess.run(
                ["codex", *args],
                env=self.setup_env(),
                cwd=self.ws,
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert r.returncode == 0, f"codex {' '.join(args)} failed: {r.stderr[-300:]}"
        hooks = [h for h in self.hooks_list() if h.get("pluginId") == "switchboard@switchboard"]
        assert hooks, "the plugin's hooks were not found in Codex's list"
        cfg = self.codex_home / "config.toml"
        cfg.write_text(config_toml(self.broker.url, self.ws, hooks, extra=cfg.read_text()))
        os.chmod(cfg, 0o600)

    def setup_env(self) -> dict[str, str]:
        """For a Codex command that is not the TUI: the temp home as both $HOME and CODEX_HOME."""
        return {
            "HOME": str(self.base),
            "PATH": self.path,
            "CODEX_HOME": str(self.codex_home),
            "TERM": "xterm-256color",
            "LANG": "en_US.UTF-8",
        }

    def hooks_list(self) -> list[dict[str, Any]]:
        """Codex's own list of the hooks it would run here, from a private stdio app-server (never the shared
        daemon), with the keys and hashes ``hooks.state`` trusts."""
        p = subprocess.Popen(
            ["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=self.setup_env(), cwd=self.ws, text=True, start_new_session=True,
        )  # fmt: skip
        assert p.stdin is not None and p.stdout is not None
        try:

            def send(obj: dict[str, Any]) -> None:
                p.stdin.write(json.dumps(obj) + "\n")  # type: ignore[union-attr]
                p.stdin.flush()  # type: ignore[union-attr]

            def recv(want: int) -> dict[str, Any]:
                while True:
                    line = p.stdout.readline()  # type: ignore[union-attr]
                    if not line:
                        raise AssertionError("codex app-server closed before it answered hooks/list")
                    msg = json.loads(line)
                    if msg.get("id") == want and "method" not in msg:
                        return msg

            send(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {"clientInfo": {"name": "switchboard-live", "version": "0"}},
                }
            )
            recv(1)
            send({"method": "initialized"})
            send({"id": 2, "method": "hooks/list", "params": {"cwds": [str(self.ws)]}})
            res = recv(2)
            assert "error" not in res, res
            return [h for entry in res["result"]["data"] for h in entry["hooks"]]
        finally:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(5)

    def argv(self, resume: bool) -> list[str]:
        return argv(self.ws, resume_id(self.last_screen) if resume else None, resume)

    def env(self) -> dict[str, str]:
        return {
            "HOME": str(self.base),
            "CODEX_HOME": str(self.codex_home),
            "SWITCHBOARD_AGENT_TOKEN": self.token,
        }

    def ready_step(self, screen: str) -> bool:
        return screens.codex_ready(screen)

    def start_subagent(self) -> None:
        # Codex holds its prompt while its Stop hook waits, so the task comes as a room message. Naming the
        # tool is what makes the cheap model call it (a bare "use a subagent" it answers without one).
        self.mention(
            "Delegate a job to a subagent: call your spawn_agent tool (collaboration.spawn_agent) once, with"
            ' task_name runner and the message: run the shell command "sh hold.sh sub1", and when it has'
            ' finished the shell command "sh hold.sh sub2", then reply done. Do not do the job yourself.'
            " After it has started, do not wait for it: reply with the single word: started."
        )

    def cost_note(self) -> str:
        """Tokens the thread's rollout files record (the last cumulative count of each)."""
        total = 0
        files = 0
        for f in (self.codex_home / "sessions").rglob("*.jsonl"):
            last = 0
            for line in f.read_text(errors="replace").splitlines():
                if '"token_count"' not in line:
                    continue
                try:
                    info = json.loads(line)["payload"]["info"]
                    last = int(info["total_token_usage"]["total_tokens"])
                except (ValueError, KeyError, TypeError):
                    continue
            total += last
            files += 1
        return f"{MODEL} ({EFFORT}): {total} tokens over {files} thread(s)"

    def scrub(self) -> None:
        """Delete the copy of the login first, before anything can fail."""
        try:
            (self.codex_home / "auth.json").unlink()
        except (AttributeError, OSError):
            pass
