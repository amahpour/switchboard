"""Live Grok Build over HTTPS (DESIGN.md §12.4, §49.15). Test-only; the product never writes these files.

Grok Build runs with a **temp $HOME**, the run's scratch repository itself, so the person's own ``~/.grok``
(config, plugins, hooks, trusted folders, sessions) is never read or written, and Grok asks for no folder trust:
it trusts the home folder without asking, so the run passes no ``--trust`` and writes no ``trusted_folders.toml``.
In it, ``.grok/plugins/switchboard`` is a copy of this checkout's ``plugins/grok`` and ``.grok/config.toml``
enables it, starts in ask mode, and allows switchboard's tools (``MCPTool(switchboard__*)``) and the scratch
script (``Bash(sh hold.sh *)``) and nothing else, as the Cursor profile does; any other prompt is declined with
Esc, never approved. The address and the token reach the plugin through the child's environment only.

The model is the person's real Grok login by default: a copy of ``~/.grok/auth.json`` (mode 0600, deleted
first when the run ends), or ``XAI_API_KEY`` if it is set. With ``SWITCHBOARD_GROK_BASE_URL`` set, the model is
that chat-completions server instead (Grok's ``[model.<id>] base_url``); ``standin`` starts the scripted one in
``tests/fakes/grok_model.py``, which needs no login and spends nothing.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import screens
from .run import Run
from .tmuxdrv import REAL_HOME

REPO = Path(__file__).resolve().parents[3]
STANDIN = "standin"  # SWITCHBOARD_GROK_BASE_URL=standin: the scripted stand-in model
MODEL_ID = "switchboard-standin"
ALLOW = ["MCPTool(switchboard__*)", "Bash(sh hold.sh *)"]
# the approval scenario's command: Grok runs a harmless ``touch`` in its folder without asking, even in ask
# mode, so a rule makes it ask (an ``ask`` rule wins over every ``allow``)
ASK = ["Bash(touch approval-was-given)"]


def plugin_dir() -> Path:
    return Path(os.environ.get("SWITCHBOARD_LIVE_GROK_PLUGIN") or REPO / "plugins" / "grok")


def config_toml(base_url: str | None) -> str:
    """The temp home's ``.grok/config.toml``: the plugin on, ask mode, the run's allow rules, and the stand-in
    model when there is one."""
    allow = ", ".join(f'"{rule}"' for rule in ALLOW)
    ask = ", ".join(f'"{rule}"' for rule in ASK)
    text = (
        f'[plugins]\nenabled = ["switchboard"]\n\n[permission]\nallow = [{allow}]\nask = [{ask}]\n\n'
        '[ui]\npermission_mode = "ask"\n'
    )
    if base_url:
        text += (
            f'\n[model.{MODEL_ID}]\nbase_url = "{base_url}"\napi_key = "standin"\ncontext_window = 128000\n'
        )
    return text


class GrokRun(Run):
    harness = "grok"
    tier = "grok:https"
    binaries = ("grok",)
    quit_command = "/quit"
    input_marker = "❯"

    def prepare(self) -> None:
        self.standin = None
        base_url = os.environ.get("SWITCHBOARD_GROK_BASE_URL") or None
        if base_url == STANDIN:
            sys.path.insert(0, str(REPO / "tests"))
            from fakes.grok_model import GrokModel

            self.standin = GrokModel().start()
            base_url = self.standin.url
        self.base_url = base_url
        grok = self.ws / ".grok"
        shutil.copytree(plugin_dir(), grok / "plugins" / "switchboard")
        (grok / "config.toml").write_text(config_toml(base_url))
        if not base_url:
            auth = Path(REAL_HOME) / ".grok" / "auth.json"
            if auth.exists():
                self.write_private(grok / "auth.json", auth.read_text())

    def env(self) -> dict[str, str]:
        env = {
            "HOME": str(self.ws),
            "SWITCHBOARD_URL": self.broker.url,
            "SWITCHBOARD_AGENT_TOKEN": self.token,
            "NO_PROXY": "switchboard.localhost,localhost,127.0.0.1",
            "GROK_AUTO_UPDATE": "0",
        }
        if not self.base_url and os.environ.get("XAI_API_KEY"):
            env["XAI_API_KEY"] = os.environ["XAI_API_KEY"]
        return env

    def argv(self, resume: bool) -> list[str]:
        model = ["-m", MODEL_ID] if self.base_url else []
        return ["grok", *model, *(["-c"] if resume else [])]

    def ready_step(self, screen: str) -> bool:
        assert not screens.quota_exhausted(screen), "Grok's usage limit is hit:\n" + screen
        assert "Do you trust the contents" not in screen, "Grok asked for folder trust:\n" + screen
        return "❯" in screen and ("Shift+Tab:mode" in screen or "ctrl+q" in screen.lower())

    def type(self, text: str) -> None:
        """While switchboard listens, Grok's turn is still running (its Stop hook is held), so Enter only
        queues a prompt (``#1``) until the turn ends; Enter again on the empty box sends it now, which ends the
        held hook. That is what a person does, and the guide says so."""
        super().type(text)
        if "Enter:send now" in self.tmux.capture(self.session):
            self.tmux.key(self.session, "Enter")

    def decline_stray_prompt(self) -> None:
        """Grok's prompt takes Esc as "scrollback", not a decline: Ctrl+C cancels it (the turn ends with
        StopCancelled), and never approves anything."""
        if screens.approval_open(self.harness, self.tmux.capture(self.session)):
            self.decline()
            self.stray_declined += 1

    def decline(self) -> None:
        self.tmux.key(self.session, "C-c")

    def input_text(self, screen: str) -> str:
        return screens.grok_input_text(screen)

    def start_subagent(self) -> None:
        raise NotImplementedError

    def cost_note(self) -> str:
        if self.standin is not None:
            return f"none: a stand-in model ({len(self.standin.requests)} requests)"
        return "the person's Grok plan: see the usage page of their xAI account"

    def scrub(self) -> None:
        """Delete the copy of the login first, before anything can fail; stop the stand-in."""
        (self.ws / ".grok" / "auth.json").unlink(missing_ok=True)
        if getattr(self, "standin", None) is not None:
            self.standin.stop()
