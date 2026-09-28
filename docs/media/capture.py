"""Capture the raw material for the README's getting-started video, from a real run.

Nothing here is mocked. A private tmux server plays the human at three terminals, as
tests/live does: one installs switchboard from the release tag into a throwaway HOME,
registers it and starts the broker; the other two run a real Claude Code session and a
real Codex session. Headless Chrome, driven over the DevTools protocol, signs in with the
link the broker printed and creates #build. Both agents join; alice asks claude-1 for a
change and a review from codex-1, and the two agents take it from there. Terminal screens
and browser screenshots are recorded with their times; render.py turns them into the video.

    uv run python docs/media/capture.py --build /tmp/sb-media

Isolation: switchboard (its install, config, broker, database and web session) lives in
a throwaway HOME under /tmp/sb-demo, and the agents work in a scratch git repo there.
Claude Code and Codex run with your own logins but only with per-launch settings, as the
live tests run them: Claude with --setting-sources project,local --strict-mcp-config,
accept-edits and switchboard's tools; Codex on a private app-server (never your daemon)
with -c overrides only (approvals on request, a workspace-write sandbox, your MCP servers,
plugins and hooks off). tests/live/harness/drift.py checks your harness config before and
after. The sessions still land in your Claude Code and Codex histories, like any live
test. The sign-in token is never written to disk.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "live"))

from harness import codex_profile, codex_trust, drift, profiles  # noqa: E402
from harness.tmuxdrv import REAL_HOME, Tmux, clean_env  # noqa: E402
from websockets.sync.client import connect  # noqa: E402

TAG = "v0.3.0"
DEMO = Path("/tmp/sb-demo")
ALICE = DEMO / "alice"            # the throwaway HOME
PROJECT = ALICE / "project"       # the repo the agents work in
CODEX_SOCK = DEMO / "cx.sock"     # the private Codex app-server (short: sun_path)
TERM_W, TERM_H = 84, 26
VIEW_W, VIEW_H, DPR = 860, 540, 2
TASK = ("@claude-1 please add input validation to parse_port (a port is 1 to 65535, anything else raises "
        "ValueError), then ask codex-1 to review it. Keep your messages short.")
TOKEN_RE = re.compile(r"(login\?t=)[A-Za-z0-9_-]+")
ANSI_RE = re.compile(r"\x1b\[[0-9;:]*[A-Za-z]")
CODEX_READY = "Ask Codex to do anything"
# with accept-edits, Claude may not edit any harness's project config or git's own files (as in m7_demo.py)
CLAUDE_DENY = ["Edit(.claude/**)", "Edit(.devin/**)", "Edit(.codex/**)", "Edit(.git/**)"]

PORTPARSE = '''"""Parse a TCP port from user input."""


def parse_port(text: str) -> int:
    """Return the port number in ``text``, e.g. "8080" -> 8080."""
    return int(text.strip())
'''
README = "# portparse\n\nA tiny library that parses TCP port numbers from user input.\n"


def log(*a: Any) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def redact(screen: str) -> str:
    """Hide the sign-in token, including the tail of a link that wrapped onto the next line."""
    lines = screen.split("\n")
    for i, ln in enumerate(lines):
        if "login?t=" in ln:
            lines[i] = TOKEN_RE.sub(r"\1••••••••", ln)
            if i + 1 < len(lines) and re.fullmatch(r"[A-Za-z0-9_-]+", ANSI_RE.sub("", lines[i + 1]).strip()):
                lines[i + 1] = ""
    return "\n".join(lines)


def q(a: str) -> str:
    return "'" + a.replace("'", "'\\''") + "'"


class CDP:
    """The few DevTools protocol calls this needs, over the page's WebSocket."""

    def __init__(self, ws_url: str) -> None:
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
        return r.get("result", {}).get("value")

    def close(self) -> None:
        self._conn.__exit__(None, None, None)


