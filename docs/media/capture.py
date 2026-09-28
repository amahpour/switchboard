"""Capture the raw material for the README's getting-started video, from a real run.

Nothing here is mocked. A private tmux server plays the human at two terminals, as
tests/live does: one installs switchboard from the release tag into a throwaway HOME,
registers it with Claude Code and starts the broker; the other runs a real Claude Code
session that joins a room. Headless Chrome, driven over the DevTools protocol, signs in
with the link the broker printed, creates #build and posts a message, and the agent is
woken and answers. Terminal screens and browser screenshots are recorded with their
times; render.py turns them into the video.

    uv run python docs/media/capture.py --build /tmp/sb-media

Isolation: switchboard (its install, config, broker, database and web session) lives in
a throwaway HOME under /tmp/sb-demo. Claude Code runs with your own login but only with
per-launch flags (--setting-sources project,local --strict-mcp-config, the model pinned,
switchboard's eight tools allowed), so none of your harness config is read or written:
tests/live/harness/drift.py checks it before and after. The session still lands in your
Claude Code history, like any live test. The sign-in token is never written to disk.
"""

from __future__ import annotations

import argparse
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

from harness import drift, profiles  # noqa: E402
from harness.tmuxdrv import REAL_HOME, Tmux, clean_env  # noqa: E402
from websockets.sync.client import connect  # noqa: E402

TAG = "v0.3.0"
DEMO = Path("/tmp/sb-demo")
ALICE = DEMO / "alice"            # the throwaway HOME
PROJECT = ALICE / "project"       # the repo Claude works in
TERM_W, TERM_H = 84, 26
VIEW_W, VIEW_H, DPR = 860, 540, 2
QUESTION = "@claude-1 what does this project do? One sentence, please."
TOKEN_RE = re.compile(r"(login\?t=)[A-Za-z0-9_-]+")
ANSI_RE = re.compile(r"\x1b\[[0-9;:]*[A-Za-z]")


def redact(screen: str) -> str:
    """Hide the sign-in token, including the tail of a link that wrapped onto the next line."""
    lines = screen.split("\n")
    for i, ln in enumerate(lines):
        if "login?t=" in ln:
            lines[i] = TOKEN_RE.sub(r"\1••••••••", ln)
            if i + 1 < len(lines) and re.fullmatch(r"[A-Za-z0-9_-]+", ANSI_RE.sub("", lines[i + 1]).strip()):
                lines[i + 1] = ""
    return "\n".join(lines)

PORTPARSE = '''"""Parse a TCP port from user input."""


def parse_port(text: str) -> int:
    """Return the port number in ``text``, e.g. "8080" -> 8080."""
    return int(text.strip())
'''
README = "# portparse\n\nA tiny library that parses TCP port numbers from user input.\n"


