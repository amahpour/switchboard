"""Live Claude Code signed in through the browser, with no token typed (DESIGN.md §49.11, #488). Test-only.

The plugin from this checkout, its server address the only setting, against a hosted broker whose public
URL is ``http://localhost:<port>`` (the one plain-http address Claude Code sends a sign-in's credentials to).
Nothing is pasted: the session's person signs it in through the browser, as a person does (``/mcp``, the
server, Authenticate), and a headless Chromium signed in as the admin plays that person on the consent card.

The CLI runs with a throwaway ``CLAUDE_CONFIG_DIR`` inside the run's temp directory, holding a 0600 copy of
only the account's login (never the real config's other secrets: its other servers' tokens or its plugins'
settings), so the sign-in Claude Code saves, its transcripts and the plugin's data never touch the real
``~/.claude``. The whole directory is removed with the run, the login copy first.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from . import screens
from .claude import ClaudeRun, settings_json
from .tmuxdrv import REAL_HOME
from .world import Broker, real_browsers


def login_only(credentials: dict[str, Any]) -> dict[str, Any]:
    """What of Claude Code's credentials file the run gets: the account's login and nothing else (the file
    also holds the tokens of every MCP server the person signed in to, and their plugins' secrets)."""
    return {"claudeAiOauth": credentials["claudeAiOauth"]}


def browser_missing() -> str | None:
    """Why the consent card can't be driven here (no Playwright, or no Chromium for it), or None."""
    try:
        from playwright.sync_api import sync_playwright

        with real_browsers(), sync_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():
                return "Playwright's Chromium is not installed (uv run playwright install chromium)"
    except ImportError:
        return "Playwright is not installed (uv sync)"
    return None


class ClaudeSigninRun(ClaudeRun):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.broker.cleanup()  # the plain broker Run made is not used: this one is hosted
        self.broker = Broker(self.harness, hosted=True)

    @property
    def claude_home(self) -> Path:
        return self.config

    def credentials(self) -> None:
        """Nothing is made: the sign-in makes the agent token."""

    def prepare(self) -> None:
        self.config = self.base / "claude-config"
        self.config.mkdir(mode=0o700)
        login = json.loads((Path(REAL_HOME) / ".claude" / ".credentials.json").read_text())
        self.write_private(self.config / ".credentials.json", json.dumps(login_only(login)))
        self.urls = self.base / "signin-urls"
        self.opener = self.base / "open-url"
        self.write_private(self.opener, f'#!/bin/sh\nprintf "%s\\n" "$1" >> "{self.urls}"\n')
        self.opener.chmod(0o700)
        self.settings = self.base / "claude-settings.json"
        self.write_private(self.settings, json.dumps(settings_json(self.broker.url, None)))

    def env(self) -> dict[str, str]:
        return {
            **super().env(),
            "CLAUDE_CONFIG_DIR": str(self.config),
            "BROWSER": str(self.opener),  # what Claude Code runs to open an address: ours writes it to a file
        }

    def ready_step(self, screen: str) -> bool:
        key = screens.claude_onboarding(screen)
        if key is not None:
            self.tmux.key(self.session, key)
            return False
        return super().ready_step(screen)

    def after_launch(self) -> None:
        self.sign_in()

    def sign_in(self) -> str:
        """What a person does to sign a plugin's server in: ``/mcp``, the server, Authenticate. Claude Code
        opens the sign-in address in the browser (``BROWSER`` is a script that writes it to a file) and waits
        for it to come back to its loopback address; the person's browser approves it on the consent card.
        Returns the address that was opened."""
        self.tmux.send_text(self.session, "/mcp")
        self.wait(lambda: "/mcp" in self.tmux.capture(self.session), 20, "/mcp to be typed")
        self.tmux.key(self.session, "Enter")
        self.press_until(
            "Enter",
            lambda s: "1. Authenticate" in s,
            lambda s: "needs authentication" in s,
            "the server's menu",
        )
        url = self.press_until(
            "Enter", lambda s: self.opened() is not None, lambda s: "1. Authenticate" in s, "the sign-in"
        )
        assert url is not None
        self.broker.approve_signin(url, self.device)
        self.wait(
            lambda: any(t["via"] == "oauth" for t in self.broker.tokens()),
            30,
            "the sign-in to make its token",
        )
        self.wait(
            lambda: "Authentication successful" in self.tmux.capture(self.session),
            30,
            "Claude Code to say it is signed in",
        )
        return str(url)

    def press_until(self, key: str, done: Any, waiting: Any, what: str, timeout: float = 40) -> str | None:
        """Press ``key`` until ``done(screen)`` holds. A key pressed before a menu has the focus is lost, so
        it is pressed again every few seconds, but only while the screen still shows what it is waiting at
        (``waiting(screen)``), never at one it has moved past. Returns the address Claude Code opened, if
        any."""
        last = [0.0]

        def step() -> bool:
            screen = self.tmux.capture(self.session)
            if done(screen):
                return True
            if waiting(screen) and time.monotonic() - last[0] > 3:
                self.tmux.key(self.session, key)
                last[0] = time.monotonic()
            return False

        self.wait(step, timeout, what)
        return self.opened()

    def opened(self) -> str | None:
        """The last address Claude Code asked the browser to open, or None."""
        try:
            seen = self.urls.read_text().split()
        except OSError:
            return None
        return seen[-1] if seen else None

    def key_file(self) -> Path:
        """The listener key's file in the plugin's data folder (named for the running Claude Code's pid)."""
        found = sorted((self.config / "plugins" / "data").glob("*/listener-key-*"))
        assert found, "no listener key file in the plugin's data folder"
        return found[-1]

    def scrub(self) -> None:
        shutil.rmtree(
            getattr(self, "config", self.base / "claude-config"), ignore_errors=True
        )  # the login first