class Recorder:
    """Every terminal screen change and every screenshot, with its time, in timeline.jsonl."""

    def __init__(self, build: Path, tmux: Tmux) -> None:
        self.build, self.tmux = build, tmux
        (build / "shots").mkdir(parents=True, exist_ok=True)
        self.f = open(build / "timeline.jsonl", "w")
        self.t0 = time.monotonic()
        self.scene = "setup"
        self.panes: list[str] = []
        self.last: dict[str, str] = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.n = 0
        self.thread = threading.Thread(target=self._poll, daemon=True)
        self.thread.start()

    def now(self) -> float:
        return round(time.monotonic() - self.t0, 3)

    def write(self, rec: dict[str, Any]) -> None:
        with self.lock:
            self.f.write(json.dumps({"t": self.now(), "scene": self.scene, **rec}) + "\n")
            self.f.flush()

    def mark(self, what: str, **extra: Any) -> None:
        self.write({"kind": "mark", "what": what, **extra})

    def _poll(self) -> None:
        while not self.stop.is_set():
            for pane in list(self.panes):
                r = self.tmux.run("capture-pane", "-e", "-p", "-t", pane)
                if r.returncode != 0:
                    continue
                text = redact(r.stdout)
                if self.last.get(pane) != text:
                    self.last[pane] = text
                    self.write({"kind": "term", "pane": pane, "text": text})
            time.sleep(0.15)

    def shot(self, cdp: CDP, label: str = "") -> None:
        self.n += 1
        name = f"shots/{self.n:04d}.png"
        data = cdp.call("Page.captureScreenshot", format="png")["data"]
        (self.build / name).write_bytes(base64.b64decode(data))
        self.write({"kind": "shot", "file": name, "label": label})

    def close(self) -> None:
        self.stop.set()
        self.thread.join(5)
        self.f.close()


def wait(pred: Any, timeout: float, what: str, step: float = 0.25, during: Any = None) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        if during is not None:
            during()
        time.sleep(step)
    raise TimeoutError(f"timed out waiting for {what}")


