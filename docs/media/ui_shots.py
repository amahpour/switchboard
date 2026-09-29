"""Screenshots of the web UI (issue #19) for the README, docs/USAGE.md and the PR.

    uv run python docs/media/ui_shots.py              # writes docs/media/ui/*.png
    uv run python docs/media/ui_shots.py --out /tmp/shots

Run by hand, never by pytest (it is not under tests/ and nothing imports it). It needs
Chrome or Chromium; everything else is the repo's own test tooling.

What it does:
- **A throwaway home.** It reuses the test suite's pieces (``tests/conftest.py``: the
  environment scrub, ``make_tmp_home``, ``InProcBroker``; ``tests/fakes/fake_agent.py``:
  ``FakeAgent``). Every switchboard file lives in a ``/tmp/yk-*`` home, HOME points at a temp
  dir, agent-harness variables are dropped, and ``~/.switchboard`` is never read or written.
  The broker runs in test mode (so scripted ``--harness test`` agents may join) and is
  switched out of it after the joins, so no TEST MODE band shows.
- **Seeded state, for the pictures only.** Three rooms (``#docs`` closed, to fill Closed
  rooms), a 0600 ``remotes.toml`` with an ``fpga-pi`` entry that is never enabled (so nothing
  dials), and four agents in ``#build``. Their tiers, approval modes, statuses and session ids
  are set through the store; their **harness and host** (and so their join lines) are
  rewritten with raw SQL, devin-1's parked reason is put straight into the engine, and the
  Codex adapter's tier refresh (which needs a live daemon) is switched off: none of these
  can happen to a scripted test agent. Two room notices (devin-1 parked, codex-1's
  approvals-off warning) are posted directly with the broker's own wording. Everything else
  (the messages, the hold, the close) goes through the real routes and MCP tools.
- **Headless Chrome over the DevTools protocol**, in a temporary profile with ``TZ=UTC``
  and no proxy (Chrome alone keeps your HOME: macOS Chrome can't load pages without it):
  sign in through a one-time link, wait for "Connected", and shoot at 1440x900 (DPR 2) and
  390x844 (mobile, DPR 3), light and dark via ``Emulation.setEmulatedMedia``.

Every wait polls a condition with a deadline; a timeout exits non-zero. The broker, the
agents and Chrome are stopped in ``finally``; the temp homes are removed unless ``--keep``.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "media" / "ui"
MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DESKTOP = (1440, 900, 2, False)  # width, height, device pixel ratio, mobile
PHONE = (390, 844, 3, True)
AGENTS = ("claude-1", "codex-1", "devin-1", "bench")
# (harness, tier, approval mode, status, host, session id): fixtures, not real sessions
PROFILE = {
    "claude-1": ("claude", "claude:inbox", "prompting", "idle", "", "3f2a91c4-7d2e-4b8a-9c1e-5a6b7c8dc91e"),
    "codex-1": ("codex", "codex:daemon", "bypass", "busy", "", "019a3c2e-55d1-7c40-a0b2-6e1f0c9d2a77"),
    "devin-1": ("devin", "devin:wait-loop", "prompting", "idle", "", "devin-7c1e2b9a4f"),
    "bench": ("claude", "claude:inbox", "prompting", "idle", "fpga-pi", "8b1d0e37-2c4a-4f19-b6d3-91e0a4c5f208"),
}
PARKED = "its turn ended without wait()"
# Chrome's own HOME: on macOS its network stack hangs under a HOME with no keychain, so Chrome
# alone keeps the real one (it still uses only its temporary --user-data-dir profile).
REAL_HOME = os.environ.get("HOME", "")
REMOTES_TOML = '[remote.fpga-pi]\nhost = "fpga-pi.local"\nuser = "alice"\nrooms = ["#build"]\n'


class Timeout(SystemExit):
    pass


def wait(pred: Any, timeout: float, what: str, step: float = 0.05) -> Any:
    """Poll ``pred`` until it returns something truthy; exit non-zero after ``timeout`` s."""
    deadline = time.monotonic() + timeout
    while True:
        v = pred()
        if v:
            return v
        if time.monotonic() > deadline:
            raise Timeout(f"ui_shots: timed out waiting for {what}")
        time.sleep(step)


# ------------------------------------------------------------------ isolation
def isolate() -> Path:
    """What tests/conftest.py's ``sanitize_env`` does, without pytest's fixtures: drop the
    agent-harness variables, point HOME at a temp dir and pin the human's name to alice."""
    for k in list(os.environ):
        if k.startswith(("CLAUDE", "CODEX_", "CURSOR_", "DEVIN_", "CHISEL_", "AI_AGENT")):
            del os.environ[k]
    fake_home = Path(tempfile.mkdtemp(prefix="yk-home-", dir="/tmp"))
    os.environ["HOME"] = str(fake_home)
    os.environ.pop("SWITCHBOARD_HOME", None)
    os.environ["SWITCHBOARD_TEST"] = "1"
    os.environ["TZ"] = "UTC"
    sys.path.insert(0, str(ROOT / "tests"))
    import switchboard.config

    switchboard.config.getpass = types.SimpleNamespace(getuser=lambda: "alice")  # type: ignore[assignment]
    return fake_home


