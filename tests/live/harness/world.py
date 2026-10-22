"""A throwaway broker, and the human at it, for the live tests (DESIGN.md §12.4). Test-only.

``switchboard start --foreground --test-mode`` in a temp home under the system temp dir (never a real
``~/.switchboard``), a person signed in through the same login link the CLI makes, and a token made
through the real web API (``POST /api/agent-tokens``, §49.1). The token is shown once; the driver keeps
it in memory and gives it to a child only through its environment or a 0600 file in the run's temp
directory, never on a command line.

``Broker(label, hosted=True)`` is a hosted broker (§30) whose public URL is ``http://localhost:<port>``,
the one plain-http address Claude Code will send a sign-in's credentials to (§49.11): its admin is set
up through the one-time password the test-mode broker writes to a file, and a browser signed in as that
admin (Playwright, headless) approves an agent's browser sign-in on the consent page (§49.10).
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx

from .tmuxdrv import REAL_HOME, clean_env

ROOM = "#build"
HUMAN = "alice"
REAL_PATH = os.environ.get("PATH", "/usr/bin:/bin")


def live_set() -> set[str]:
    return {x.strip() for x in os.environ.get("SWITCHBOARD_LIVE", "").split(",") if x.strip()}


def skip_reason(harness: str, *binaries: str) -> str | None:
    """Why this harness's live test doesn't run, or None. ``SWITCHBOARD_LIVE=claude,codex,devin,cursor``
    (or ``all``) opts in; the CLIs and tmux must be on PATH."""
    want = live_set()
    if harness not in want and "all" not in want:
        return f"set SWITCHBOARD_LIVE={harness} to run the live {harness} test"
    for b in (*binaries, "tmux"):
        if not shutil.which(b, path=REAL_PATH):
            return f"{b} is not on PATH"
    return None


@contextlib.contextmanager
def real_browsers() -> Iterator[None]:
    """Playwright looks for its browsers under ``$HOME``, which ``tests/conftest.py`` points at a temp
    directory: point it at the real home's cache for as long as a browser is wanted."""
    key = "PLAYWRIGHT_BROWSERS_PATH"
    before = os.environ.get(key)
    if before is None:
        os.environ[key] = str(Path(REAL_HOME) / ".cache" / "ms-playwright")
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(key, None)