class Demo:
    def __init__(self, build: Path, claude_model: str) -> None:
        self.build = build
        self.claude_model = claude_model
        self.path = os.environ.get("PATH", "/usr/bin:/bin")
        self.claude = shutil.which("claude") or sys.exit("claude is not on PATH")
        self.codex = os.path.realpath(shutil.which("codex") or sys.exit("codex is not on PATH"))
        self.chrome = shutil.which("google-chrome") or shutil.which("chromium") or sys.exit("no Chrome")
        uv = shutil.which("uv") or sys.exit("uv is not on PATH")
        self.tmux = Tmux("media", self.path)
        self.rec = Recorder(build, self.tmux)
        self.drift0 = drift.snapshot()
        self.codex_cfg0 = drift.codex_config()
        self.chrome_proc: subprocess.Popen[bytes] | None = None
        self.app_server: subprocess.Popen[bytes] | None = None
        self.cdp: CDP | None = None
        # the agents' CLIs, under their own names, from a folder of links (so no real path shows)
        self.bin = DEMO / "bin"
        # alice's terminal: her HOME, her ~/.local/bin first; uv's caches are shared with yours
        self.alice_env = clean_env(
            f"{ALICE}/.local/bin:{self.bin}:{Path(uv).parent}:/usr/bin:/bin",
            HOME=str(ALICE), USER="alice", LOGNAME="alice",
            UV_CACHE_DIR=f"{REAL_HOME}/.cache/uv", UV_PYTHON_INSTALL_DIR=f"{REAL_HOME}/.local/share/uv/python",
            PS1="$ ")

    # --------------------------------------------------------------- setup
    def setup(self) -> None:
        if DEMO.exists():
            if DEMO.lstat().st_uid != os.getuid() or DEMO.is_symlink():
                sys.exit(f"{DEMO} exists and isn't yours; remove it first")
            subprocess.run(["pkill", "-f", str(DEMO)], check=False)
            time.sleep(0.5)
            shutil.rmtree(DEMO)
        PROJECT.mkdir(parents=True, mode=0o700)
        os.chmod(DEMO, 0o700)
        self.bin.mkdir()
        (self.bin / "claude").symlink_to(os.path.realpath(self.claude))
        (self.bin / "codex").symlink_to(self.codex)
        (PROJECT / "portparse.py").write_text(PORTPARSE)
        (PROJECT / "README.md").write_text(README)
        genv = clean_env(self.path, GIT_AUTHOR_NAME="alice", GIT_AUTHOR_EMAIL="alice@example.com",
                         GIT_COMMITTER_NAME="alice", GIT_COMMITTER_EMAIL="alice@example.com")
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "portparse"]):
            subprocess.run(cmd, cwd=PROJECT, env=genv, check=True)
        # the broker reads Claude's session registry from your real Claude home, and talks to
        # the private Codex app-server, never your daemon
        sb = ALICE / ".switchboard"
        sb.mkdir(mode=0o700)
        (sb / "config.toml").write_text(
            f'human_name = "alice"\n[claude]\nsessions_dir = "{REAL_HOME}/.claude/sessions"\n'
            f'[codex]\ncontrol_socket = "{CODEX_SOCK}"\nbin = "{self.codex}"\n')
        os.chmod(sb / "config.toml", 0o600)

    def bash(self, name: str, env: dict[str, str], cwd: Path, rcfile: Path | None = None) -> None:
        argv = ["bash", "--noprofile"] + (["--rcfile", str(rcfile)] if rcfile else ["--norc"]) + ["-i"]
        self.tmux.new_session(name, str(cwd), env, argv, width=TERM_W, height=TERM_H)
        self.rec.panes.append(name)
        wait(lambda: self.screen(name).rstrip().endswith("$"), 10, f"{name} prompt")
        self.tmux.run("send-keys", "-t", name, "clear", "Enter")
        time.sleep(0.4)

    def screen(self, name: str) -> str:
        return self.tmux.capture(name)

    def joined(self, name: str) -> str:
        """The screen with wrapped lines joined (a sign-in link can be wider than the pane)."""
        return self.tmux.run("capture-pane", "-p", "-J", "-t", name).stdout

    def type(self, name: str, text: str, enter: bool = True, cps: float = 18) -> None:
        for ch in text:
            self.tmux.send_text(name, ch)
            time.sleep(1 / cps)
        if enter:
            time.sleep(0.35)
            self.tmux.key(name, "Enter")

    # ------------------------------------------------------ scene: install
    def scene_install(self) -> str:
        self.rec.scene = "install"
        self.bash("term", self.alice_env, ALICE)
        self.rec.mark("start")
        self.type("term", f"uv tool install git+https://github.com/amahpour/switchboard@{TAG}")
        wait(lambda: "Installed 1 executable" in self.screen("term"), 300, "uv tool install")
        time.sleep(1.2)
        self.rec.scene = "register"
        self.tmux.run("send-keys", "-t", "term", "clear", "Enter")
        time.sleep(0.4)
        self.type("term", "switchboard install all")
        wait(lambda: "Apply? [y/N]" in self.screen("term"), 60, "the install diff")
        time.sleep(2.0)
        self.type("term", "y")
        wait(lambda: re.search(r"^  devin: ", self.screen("term"), re.M), 60, "the install summary")
        s = self.screen("term")
        if not (re.search(r"^  claude: installed", s, re.M) and re.search(r"^  codex: installed", s, re.M)):
            raise RuntimeError("switchboard install all failed:\n" + s)
        time.sleep(1.2)
        self.rec.scene = "setup-codex"
        self.codex_server()
        self.rec.scene = "start"
        self.tmux.run("send-keys", "-t", "term", "clear", "Enter")
        time.sleep(0.4)
        self.type("term", "switchboard start")
        m = wait(lambda: re.search(r"http://switchboard\.localhost:\d+/login\?t=[A-Za-z0-9_-]{20,}",
                                   self.joined("term")), 30, "the sign-in link")
        time.sleep(1.5)
        return m.group(0)

    # ------------------------------------------------------- scene: browser
    def open_browser(self) -> None:
        prof = self.build / "chrome-profile"
        shutil.rmtree(prof, ignore_errors=True)
        self.chrome_proc = subprocess.Popen(
            [self.chrome, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={prof}",
             "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--hide-scrollbars",
             "--disable-features=Translate", f"--window-size={VIEW_W},{VIEW_H}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        port = wait(lambda: (prof / "DevToolsActivePort").exists()
                    and ((prof / "DevToolsActivePort").read_text().split() or [None])[0], 20, "Chrome")
        pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5))
        page = next(p for p in pages if p.get("type") == "page")
        self.cdp = CDP(page["webSocketDebuggerUrl"])
        self.cdp.call("Page.enable")
        self.cdp.call("Emulation.setDeviceMetricsOverride", width=VIEW_W, height=VIEW_H,
                      deviceScaleFactor=DPR, mobile=False)

    def js(self, expr: str) -> Any:
        assert self.cdp is not None
        return self.cdp.eval(expr)

    def shot(self, label: str = "") -> None:
        assert self.cdp is not None
        self.rec.shot(self.cdp, label)

    def buddies(self) -> str:
        return self.js("document.getElementById('buddy-list').textContent") or ""

    def scene_signin(self, link: str) -> None:
        self.rec.scene = "signin"
        assert self.cdp is not None
        self.cdp.call("Page.navigate", url=link)
        wait(lambda: self.js("document.getElementById('st-conn') && "
                             "document.getElementById('st-conn').textContent") == "online", 20, "the web UI")
        time.sleep(0.8)
        self.shot("signed-in")
        time.sleep(0.6)
        self.js("document.getElementById('create-build').click()")
        wait(lambda: self.js("[...document.querySelectorAll('#tabs [role=tab]')]"
                             ".some(t => t.textContent.includes('#build'))"), 10, "#build")
        time.sleep(0.8)
        self.shot("room")

    # -------------------------------------------------------- the agents
    def print_args(self, harness: str) -> dict[str, Any]:
        pa = subprocess.run([str(ALICE / ".local/bin/switchboard"), "install", harness, "--print-args",
                             "--home", str(ALICE / ".switchboard")], env=self.alice_env,
                            capture_output=True, text=True, timeout=30, check=True)
        return json.loads(pa.stdout)

    def claude_pane(self) -> None:
        pa = self.print_args("claude")
        (self.build / "mcp.json").write_text(json.dumps(profiles.claude_mcp_config(pa)))
        settings = profiles.claude_settings(pa, None)
        settings["permissions"] = {"allow": [], "deny": list(profiles.TEST_DENY) + CLAUDE_DENY}
        (self.build / "settings.json").write_text(json.dumps(settings))
        argv = [str(self.bin / "claude"), "--model", self.claude_model, "--setting-sources", "project,local",
                "--strict-mcp-config", "--permission-mode", "acceptEdits",
                "--allowedTools", ",".join(profiles.SWITCHBOARD_TOOLS),
                "--mcp-config", str(self.build / "mcp.json"), "--settings", str(self.build / "settings.json")]
        # the viewer sees "claude"; the flags that keep your own config out stay in this function
        rc = self.build / "claude-rc.sh"
        rc.write_text("PS1='$ '\nclaude() { " + " ".join(q(a) for a in argv) + ' "$@"; }\n')
        self.bash("claude", clean_env(self.path, DISABLE_AUTOUPDATER="1"), PROJECT, rcfile=rc)

    def codex_server(self) -> None:
        """Codex's private app-server, before the broker starts (so its Codex link is up at once)."""
        pa = self.print_args("codex")
        (PROJECT / ".codex").mkdir()
        (PROJECT / ".codex" / "hooks.json").write_text(pa["files"]["hooks.json"])
        overrides = codex_profile.base_overrides(PROJECT, pa)
        hooks_state, hooks = codex_trust.hooks_state(self.codex, self.path, PROJECT, overrides)
        assert len([h for h in hooks if h["source"] == "project"]) == 5, hooks
        ov = overrides + ["-c", f"hooks.state={hooks_state}"]
        [res] = codex_trust.stdio_calls(self.codex, self.path, PROJECT, ov,
                                        [("config/read", {"cwd": str(PROJECT), "includeLayers": False})])
        cfg = res["result"]["config"]
        assert cfg.get("approval_policy") == "on-request", "Codex approvals must prompt"
        assert cfg.get("sandbox_mode") == "workspace-write", "Codex must run in the workspace-write sandbox"
        on = [k for k, v in (cfg.get("mcp_servers") or {}).items() if not isinstance(v, dict) or v.get("enabled", True)]
        assert on == ["switchboard"], "other MCP servers are enabled"
        out = open(self.build / "app-server.out", "ab")
        self.app_server = subprocess.Popen(
            codex_profile.app_server_argv(self.codex, str(CODEX_SOCK), overrides, hooks_state),
            cwd=str(PROJECT), env=clean_env(self.path), stdin=subprocess.DEVNULL, stdout=out, stderr=out,
            start_new_session=True)
        wait(self.app_server_answers, 30, "the private Codex app-server", step=0.3)

    def codex_pane(self) -> None:
        tui = codex_profile.tui_argv(str(self.bin / "codex"), str(CODEX_SOCK))
        rc = self.build / "codex-rc.sh"
        rc.write_text("PS1='$ '\ncodex() { " + " ".join(q(a) for a in tui) + ' "$@"; }\n')
        self.bash("codex", clean_env(self.path), PROJECT, rcfile=rc)

    def app_server_answers(self) -> bool:
        from switchboard.adapters.codex_rpc import one_shot
        try:
            asyncio.run(one_shot(str(CODEX_SOCK), lambda r: r.loaded_threads(), timeout=3))
            return True
        except Exception:
            return False

    def claude_ready(self, timeout: float = 90) -> None:
        deadline = time.monotonic() + timeout
        seen_folder = False
        while time.monotonic() < deadline:
            s = self.screen("claude")
            if "Yes, I trust this folder" in s:
                # only our own scratch project is ever trusted; the cursor starts on "No, exit"
                if not seen_folder:
                    assert str(PROJECT) in s, "trust dialog for an unexpected folder:\n" + s
                    seen_folder = True
                if re.search(r"❯\s*(\d\.\s*)?Yes, I trust this folder", s):
                    self.tmux.key("claude", "Enter")
                    time.sleep(1.5)
                else:
                    self.tmux.key("claude", "Down")
                    time.sleep(0.5)
                continue
            if "Claude in Chrome" in s:
                self.tmux.key("claude", "Escape")
                time.sleep(1)
                continue
            if re.search(r"(shortcuts|manual mode|accept edits|bypass permissions)", s):
                self.rec.mark("claude-ready")
                return
            time.sleep(0.4)
        raise TimeoutError("claude never became ready:\n" + self.screen("claude"))

    def codex_ready(self, timeout: float = 90) -> None:
        """Esc a "Hooks need review" dialog (continue without trusting), never "trust all";
        never answer a folder-trust dialog (the project is trusted per launch)."""
        deadline = time.monotonic() + timeout
        ready_since: float | None = None
        while time.monotonic() < deadline:
            s = self.screen("codex")
            if "Hooks need review" in s or ("esc close" in s and "trust all" in s):
                self.tmux.key("codex", "Escape")
                ready_since = None
                time.sleep(1.5)
                continue
            if "Trust this folder" in s or "trust this directory" in s.lower():
                raise RuntimeError("a Codex folder-trust dialog appeared:\n" + s)
            if CODEX_READY in s:
                ready_since = ready_since or time.monotonic()
                if time.monotonic() - ready_since >= 2.5:
                    self.rec.mark("codex-ready")
                    return
            else:
                ready_since = None
            time.sleep(0.4)
        raise TimeoutError("codex never became ready:\n" + self.screen("codex"))

    def idle(self, pane: str) -> bool:
        s = self.screen(pane)
        return "esc to interrupt" not in s and "Working" not in s

    # --------------------------------------------------------- scene: join
    def scene_join(self) -> None:
        self.rec.scene = "join-claude"
        self.claude_pane()
        self.type("claude", "claude")
        self.claude_ready()
        time.sleep(0.8)
        self.type("claude", "join switchboard room #build as claude-1")
        wait(lambda: "claude-1" in self.buddies(), 120, "claude-1 in the buddy list", step=0.6,
             during=lambda: self.shot("join"))
        wait(lambda: self.idle("claude"), 90, "claude to finish joining", step=0.6)
        self.shot("joined-claude")
        self.rec.scene = "join-codex"
        self.codex_pane()
        self.type("codex", "codex")
        self.codex_ready()
        time.sleep(0.8)
        self.type("codex", "join switchboard room #build as codex-1")
        wait(lambda: "codex-1" in self.buddies() and "verifying" not in self.buddies(), 180,
             "codex-1 verified in the buddy list", step=0.6, during=lambda: self.shot("join"))
        wait(lambda: self.idle("codex"), 120, "codex to finish joining", step=0.6)
        for _ in range(3):
            self.shot("joined")
            time.sleep(0.5)

    # --------------------------------------------------------- scene: talk
    def chat(self) -> list[dict[str, str]]:
        return self.js("[...document.querySelectorAll('#log .line.k-chat')].map(l => ({"
                       "who: (l.querySelector('.nick-human, .nick-agent') || {}).textContent || '',"
                       "text: l.textContent}))") or []

    def scene_talk(self, max_s: float) -> None:
        self.rec.scene = "task"
        self.js("document.getElementById('input').focus()")
        assert self.cdp is not None
        for i, ch in enumerate(TASK):
            self.cdp.call("Input.insertText", text=ch)
            if i % 4 == 3:
                self.shot("typing")
        self.shot("typed")
        time.sleep(0.6)
        for typ in ("keyDown", "keyUp"):
            self.cdp.call("Input.dispatchKeyEvent", type=typ, key="Enter", code="Enter",
                          windowsVirtualKeyCode=13, nativeVirtualKeyCode=13)
        self.rec.mark("posted")
        self.rec.scene = "talk"
        seen = 0
        quiet_since = time.monotonic()
        deadline = time.monotonic() + max_s
        while time.monotonic() < deadline:
            msgs = self.chat()
            agents = [m for m in msgs if "alice" not in m["who"]]
            if len(msgs) != seen:
                seen = len(msgs)
                quiet_since = time.monotonic()
                if msgs:
                    self.rec.mark("message", who=msgs[-1]["who"])
            self.shot("talk")
            both = {"claude-1", "codex-1"} <= {a["who"].strip("<> ") for a in agents}
            if both and self.idle("claude") and self.idle("codex") and time.monotonic() - quiet_since > 12:
                break
            time.sleep(0.8)
        for _ in range(4):
            self.shot("end")
            time.sleep(0.5)
        # where the conversation (alice's request onwards) is, for render.py's closing crop
        box = self.js("(() => { const ls = [...document.querySelectorAll('#log .line.k-chat')];"
                      " if (!ls.length) return null; const a = ls[0].getBoundingClientRect(),"
                      " b = ls[ls.length - 1].getBoundingClientRect();"
                      " return [a.left - 8, a.top - 8, a.right + 8, b.bottom + 8]; })()")
        self.rec.mark("end", chat_box=box)

    # ------------------------------------------------------------ teardown
    def teardown(self) -> None:
        self.rec.close()
        try:
            if "term" in self.rec.panes:
                self.type("term", "switchboard stop", cps=200)
                time.sleep(2)
        except Exception:
            pass
        if self.cdp is not None:
            try:
                self.cdp.close()
            except Exception:
                pass
        for p in (self.chrome_proc, self.app_server):
            if p is not None and p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        self.tmux.kill_server()
        subprocess.run(["pkill", "-f", str(DEMO)], check=False)
        shutil.rmtree(self.build / "chrome-profile", ignore_errors=True)
        fail, info = drift.compare(self.drift0, drift.snapshot())
        bad, tui = drift.codex_config_diff(self.codex_cfg0, drift.codex_config())
        fail = [f for f in fail if f != "~/.codex/config.toml"] + [f"~/.codex/config.toml: {k}" for k in bad]
        meta = {"tag": TAG, "claude_model": self.claude_model, "codex_model": codex_profile.MODEL, "task": TASK,
                "term": [TERM_W, TERM_H], "view": [VIEW_W, VIEW_H, DPR], "drift_fail": fail,
                "drift_info": info + [f"~/.codex/config.toml: [{k}]" for k in tui]}
        for name, argv in (("claude_version", [self.claude, "--version"]), ("codex_version", [self.codex, "--version"])):
            meta[name] = subprocess.run(argv, capture_output=True, text=True, env=clean_env(self.path)).stdout.strip()
        (self.build / "meta.json").write_text(json.dumps(meta, indent=1))
        if fail:
            sys.exit(f"your harness config changed during the capture: {fail}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--build", type=Path, required=True, help="where to write timeline.jsonl and shots/")
    ap.add_argument("--claude-model", default="sonnet", help="Claude model for claude-1 (default: sonnet)")
    ap.add_argument("--talk-s", type=float, default=240, help="the longest the agents may talk (s)")
    args = ap.parse_args()
    args.build.mkdir(parents=True, exist_ok=True)
    demo = Demo(args.build.resolve(), args.claude_model)
    try:
        demo.setup()
        link = demo.scene_install()
        demo.open_browser()
        demo.scene_signin(link)
        demo.scene_join()
        demo.scene_talk(args.talk_s)
    finally:
        demo.teardown()
    log("captured:", args.build)


if __name__ == "__main__":
    main()