# ------------------------------------------------------------------- seeding
def md_messages() -> dict[str, str]:
    """The conversation (after Chosen.dc and MarkdownSheet.dc): every Markdown construct the UI
    renders, plus a blocked link and raw HTML, which must show as inert text."""
    js_scheme = "java" + "script:"  # built at run time, so this file never holds the literal
    return {
        "ask": "@claude-1 add input validation to `parse_port`, then @codex-1 review it.",
        "plan": ("On it. Plan:\n\n1. Reject non-digits and values outside 0–65535\n"
                 "2. Keep `0` for 'pick a free port'\n3. Add a test for each edge"),
        "done": ("Done in `.worktrees/claude-1`:\n\n```python\ndef parse_port(s: str) -> int:\n"
                 "    if not s.isdigit():\n        raise ValueError(f\"not a port: {s!r}\")\n"
                 "    n = int(s)\n    if not 0 <= n <= 65535:\n"
                 "        raise ValueError(f\"out of range: {n}\")\n    return n\n```"),
        "review": ("Two issues:\n\n- `'٢'.isdigit()` is True (Arabic-Indic digits): use "
                   "`s.isascii() and s.isdigit()`\n- Leading zeros: `'0080'` passes. Intended?"),
        "devin": ("## Open questions\n\nPicking this up from:\n\n> @claude-1 add input validation to "
                  "`parse_port`, then @codex-1 review it.\n\n---\n\n"
                  f"Not a link: [CI run]({js_scheme}alert(document.cookie))\n\n"
                  "<script>alert(1)</script> stays text, as does <b>this</b>."),
        "flash": "@bench flash it once codex-1 signs off. Keep **`0`** as *pick a free port*.",
        "report": ("**Doing:** hardening `parse_port` in `.worktrees/claude-1`\n"
                   "**Decided:** `0` means *pick a free port*\n**Open questions:** leading zeros\n"
                   "**Next step:** flash once codex-1 signs off"),
        "table": ("Bitstream flashed; UART shows `PORT_OK 8080`.\n\n| input | result |\n|:--|:--|\n"
                  "| `'8080'` | ok |\n| `'0080'` | ok (see codex-1) |\n| `'٢'` | ValueError |\n\n"
                  "[CI run](https://github.com/example/switchboard/actions/runs/123)"),
    }


def start_broker(home: Path) -> Any:
    from conftest import InProcBroker
    from switchboard.config import Config

    # no say() rate limit and a roomier loop guard, so the scripted conversation posts in one go
    cfg = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0, rate_limit_s=0.0, hop_limit=30)
    return InProcBroker(home, cfg, test_mode=True).start()


def not_test_mode(b: Any) -> None:
    def off() -> None:
        b.state.test_mode = False
        b.state.info.test_mode = False
    b.on_loop(off)


