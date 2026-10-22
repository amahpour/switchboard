"""Live Devin over HTTPS (DESIGN.md §12.4, §49.7). Test-only; the product never emits these flags.

Devin reads a project's ``.devin/config.json`` and ``.devin/mcp_config.json`` (§49.7): the first is
``plugins/devin/config.example.json`` from this checkout with its hook paths filled in (plus the tools it
may use without asking, and nothing imported from other tools' configs), the second holds the
``switchboard_https``
entry with the throwaway broker's address and the run's token (mode 0600, in the run's temp directory). The
user's own Devin config is out of the way: ``XDG_CONFIG_HOME`` points at an empty temp directory, so the
user's hooks and server entry (which name a real server) never run here; the login (``~/.local/share/devin``)
is the real home's. The hooks get the address and token from the child's environment. The model is the
cheapest the account offers.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import screens
from .run import Run

REPO = Path(__file__).resolve().parents[3]
MODEL = "swe-1-6-slow"
TOOLS = ("join", "leave", "who", "say", "read", "wait", "pass", "away", "claim", "review", "status")
READ_CONFIG_FROM = ("agents_standard", "cursor", "windsurf", "claude", "copilot", "opencode", "zed")
ALLOW_EXTRA = ["Exec(sh hold.sh)"]


def project_config(plugin_dir: Path) -> dict:
    """``.devin/config.json``: the plugin's hook groups with its path, the allowed tools, no imports."""
    text = (
        (plugin_dir / "config.example.json")
        .read_text()
        .replace("/ABSOLUTE/PATH/TO/plugins/devin", str(plugin_dir))
    )
    cfg = json.loads(text)
    cfg["permissions"] = {"allow": [f"mcp__switchboard_https__{t}" for t in TOOLS] + ALLOW_EXTRA}
    cfg["read_config_from"] = {k: False for k in READ_CONFIG_FROM}
    return cfg


def argv(resume: bool) -> list[str]:
    # workspace trust off: the folder is the run's own scratch repository, and no trust entry is written
    return [
        "devin",
        "--model",
        MODEL,
        "--respect-workspace-trust",
        "false",
        *(["--continue"] if resume else []),
    ]


def mcp_config(server_url: str, token: str) -> dict:
    return {
        "mcpServers": {
            "switchboard_https": {
                "type": "http",
                "url": server_url + "/mcp",
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }


class DevinRun(Run):
    harness = "devin"
    tier = "devin:https"
    binaries = ("devin",)
    input_marker = "❭"
    wake_paths = frozenset({"wait"})
    # A message for a busy Devin comes as hook context after its next tool call, any tool's (#493): the
    # config's PostToolUse hooks cover every tool. Before that they ran after switchboard's own tools only,
    # and the message waited for the next switchboard call (its say() or wait() answer).
    # ``Run.mid_paths`` is ``{"hook_ctx"}``; a unit test holds it to what the hooks do.

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.quota_seen: list[float] = []  # the "N% remaining" Devin's banner showed at each start
        self.ready_since = 0.0

    def prepare(self) -> None:
        self.xdg = self.base / "xdg-config"
        self.xdg.mkdir(mode=0o700)
        cfg = self.ws / ".devin"
        self.write_private(
            cfg / "config.json", json.dumps(project_config(REPO / "plugins" / "devin"), indent=1)
        )
        self.write_private(
            cfg / "mcp_config.json", json.dumps(mcp_config(self.broker.url, self.token), indent=1)
        )

    def argv(self, resume: bool) -> list[str]:
        return argv(resume)

    def env(self) -> dict[str, str]:
        return {
            "XDG_CONFIG_HOME": str(self.xdg),
            "SWITCHBOARD_URL": self.broker.url,
            "SWITCHBOARD_AGENT_TOKEN": self.token,
        }

    def launch(self, resume: bool = False) -> None:
        self.ready_since = 0.0
        super().launch(resume)

    def ready_step(self, screen: str) -> bool:
        if not screens.devin_ready(screen):
            return False
        assert not screens.quota_exhausted(screen), "Devin's quota is used up:\n" + screen
        now = time.monotonic()
        self.ready_since = self.ready_since or now
        left = screens.quota_all(screen)
        if left and (not self.quota_seen or self.quota_seen[-1] != left[-1]):
            self.quota_seen.append(left[-1])
        # the banner with the quota comes a moment after the prompt: wait for it (at most 10 s) to report it
        return bool(left) or now - self.ready_since > 10

    def settled(self) -> bool:
        """Listening: its join turn is over and it is inside its wait() call (Members shows it busy)."""
        m = self.member()
        return bool(m and m["status"] == "busy" and screens.devin_waiting(self.tmux.capture(self.session)))

    def cost_note(self) -> str:
        left = self.quota_seen or screens.quota_all(self.final_screen)
        span = f"{left[0]:g}% at the first start, {left[-1]:g}% at the last" if left else "not shown"
        return f"{MODEL}: Devin's quota remaining {span}"

    def start_subagent(self) -> None:
        raise NotImplementedError