def log(*a: Any) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


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

    def mark(self, what: str) -> None:
        self.write({"kind": "mark", "what": what})

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
    def __init__(self, build: Path, model: str) -> None:
        self.build = build
        self.model = model
        self.path = os.environ.get("PATH", "/usr/bin:/bin")
        self.claude = shutil.which("claude") or sys.exit("claude is not on PATH")
        self.chrome = shutil.which("google-chrome") or shutil.which("chromium") or sys.exit("no Chrome")
        uv = shutil.which("uv") or sys.exit("uv is not on PATH")
        self.tmux = Tmux("media", self.path)
        self.rec = Recorder(build, self.tmux)
        self.drift0 = drift.snapshot()
        self.chrome_proc: subprocess.Popen[bytes] | None = None
        self.cdp: CDP | None = None
        # alice's terminal: her HOME, her ~/.local/bin first; uv's caches are shared with yours
        self.alice_env = clean_env(
            f"{ALICE}/.local/bin:{Path(self.claude).parent}:{Path(uv).parent}:/usr/bin:/bin",
            HOME=str(ALICE), USER="alice", LOGNAME="alice",
            UV_CACHE_DIR=f"{REAL_HOME}/.cache/uv", UV_PYTHON_INSTALL_DIR=f"{REAL_HOME}/.local/share/uv/python",
            PS1="$ ")

    # --------------------------------------------------------------- setup
    def setup(self) -> None:
        if DEMO.exists():
            st = DEMO.lstat()
            if st.st_uid != os.getuid() or DEMO.is_symlink():
                sys.exit(f"{DEMO} exists and isn't yours; remove it first")
            subprocess.run(["pkill", "-f", str(ALICE)], check=False)
            shutil.rmtree(DEMO)
        PROJECT.mkdir(parents=True, mode=0o700)
        os.chmod(DEMO, 0o700)
        (PROJECT / "portparse.py").write_text(PORTPARSE)
        (PROJECT / "README.md").write_text(README)
        genv = clean_env(self.path, GIT_AUTHOR_NAME="alice", GIT_AUTHOR_EMAIL="alice@example.com",
                         GIT_COMMITTER_NAME="alice", GIT_COMMITTER_EMAIL="alice@example.com")
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "portparse"]):
            subprocess.run(cmd, cwd=PROJECT, env=genv, check=True)
        # the broker reads Claude's session registry from your real Claude home
        sb = ALICE / ".switchboard"
        sb.mkdir(mode=0o700)
        (sb / "config.toml").write_text(
            f'human_name = "alice"\n[claude]\nsessions_dir = "{REAL_HOME}/.claude/sessions"\n')
        os.chmod(sb / "config.toml", 0o600)

    def bash(self, name: str, env: dict[str, str], cwd: Path, rcfile: Path | None = None) -> None:
        argv = ["bash", "--noprofile"] + (["--rcfile", str(rcfile)] if rcfile else ["--norc"]) + ["-i"]
        self.tmux.new_session(name, str(cwd), env, argv, width=TERM_W, height=TERM_H)
        self.rec.panes.append(name)
        wait(lambda: self.screen(name).rstrip().endswith("$"), 10, f"{name} prompt")

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
        self.tmux.run("send-keys", "-t", "term", "clear", "Enter")
        time.sleep(0.5)
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
        wait(lambda: re.search(r"^  claude: ", self.screen("term"), re.M), 60, "the install summary")
        if not re.search(r"^  claude: installed", self.screen("term"), re.M):
            raise RuntimeError("switchboard install all failed:\n" + self.screen("term"))
        time.sleep(1.2)
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

    # --------------------------------------------------------- scene: join
    def scene_join(self) -> None:
        self.rec.scene = "join"
        pa = subprocess.run([str(ALICE / ".local/bin/switchboard"), "install", "claude", "--print-args",
                             "--home", str(ALICE / ".switchboard")], env=self.alice_env,
                            capture_output=True, text=True, timeout=30, check=True)
        print_args = json.loads(pa.stdout)
        (self.build / "mcp.json").write_text(json.dumps(profiles.claude_mcp_config(print_args)))
        (self.build / "settings.json").write_text(json.dumps(profiles.claude_settings(print_args, None)))
        argv = profiles.claude_argv(self.claude, self.build / "mcp.json", self.build / "settings.json")
        argv[argv.index("--model") + 1] = self.model
        # the viewer sees "claude"; the flags that keep your own config out stay in this function
        rc = self.build / "claude-rc.sh"
        rc.write_text("PS1='$ '\nclaude() { " + " ".join(_q(a) for a in argv) + ' "$@"; }\n')
        env = clean_env(self.path, DISABLE_AUTOUPDATER="1")
        self.bash("claude", env, PROJECT, rcfile=rc)
        self.tmux.run("send-keys", "-t", "claude", "clear", "Enter")
        time.sleep(0.4)
        self.type("claude", "claude")
        self.claude_ready()
        time.sleep(1.0)
        self.type("claude", "join switchboard room #build as claude-1")
        wait(lambda: "claude-1" in (self.js("document.getElementById('buddy-list').textContent") or ""),
             120, "claude-1 in the buddy list", step=0.5, during=lambda: self.shot("join"))
        wait(self.claude_idle, 90, "claude to finish the join", step=0.5, during=lambda: self.shot("join"))
        for _ in range(3):
            self.shot("joined")
            time.sleep(0.5)

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

    def claude_idle(self) -> bool:
        s = self.screen("claude")
        return "esc to interrupt" not in s

    # --------------------------------------------------------- scene: wake
    def scene_wake(self) -> None:
        self.rec.scene = "wake"
        self.js("document.getElementById('input').focus()")
        assert self.cdp is not None
        for i, ch in enumerate(QUESTION):
            self.cdp.call("Input.insertText", text=ch)
            if i % 3 == 2:
                self.shot("typing")
        self.shot("typed")
        time.sleep(0.6)
        for typ in ("keyDown", "keyUp"):
            self.cdp.call("Input.dispatchKeyEvent", type=typ, key="Enter", code="Enter",
                          windowsVirtualKeyCode=13, nativeVirtualKeyCode=13)
        self.rec.mark("posted")
        reply = ("[...document.querySelectorAll('#log .line.k-chat')]"
                 ".some(l => l.textContent.includes('claude-1') && l.querySelector('.nick-agent'))")
        wait(lambda: self.js(reply), 180, "claude-1's answer", step=0.4, during=lambda: self.shot("wake"))
        self.rec.mark("answered")
        for _ in range(6):
            self.shot("answered")
            time.sleep(0.5)
        wait(self.claude_idle, 60, "claude to finish", step=0.5)
        time.sleep(1.0)
        self.rec.mark("end")

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
        if self.chrome_proc is not None and self.chrome_proc.poll() is None:
            os.killpg(self.chrome_proc.pid, signal.SIGTERM)
        self.tmux.kill_server()
        subprocess.run(["pkill", "-f", str(ALICE)], check=False)
        shutil.rmtree(self.build / "chrome-profile", ignore_errors=True)
        fail, info = drift.compare(self.drift0, drift.snapshot())
        meta = {"tag": TAG, "model": self.model, "question": QUESTION, "term": [TERM_W, TERM_H],
                "view": [VIEW_W, VIEW_H, DPR], "drift_fail": fail, "drift_info": info,
                "claude_version": subprocess.run([self.claude, "--version"], capture_output=True, text=True,
                                                 env=clean_env(self.path)).stdout.strip()}
        (self.build / "meta.json").write_text(json.dumps(meta, indent=1))
        if fail:
            sys.exit(f"your harness config changed during the capture: {fail}")


def _q(a: str) -> str:
    return "'" + a.replace("'", "'\\''") + "'"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--build", type=Path, required=True, help="where to write timeline.jsonl and shots/")
    ap.add_argument("--model", default="haiku", help="Claude model for the agent (default: haiku)")
    args = ap.parse_args()
    args.build.mkdir(parents=True, exist_ok=True)
    demo = Demo(args.build.resolve(), args.model)
    try:
        demo.setup()
        link = demo.scene_install()
        demo.open_browser()
        demo.scene_signin(link)
        demo.scene_join()
        demo.scene_wake()
    finally:
        demo.teardown()
    log("captured:", args.build)


if __name__ == "__main__":
    main()