async def seed(b: Any, agents: dict[str, Any]) -> None:
    import httpx

    web = b.web_client()
    h = b.write_headers()

    def ok(r: httpx.Response) -> dict[str, Any]:
        if r.status_code != 200:
            raise SystemExit(f"ui_shots: {r.request.method} {r.request.url.path}: {r.status_code} {r.text}")
        return r.json()

    for room in ("#build", "#fpga-bench", "#docs"):
        ok(web.post("/api/rooms", json={"name": room}, headers=h))
    for name, a in agents.items():
        await a.start()
        await a.join("#build", name)
    t = md_messages()

    def human(text: str) -> int:
        return ok(web.post("/api/rooms/build/say", json={"text": text}, headers=h))["id"]

    async def say(name: str, text: str, reply_to: int | None = None) -> int:
        r = await agents[name].say("#build", text, reply_to=reply_to)
        if not r.get("posted_id"):
            raise SystemExit(f"ui_shots: {name} could not post: {r}")
        return int(r["posted_id"])

    def notice(text: str, level: str | None = None) -> None:
        b.on_loop(lambda: b.state.service.post_notice(b.state.store.get_room("#build"), text, level=level))

    ask = human(t["ask"])
    await say("claude-1", t["plan"], reply_to=ask)
    done = await say("claude-1", t["done"])
    await say("codex-1", t["review"], reply_to=done)
    await say("devin-1", t["devin"])
    notice(f"devin-1 is parked — needs a poke ({PARKED})")
    notice("⚠ codex-1 runs with approvals off: what it reads (tool output, web pages) can steer it", "warn")
    human(t["flash"])
    await say("bench", t["report"])
    await say("bench", t["table"])
    ok(web.post("/api/rooms/docs/say", json={"text": "Draft the release notes."}, headers=h))
    ok(web.post("/api/rooms/docs/command", json={"text": "/close"}, headers=h))

    # The pictures' agents: a harness, tier, approval mode, status, host and session each. The
    # scripted agents make no more calls after this (a rewritten host or harness would no
    # longer match the MCP process that joined).
    def profile() -> None:
        from switchboard import db

        st = b.state
        s = st.store
        room = s.get_room("#build")
        for name, (harness, tier, mode, status, host, sid) in PROFILE.items():
            m = s.find_member(room.id, name)
            s.update_participant(m.participant_id, tier=tier, approval_mode=mode, session_id=sid)
            s.set_status(m.participant_id, status, "hook:PreToolUse" if status == "busy" else "hook:Stop")
            with db.tx(s.con):  # screenshots only: a test agent's harness and host never change
                s.con.execute("UPDATE participants SET harness=?, host=? WHERE id=?", (harness, host, m.participant_id))
                s.con.execute("UPDATE messages SET sender_harness=?, sender_host=? WHERE sender_membership_id=?",
                              (harness, host or None, m.membership_id))
                # its join line, as the broker words it for that harness and host
                where = f"{harness} on {host}" if host else harness
                s.con.execute("UPDATE messages SET text=? WHERE sender_membership_id=? AND kind='join'",
                              (f"joined ({where}, {tier})", m.membership_id))
            if name in ("claude-1", "bench"):
                # these two have "read" the room: nothing left for the engine to park them over
                # (a scripted agent has no wake path, so an idle one with a mention would park)
                with db.tx(s.con):
                    s.con.execute("UPDATE deliveries SET state='handled', handled_at=? WHERE membership_id=?"
                                  " AND state IN ('pending','offered','in_context')", (time.time(), m.membership_id))
                    s.con.execute("UPDATE batches SET state='cancelled' WHERE membership_id=? AND state='offered'",
                                  (m.membership_id,))
                st.engine.parked.pop(m.membership_id, None)
            if name == "devin-1":
                st.engine.parked[m.membership_id] = PARKED
        # the Codex adapter re-derives its members' tiers from the live daemon link, which this
        # throwaway broker doesn't have: keep the pictures' codex:daemon (screenshots only)
        st.engine.adapters["codex"].refresh_tiers = lambda: None
        st.hub.members_changed("#build")

    b.on_loop(profile)
    not_test_mode(b)
    web.close()