def free_port() -> int:
    """A TCP port nothing listens on now (a hosted broker's public URL names its port before it starts)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Broker:
    def __init__(self, label: str, hosted: bool = False) -> None:
        self.hosted = hosted
        self.admin_password = ""
        self.base = Path(tempfile.mkdtemp(prefix=f"sb-live-{label}-"))
        self.home = self.base / "home"
        self.port = 0
        self.proc: subprocess.Popen[bytes] | None = None
        self.web: httpx.Client | None = None
        self.hdr: dict[str, str] = {}
        self.url = ""

    # ------------------------------------------------------------ start and stop
    def start(self) -> Broker:
        from switchboard.mcp.client import call_sync, ping
        from switchboard.paths import Paths

        self.home.mkdir(mode=0o700)
        (self.home / ".switchboard-test").touch()
        (self.home / "config.toml").write_text(f'human_name = "{HUMAN}"\n')
        env = {
            "PATH": REAL_PATH,
            "HOME": str(self.base),  # the broker never sees the real home
            "LANG": "en_US.UTF-8",
            "TMPDIR": str(self.base),
            "SWITCHBOARD_TEST": "1",
            "LOGNAME": HUMAN,
        }
        log = open(self.base / "broker.log", "ab")
        if self.hosted:
            self.port = free_port()
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "switchboard",
                "start",
                "--foreground",
                "--test-mode",
                "--test-trust-uds",
                "--home",
                str(self.home),
                "--port",
                str(self.port),
                *(["--public-url", f"http://localhost:{self.port}"] if self.hosted else []),
            ],  # fmt: skip
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        self.paths = Paths.from_home(self.home)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            info = ping(self.paths.sock, timeout=1.0)
            if info:
                self.port = info["port"]
                break
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        assert self.port, "the broker did not start:\n" + (self.base / "broker.log").read_text()
        self.url = (
            f"http://localhost:{self.port}" if self.hosted else f"http://switchboard.localhost:{self.port}"
        )
        self.hdr = {"Origin": self.url, "X-Switchboard": "1", "Content-Type": "application/json"}
        self.web = httpx.Client(base_url=self.url, timeout=10.0, follow_redirects=False)
        if self.hosted:
            self._set_up_admin()
        else:
            link = call_sync(self.paths.sock, "human.login_link")["url"]
            assert self.web.get(link.removeprefix(self.url)).status_code == 303
        r = self.web.post("/api/rooms", json={"name": ROOM}, headers=self.hdr)
        assert r.status_code == 200, r.text
        return self

    def _set_up_admin(self) -> None:
        """A hosted broker has no admin until someone uses the one-time password it prints; a test-mode broker
        also writes it to a file. Sign in with it and choose a password, so this driver is signed in as the
        admin and the consent page's "confirm it's you" has a password to check (§31.6)."""
        assert self.web is not None
        deadline = time.monotonic() + 15
        while not self.paths.test_claim_link.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        one_time = self.paths.test_claim_link.read_text().strip().split("#t=", 1)[1]
        r = self.web.post(
            "/api/signin/password", json={"name": "admin", "password": one_time}, headers=self.hdr
        )
        assert r.status_code == 200, r.text
        self.admin_password = secrets.token_urlsafe(12) + "-live-admin"
        r = self.web.post("/api/setup/password", json={"password": self.admin_password}, headers=self.hdr)
        assert r.status_code == 200, r.text

    def approve_signin(self, url: str, device: str, timeout: float = 30) -> None:
        """The person approves an agent's browser sign-in. A headless Chromium, signed in as the admin, opens
        the address the agent printed, names the device on the consent card and presses Approve (confirming
        it's the admin first if the last check is more than five minutes old). The browser then goes to the
        agent's own loopback address, which answers it, and the page has done its part."""
        from playwright.sync_api import sync_playwright

        assert self.hosted and self.web is not None
        cookie = self.web.cookies.get("switchboard_session")
        assert cookie, "the admin is not signed in"
        ms = int(timeout * 1000)
        with real_browsers(), sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                ctx = browser.new_context()
                ctx.add_cookies([{"name": "switchboard_session", "value": cookie, "url": self.url}])
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=ms)
                page.wait_for_selector("#step-consent:not(.hidden)", timeout=ms)
                page.fill("#consent-device", device)
                if page.locator("#consent-confirm:not(.hidden)").count():
                    page.fill("#consent-password", self.admin_password)
                with page.expect_navigation(url=lambda u: not u.startswith(self.url), timeout=ms):
                    page.click("#consent-approve")
            finally:
                browser.close()

    def stop(self) -> None:
        if self.web is not None:
            self.web.close()
        if self.proc is not None and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(5)

    def cleanup(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)

    # --------------------------------------------------------------- the human
    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        assert self.web is not None
        r = self.web.post(path, json=body, headers=self.hdr)
        assert r.status_code == 200, (path, r.status_code, r.text)
        return r.json()

    def say(self, text: str) -> int:
        """A message from the person, as the web page sends it; its id."""
        return int(self._post(f"/api/rooms/{ROOM.lstrip('#')}/say", {"text": text})["id"])

    def command(self, text: str) -> dict[str, Any]:
        return self._post(f"/api/rooms/{ROOM.lstrip('#')}/command", {"text": text})

    def make_token(self, device: str) -> tuple[int, str]:
        """(id, token) of a new agent token for ``device`` (the token is shown once)."""
        made = self._post("/api/agent-tokens", {"device": device})
        return int(made["id"]), str(made["token"])

    def tokens(self) -> list[dict[str, Any]]:
        assert self.web is not None
        r = self.web.get("/api/agent-tokens")
        assert r.status_code == 200, r.text
        return list(r.json()["tokens"])

    def revoke_token(self, token_id: int) -> dict[str, Any]:
        return self._post(f"/api/agent-tokens/{token_id}/revoke", {})

    def members(self) -> list[dict[str, Any]]:
        assert self.web is not None
        r = self.web.get(f"/api/rooms/{ROOM.lstrip('#')}/members")
        assert r.status_code == 200, r.text
        return list(r.json()["members"])

    def member(self, name: str) -> dict[str, Any] | None:
        return next((m for m in self.members() if m["name"] == name), None)

    def messages(self, after: int = 0) -> list[dict[str, Any]]:
        assert self.web is not None
        r = self.web.get(f"/api/rooms/{ROOM.lstrip('#')}/messages", params={"after": after, "limit": 500})
        assert r.status_code == 200, r.text
        return list(r.json()["messages"])

    def said_by(self, name: str, pattern: str, after: int = 0) -> bool:
        """An agent message from ``name`` after message ``after`` that contains ``pattern``."""
        return any(
            m["from"] == name and m["kind"] == "chat" and pattern in m["text"] for m in self.messages(after)
        )

    # ------------------------------------------------------------ the database
    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        """A read-only query of the broker's database (delivery paths and states, which no page shows)."""
        con = sqlite3.connect(f"file:{self.paths.db}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def delivery_of(self, message_id: int, name: str) -> sqlite3.Row | None:
        """How message ``message_id`` reached ``name``: the delivery's state, and its batch's path, kind."""
        rows = self.q(
            "SELECT d.state AS dstate, b.path, b.kind, b.state AS bstate, b.wake_kind"
            " FROM deliveries d JOIN memberships m ON m.id = d.membership_id"
            " LEFT JOIN batches b ON b.id = d.batch_id"
            " WHERE d.message_id = ? AND m.screen_name = ? ORDER BY m.id DESC",
            message_id,
            name,
        )
        return rows[0] if rows else None

    def log_tail(self, n: int = 20) -> str:
        try:
            return "\n".join((self.base / "broker.log").read_text(errors="replace").splitlines()[-n:])
        except OSError:
            return ""


def poll(fn: Callable[[], Any], timeout: float, step: float = 0.25) -> bool:
    """Whether ``fn`` turns truthy within ``timeout``; unlike ``wait_for`` it never fails."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(step)
    return False


def wait_for(
    fn: Callable[[], Any],
    timeout: float,
    what: str,
    step: float = 0.25,
    on_fail: Callable[[], str] | None = None,
) -> Any:
    """Poll ``fn`` until it returns something truthy; fail with ``what`` and the screen otherwise.
    A wait for an event with a deadline, never a sleep that means 'by now'."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(step)
    extra = on_fail() if on_fail else ""
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}\n{extra}")


__all__ = ["HUMAN", "REAL_PATH", "ROOM", "Broker", "clean_env", "live_set", "skip_reason", "wait_for"]