# ------------------------------------------------------------------- Chrome
class CDP:
    """The few DevTools protocol calls this needs, over the page's WebSocket (as capture.py)."""

    def __init__(self, ws_url: str) -> None:
        from websockets.sync.client import connect

        self._conn = connect(ws_url, max_size=None, open_timeout=10)
        self.ws = self._conn.__enter__()
        self.n = 0

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv(timeout=60))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expr: str) -> Any:
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(f"page script failed: {r['exceptionDetails'].get('text')}: {expr[:120]}")
        return r.get("result", {}).get("value")

    def close(self) -> None:
        self._conn.__exit__(None, None, None)


def find_chrome(explicit: str | None) -> str:
    for c in (explicit, shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser"),
              shutil.which("chrome"), MAC_CHROME):
        if c and os.path.exists(c):
            return c
    raise SystemExit("ui_shots: no Chrome or Chromium found (pass --chrome PATH)")


class Browser:
    def __init__(self, chrome: str, profile: Path) -> None:
        env = {**os.environ, "TZ": "UTC", "HOME": REAL_HOME or os.environ["HOME"]}
        self.proc = subprocess.Popen(
            [chrome, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}",
             "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--hide-scrollbars",
             "--disable-features=Translate", "--force-color-profile=srgb", "--no-proxy-server", "--window-size=1440,900", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=env)
        port_file = profile / "DevToolsActivePort"
        port = wait(lambda: port_file.exists() and (port_file.read_text().split() or [None])[0], 30, "Chrome")
        pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5))
        page = next(p for p in pages if p.get("type") == "page")
        self.cdp = CDP(page["webSocketDebuggerUrl"])
        self.cdp.call("Page.enable")
        self.cdp.call("Network.enable")
        self.view(DESKTOP)
        self.scheme("light")

    def close(self) -> None:
        try:
            self.cdp.close()
        finally:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                self.proc.kill()

    # -- page helpers
    def js(self, expr: str) -> Any:
        return self.cdp.eval(expr)

    def view(self, v: tuple[int, int, int, bool], height: int | None = None) -> None:
        w, h, dpr, mobile = v
        self.cdp.call("Emulation.setDeviceMetricsOverride", width=w, height=height or h,
                      deviceScaleFactor=dpr, mobile=mobile)
        self.cdp.call("Emulation.setTouchEmulationEnabled", enabled=mobile)

    def scheme(self, which: str) -> None:
        self.cdp.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": which}])

    def goto(self, url: str) -> None:
        self.cdp.call("Page.navigate", url=url)

    def until(self, expr: str, what: str, timeout: float = 20) -> Any:
        return wait(lambda: self.js(expr), timeout, what)

    def settle(self) -> None:
        """Fonts loaded, two frames drawn, and no CSS transition or animation still running."""
        self.js("document.fonts ? document.fonts.ready.then(() => true) : true")
        self.until("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r("
                   "document.getAnimations().every(a => a.playState !== 'running')))))",
                   "the page to settle", 10)

    def click(self, selector: str) -> None:
        sel = json.dumps(selector)
        self.until(f"!!document.querySelector({sel})", f"{selector} to exist")
        self.js(f"document.querySelector({sel}).click()")

    def visible(self, selector: str) -> str:
        sel = json.dumps(selector)
        return (f"(() => {{ const e = document.querySelector({sel}); return !!e && !e.classList.contains('hidden')"
                " && e.getClientRects().length > 0; })()")

    def type(self, text: str) -> None:
        self.js("document.getElementById('input').focus()")
        self.cdp.call("Input.insertText", text=text)

    def clear_input(self) -> None:
        self.cdp.call("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape",
                      windowsVirtualKeyCode=27, nativeVirtualKeyCode=27)
        self.cdp.call("Input.dispatchKeyEvent", type="keyUp", key="Escape", code="Escape",
                      windowsVirtualKeyCode=27, nativeVirtualKeyCode=27)
        self.js("(() => { const i = document.getElementById('input'); i.value = '';"
                " i.dispatchEvent(new Event('input', {bubbles: true})); i.blur(); return true; })()")

    def shot(self, out: Path, name: str, clip: dict[str, float] | None = None) -> None:
        self.settle()
        params: dict[str, Any] = {"format": "png"}
        if clip:
            params["clip"] = {**clip, "scale": 1}
        data = self.cdp.call("Page.captureScreenshot", **params)["data"]
        path = out / name
        path.write_bytes(base64.b64decode(data))
        print(path)


# ------------------------------------------------------------------- shots
def sign_in(br: Browser, b: Any) -> None:
    br.goto(b.login_url())
    br.until("(document.getElementById('st-conn') || {}).textContent === 'Connected'", "the web UI to connect")


def open_build(br: Browser, n_chat: int) -> None:
    br.js("location.hash = 'build'; true")
    br.until("(document.getElementById('room-title') || {}).textContent === 'build'", "#build to open")
    br.until(f"document.querySelectorAll('#log .line.k-chat').length >= {n_chat}", "the #build history")
    br.until("document.querySelectorAll('#buddy-list .member').length === 4", "the four members")


def fresh(br: Browser, n_chat: int) -> None:
    """Reload the page and reopen #build: every shot after a tall crop, a sheet or a phone
    layout starts from a clean page (headless Chrome can otherwise leave the right pane
    unpainted after those), with the log scrolled to its end."""
    br.cdp.call("Page.reload", ignoreCache=True)
    br.until("(document.getElementById('st-conn') || {}).textContent === 'Connected'", "the web UI to reconnect")
    open_build(br, n_chat)


def inspect(br: Browser, name: str) -> None:
    br.click(f'#buddy-list .member[data-name="{name}"]')
    br.until(f"(document.getElementById('insp-name') || {{}}).textContent.includes({json.dumps(name)})",
             f"the Inspector for {name}")
    # the detail GET has answered (its response is what fills the session and timeline)
    br.until("performance.getEntriesByType('resource').some(e => e.name.includes("
             f"'/members/{name}') && e.responseEnd > 0)", f"{name}'s detail")


def back(br: Browser) -> None:
    br.click("#insp-back")
    br.until("!document.getElementById('pane').classList.contains('inspecting')", "the Members view")


def log_clip(br: Browser) -> dict[str, float]:
    return br.js("(() => { const log = document.getElementById('log'), rows = [...log.children]"
                 ".filter(e => e.classList.contains('line') || e.classList.contains('day'));"
                 " const L = log.getBoundingClientRect(), a = rows[0].getBoundingClientRect(),"
                 " z = rows[rows.length - 1].getBoundingClientRect();"
                 " return {x: L.left, y: Math.max(0, a.top - 12), width: L.width,"
                 " height: z.bottom + 12 - Math.max(0, a.top - 12)}; })()")


def shoot(chrome: str, profile: Path, out: Path, b: Any, empty: Any, n_chat: int) -> None:
    # signed in before the browser is: a sign-in posts a live "new web login" notice (and a
    # CLI-issued link another), which the open page would show in the log
    web = b.web_client()
    br = Browser(chrome, profile)
    try:
        sign_in(br, b)
        open_build(br, n_chat)
        br.shot(out, "desktop-light.png")

        br.view(DESKTOP, height=2400)  # tall enough for the whole conversation, then cropped to it
        br.shot(out, "markdown-light.png", log_clip(br))
        br.view(DESKTOP)

        br.type("/")
        br.until(br.visible("#palette"), "the command palette")
        br.shot(out, "palette-light.png")
        br.clear_input()
        br.type("@co")
        br.until(br.visible("#mentions"), "the mention list")
        br.shot(out, "mention-light.png")
        br.clear_input()

        br.click("#closed-rooms")
        br.until(br.visible("#closed-panel") + " && !!document.querySelector('#closed-body .closed-card')",
                 "the Closed rooms sheet")
        br.shot(out, "closed-light.png")
        br.click("#closed-close")
        br.click("#remotes .remote")
        br.until(br.visible("#remotes-panel") + " && !!document.querySelector('#remotes-body .remote-card')",
                 "the remotes sheet")
        br.shot(out, "remotes-light.png")
        br.click("#remotes-close")

        fresh(br, n_chat)
        inspect(br, "bench")
        br.shot(out, "inspector-remote-light.png")
        back(br)

        br.scheme("dark")
        fresh(br, n_chat)
        br.shot(out, "desktop-dark.png")
        br.view(DESKTOP, height=2400)
        br.shot(out, "markdown-dark.png", log_clip(br))
        br.view(DESKTOP)
        fresh(br, n_chat)
        inspect(br, "devin-1")
        br.shot(out, "inspector-dark-parked.png")
        back(br)

        br.scheme("light")
        br.view(PHONE)
        br.shot(out, "phone-light.png")
        br.scheme("dark")
        br.click("#buddy-toggle")
        br.until("document.getElementById('app').classList.contains('sheet-open')", "the Members sheet")
        br.shot(out, "phone-dark-sheet.png")
        br.scheme("light")
        br.view(DESKTOP)
        fresh(br, n_chat)

        # /hold is shown in one shot (the Inspector's Held chip and Release button), then released
        hdr = b.write_headers()
        assert web.post("/api/rooms/build/command", json={"text": "/hold codex-1"}, headers=hdr).status_code == 200
        br.until("[...document.querySelectorAll('#buddy-list .member')].some(e => e.dataset.name === 'codex-1'"
                 " && /held/i.test(e.textContent))", "codex-1 held")
        inspect(br, "codex-1")
        br.click("#insp-body .queue-toggle")
        br.until(br.visible("#insp-queue"), "the queued list")
        br.shot(out, "inspector-light.png")
        web.post("/api/rooms/build/command", json={"text": "/release codex-1"}, headers=hdr)

        # first run: a second broker with no rooms (its session cookie replaces the first's)
        sign_in(br, empty)
        br.until(br.visible("#empty"), "the Welcome view")
        br.shot(out, "welcome-light.png")

        # the sign-in page: no cookie at all
        br.cdp.call("Network.clearBrowserCookies")
        br.goto(empty.base + "/")
        br.until("document.body && document.body.textContent.includes('switchboard login')", "the sign-in page")
        br.shot(out, "login-light.png")
    finally:
        br.close()
        web.close()


# -------------------------------------------------------------------- main
async def run(args: argparse.Namespace) -> None:
    from conftest import make_tmp_home
    from fakes.fake_agent import FakeAgent

    chrome = find_chrome(args.chrome)
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    home, home2 = make_tmp_home(), make_tmp_home()
    profile = Path(tempfile.mkdtemp(prefix="yk-chrome-", dir="/tmp"))
    rt = home / "remotes.toml"
    fd = os.open(rt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(REMOTES_TOML)
    b = empty = None
    agents = {n: FakeAgent(home, f"shots-{n}") for n in AGENTS}
    try:
        b = start_broker(home)
        await seed(b, agents)
        empty = start_broker(home2)
        not_test_mode(empty)
        n_chat = len(md_messages())
        # the browser part is synchronous: run it off the event loop, which keeps the agents'
        # MCP sessions (and so their memberships) alive
        await asyncio.to_thread(shoot, chrome, profile, out, b, empty, n_chat)
    finally:
        for a in agents.values():
            await a.close()
        for x in (b, empty):
            if x is not None:
                x.stop()
        if not args.keep:
            for d in (home, home2, profile):
                shutil.rmtree(d, ignore_errors=True)
        else:
            print(f"kept: {home} {home2} {profile}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(OUT), help="output directory (default docs/media/ui)")
    ap.add_argument("--chrome", help="the Chrome or Chromium binary (default: found on PATH or in /Applications)")
    ap.add_argument("--keep", action="store_true", help="keep the temp homes and Chrome profile")
    args = ap.parse_args()
    fake_home = isolate()
    try:
        asyncio.run(run(args))
    finally:
        shutil.rmtree(fake_home, ignore_errors=True)


if __name__ == "__main__":
    main()
