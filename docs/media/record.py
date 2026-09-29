"""Record the README's demo video from a real run, as screen video.

Nothing here is mocked or redrawn. switchboard runs from this checkout in a throwaway HOME,
with a real Claude Code session and a real Codex session in a private tmux server. Three
virtual X displays are recorded by ffmpeg while it happens:

- the web UI, in Chrome at 1280x720 and 2.5x (3200x1800), in its dark theme;
- each agent's terminal, live: a read-only tmux client streamed into xterm.js (termview.py,
  termview.html), in Chrome at 1280x720 and 1.5x (1920x1080).

The scene: both agents review a real pull request from this repository (#14, "remote machines
may join any room by default") in a clone checked out at its base, with the PR as a branch.
They have to reconcile their findings with each other before the human reads them.

The human is played over the DevTools protocol: sign in, create #build, type the task. The
recordings carry wall-clock timestamps, and timeline.jsonl marks what happened when (each
message with its box on screen, each edit or run in a terminal), so edit.py can cut the video.

    uv run python docs/media/record.py --build /tmp/sb-rec

Isolation is as in the live tests: switchboard (install, config, broker, database, web
session) lives under /tmp/sb-demo, and the agents work in a scratch git repo there. Claude
Code and Codex use your logins but only per-launch settings: Claude with --setting-sources
project,local --strict-mcp-config, accept-edits and an allow list; Codex on a private
app-server (never your daemon) with -c overrides only. tests/live/harness/drift.py checks
your harness config before and after. The sessions still land in your Claude Code and Codex
histories. The sign-in token is never written to disk.
"""

from __future__ import annotations

import argparse
import asyncio
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

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "tests" / "live"))

from harness import codex_profile, codex_trust, drift, profiles  # noqa: E402
from harness.tmuxdrv import REAL_HOME, Tmux, clean_env  # noqa: E402
from websockets.sync.client import connect  # noqa: E402

DEMO = Path("/tmp/sb-demo")
ALICE = DEMO / "alice"            # the throwaway HOME
PROJECT = ALICE / "project"       # the repo the agents work in
CODEX_SOCK = DEMO / "cx.sock"     # the private Codex app-server (short: sun_path)
COLS, ROWS = 100, 28              # each agent's terminal
UI = (1280, 720, 2.5)             # the web UI: CSS width, height, device scale (3200x1800)
TV = (1280, 720, 1.5)             # a terminal view
DISPLAYS = {"ui": 90, "claude": 91, "codex": 92}
FPS = 30
# the pull request under review: this repository's #14, as a branch `review` on top of `main` at its base
PR_BASE, PR_HEAD = "d0adbb672ac74dee300e969d8c7a22ecc2bd8411", "6df58b36b5ca4dc8537a71dddac408d8a6da0bb5"
# Claude Code wrote #14, so it defends it; Codex reviews it; they settle it before the human reads it
TASK = ("@codex-1 review the branch `review` against `main`. Be tough. @claude-1 you wrote it: push back "
        "where Codex is wrong, concede where it's right. Settle it between you, then Codex gives the verdict.")
CLOSING = "Verdict accepted. Thanks, both."
CPS = 11                          # the human's typing speed, characters per second
# how the agents talk in the room: in the project's instruction files, which both harnesses read
ETIQUETTE = """# Working in this repository

You are in a switchboard room with another coding agent and your user.

- In the room, write plain sentences: one or two per message, under 25 words, no code blocks, no bullet lists.
- Name what you found and where, in words: "the welcome frame lists only the first 64 rooms".
- Never accept a peer's claim as-is: check it in the code, then say "confirmed" or say what you found instead.
- When you were wrong, say so in one sentence and move on.
- Review by reading the diff (git diff main...review) and the files. Don't run tests or install anything.
- The reviewer ends the discussion with one message that starts with "Verdict:".
"""
# with accept-edits, Claude may not edit any harness's project config or git's own files,
# and may run only read-only git and searches
CLAUDE_DENY = ["Edit(.claude/**)", "Edit(.devin/**)", "Edit(.codex/**)", "Edit(.git/**)", "Bash(git diff --output:*)"]
CLAUDE_BASH_ALLOW = ["Bash(git diff:*)", "Bash(git show:*)", "Bash(git log:*)", "Bash(git status:*)",
                     "Bash(git branch:*)", "Bash(git fetch:*)", "Bash(git rev-parse:*)", "Bash(grep:*)", "Bash(rg:*)", "Bash(cat:*)",
                     "Bash(head:*)", "Bash(sed -n:*)"]
APPROVAL = re.compile(r"(Do you want to proceed|Would you like to run|Allow command|approve this|\[y/n\])", re.I)
CODEX_READY = "Ask Codex to do anything"
LINK_RE = re.compile(r"http://switchboard\.localhost:\d+/login\?t=[A-Za-z0-9_-]{20,}")
# lines worth a mark: an agent reading the diff or the code
WORK = {
    "claude": re.compile(r"^\W*(Read|Bash|Grep|Search)\(.*(git diff|git show|remote|agents\.py|pairing|config)"),
    "codex": re.compile(r"^\W*(Ran git (diff|show|log)|Explored|Read .*\.py)"),
}


def log(*a: Any) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def q(a: str) -> str:
    return "'" + a.replace("'", "'\\''") + "'"


def wait(pred: Any, timeout: float, what: str, step: float = 0.25) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise TimeoutError(f"timed out waiting for {what}")


class CDP:
    """The few DevTools protocol calls this needs, over a page's WebSocket."""

    def __init__(self, ws_url: str) -> None:
        self._conn = connect(ws_url, max_size=None, open_timeout=10)
        self.ws = self._conn.__enter__()
        self.n = 0
        self.lock = threading.Lock()

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        with self.lock:
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


class Timeline:
    """Marks with wall-clock times (the recordings' own clock), and every change to a
    terminal's text, in timeline.jsonl."""

    def __init__(self, build: Path, tmux: Tmux) -> None:
        self.f = open(build / "timeline.jsonl", "w")
        self.tmux = tmux
        self.panes: list[str] = []
        self.last: dict[str, list[str]] = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._poll, daemon=True)
        self.thread.start()

    def write(self, rec: dict[str, Any]) -> None:
        with self.lock:
            self.f.write(json.dumps({"t": round(time.time(), 3), **rec}) + "\n")
            self.f.flush()

    def mark(self, what: str, **extra: Any) -> None:
        self.write({"kind": "mark", "what": what, **extra})

    def _poll(self) -> None:
        while not self.stop.is_set():
            for pane in list(self.panes):
                r = self.tmux.run("capture-pane", "-p", "-t", pane)
                if r.returncode != 0:
                    continue
                lines = r.stdout.split("\n")
                old = self.last.get(pane)
                if old != lines:
                    self.write({"kind": "term", "pane": pane, "text": r.stdout})
                    rx = WORK.get(pane)
                    if rx is not None and old is not None:
                        before = set(old)
                        for row, ln in enumerate(lines):
                            if ln not in before and rx.search(ln):
                                self.mark("work", pane=pane, row=row, line=ln.strip())
                    self.last[pane] = lines
            time.sleep(0.2)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(5)
        self.f.close()


class Rig:
    """Virtual displays, the Chrome windows on them and the ffmpeg recordings of them."""

    def __init__(self, build: Path) -> None:
        self.build = build
        self.procs: list[subprocess.Popen[bytes]] = []
        self.recs: dict[str, subprocess.Popen[bytes]] = {}
        self.chrome = shutil.which("google-chrome") or shutil.which("chromium") or sys.exit("no Chrome")
        for tool in ("Xvfb", "ffmpeg"):
            shutil.which(tool) or sys.exit(f"{tool} is not on PATH")

    def spawn(self, argv: list[str], env: dict[str, str] | None = None, log_name: str = "") -> subprocess.Popen[bytes]:
        out = open(self.build / f"{log_name}.log", "ab") if log_name else subprocess.DEVNULL
        p = subprocess.Popen(argv, env=env, stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
        self.procs.append(p)
        return p

    def display(self, n: int, w: int, h: int) -> None:
        if Path(f"/tmp/.X11-unix/X{n}").exists():
            sys.exit(f"display :{n} is taken")
        self.spawn(["Xvfb", f":{n}", "-screen", "0", f"{w}x{h}x24", "-nolisten", "tcp", "-nocursor"],
                   log_name=f"xvfb-{n}")
        wait(lambda: Path(f"/tmp/.X11-unix/X{n}").exists(), 10, f"Xvfb :{n}")

    def browser(self, n: int, url: str, w: int, h: int, scale: float, name: str) -> CDP:
        """Chrome on display n, one app window filling it; returns its page over CDP."""
        prof = self.build / f"chrome-{name}"
        shutil.rmtree(prof, ignore_errors=True)
        env = {k: v for k, v in os.environ.items() if k not in ("WAYLAND_DISPLAY",)}
        env.update(DISPLAY=f":{n}", XDG_SESSION_TYPE="x11")
        self.spawn([self.chrome, "--ozone-platform=x11", f"--app={url}", f"--user-data-dir={prof}",
                    "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
                    "--disable-extensions", "--hide-scrollbars", "--disable-features=Translate,TranslateUI",
                    "--password-store=basic", "--disable-session-crashed-bubble", "--noerrdialogs",
                    f"--force-device-scale-factor={scale}", "--window-position=0,0", f"--window-size={w},{h}"],
                   env=env, log_name=f"chrome-{name}")
        port = wait(lambda: (prof / "DevToolsActivePort").exists()
                    and ((prof / "DevToolsActivePort").read_text().split() or [None])[0], 30, f"Chrome ({name})")
        pages = wait(lambda: [p for p in json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list",
                                                                           timeout=5)) if p.get("type") == "page"],
                     20, f"a page in Chrome ({name})")
        cdp = CDP(pages[0]["webSocketDebuggerUrl"])
        cdp.call("Page.enable")
        return cdp

    def record(self, name: str, n: int, w: int, h: int) -> None:
        """Record display n from now on; the file keeps the wall-clock timestamps (-copyts)."""
        out = self.build / f"{name}.mkv"
        p = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-y", "-f", "x11grab", "-draw_mouse", "0", "-framerate", str(FPS),
             "-video_size", f"{w}x{h}", "-thread_queue_size", "1024", "-i", f":{n}", "-copyts",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "14", "-pix_fmt", "yuv420p", str(out)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=open(self.build / f"ffmpeg-{name}.log", "ab"),
            start_new_session=True)
        self.recs[name] = p

    def stop_recording(self) -> None:
        for p in self.recs.values():
            try:
                assert p.stdin is not None
                p.stdin.write(b"q")
                p.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        for p in self.recs.values():
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                p.kill()

    def close(self) -> None:
        self.stop_recording()
        for p in reversed(self.procs):
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        time.sleep(0.5)
        for name in list(DISPLAYS.values()):
            Path(f"/tmp/.X11-unix/X{name}").unlink(missing_ok=True)
            Path(f"/tmp/.X{name}-lock").unlink(missing_ok=True)


class Demo:
    def __init__(self, build: Path, claude_model: str) -> None:
        self.build = build
        self.claude_model = claude_model
        self.path = os.environ.get("PATH", "/usr/bin:/bin")
        self.claude = shutil.which("claude") or sys.exit("claude is not on PATH")
        self.codex = os.path.realpath(shutil.which("codex") or sys.exit("codex is not on PATH"))
        self.uv = shutil.which("uv") or sys.exit("uv is not on PATH")
        self.tmux = Tmux("rec", self.path)
        self.tl = Timeline(build, self.tmux)
        self.rig = Rig(build)
        self.drift0 = drift.snapshot()
        self.codex_cfg0 = drift.codex_config()
        self.app_server: subprocess.Popen[bytes] | None = None
        self.ui: CDP | None = None
        self.views: dict[str, CDP] = {}
        self.bin = DEMO / "bin"
        self.alice_env = clean_env(
            f"{ALICE}/.local/bin:{self.bin}:{Path(self.uv).parent}:/usr/bin:/bin",
            HOME=str(ALICE), USER="alice", LOGNAME="alice",
            UV_CACHE_DIR=f"{REAL_HOME}/.cache/uv", UV_PYTHON_INSTALL_DIR=f"{REAL_HOME}/.local/share/uv/python")

    # --------------------------------------------------------------- setup
    def setup(self) -> None:
        if DEMO.exists():
            if DEMO.lstat().st_uid != os.getuid() or DEMO.is_symlink():
                sys.exit(f"{DEMO} exists and isn't yours; remove it first")
            subprocess.run(["pkill", "-f", str(DEMO)], check=False)
            time.sleep(0.5)
            shutil.rmtree(DEMO)
        ALICE.mkdir(parents=True, mode=0o700)
        os.chmod(DEMO, 0o700)
        self.bin.mkdir()
        (self.bin / "claude").symlink_to(os.path.realpath(self.claude))
        (self.bin / "codex").symlink_to(self.codex)
        # the repository at the PR's base, the PR as the branch `review`; no remote, so nothing
        # names this machine's paths, and the instruction files kept out of git's view
        genv = clean_env(self.path, GIT_AUTHOR_NAME="alice", GIT_AUTHOR_EMAIL="alice@example.com",
                         GIT_COMMITTER_NAME="alice", GIT_COMMITTER_EMAIL="alice@example.com")
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(ROOT), str(PROJECT)], env=genv, check=True)
        for cmd in (["git", "checkout", "-q", "-B", "main", PR_BASE], ["git", "branch", "-f", "review", PR_HEAD],
                    ["git", "remote", "remove", "origin"], ["git", "config", "user.name", "alice"],
                    ["git", "config", "user.email", "alice@example.com"]):
            subprocess.run(cmd, cwd=PROJECT, env=genv, check=True)
        for name in ("CLAUDE.md", "AGENTS.md"):
            (PROJECT / name).write_text(ETIQUETTE)
        with open(PROJECT / ".git" / "info" / "exclude", "a") as f:
            f.write("CLAUDE.md\nAGENTS.md\n.codex/\n")
        sb = ALICE / ".switchboard"
        sb.mkdir(mode=0o700)
        (sb / "config.toml").write_text(
            f'human_name = "alice"\n[claude]\nsessions_dir = "{REAL_HOME}/.claude/sessions"\n'
            f'[codex]\ncontrol_socket = "{CODEX_SOCK}"\nbin = "{self.codex}"\n'
            # a longer back-and-forth than the loop guard's default of 6 agent messages in a row
            '[delivery]\nhop_limit = 14\n')
        os.chmod(sb / "config.toml", 0o600)

    def alice(self, *argv: str, timeout: float = 600, quiet: bool = False) -> str:
        """A command in alice's shell; `quiet` for one that leaves a daemon holding its output."""
        out = subprocess.DEVNULL if quiet else subprocess.PIPE
        r = subprocess.run(list(argv), env=self.alice_env, cwd=ALICE, stdout=out, stderr=out, text=True,
                           timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(f"{argv} failed ({r.returncode}): {r.stdout or ''}{r.stderr or ''}")
        return r.stdout or ""

    def install(self) -> str:
        """switchboard from this checkout, registered with the harnesses; returns the sign-in link."""
        log("installing switchboard from", ROOT)
        self.alice(self.uv, "tool", "install", "--force", str(ROOT))
        self.alice("switchboard", "install", "all", "--yes")
        self.codex_server()
        # sign-in links are issued only to a terminal a human typed in: alice's, which is
        # never recorded (the timeline doesn't read it either, so the token stays off disk)
        self.tmux.new_session("term", str(ALICE), dict(self.alice_env, PS1="$ "),
                              ["bash", "--noprofile", "--norc", "-i"], width=200, height=50)
        wait(lambda: self.screen("term").rstrip().endswith("$"), 10, "alice's prompt")
        self.tmux.type("term", "switchboard start --port 0")
        m = wait(lambda: LINK_RE.search(self.tmux.run("capture-pane", "-p", "-J", "-t", "term").stdout), 60,
                 "the sign-in link")
        return m.group(0)

    def print_args(self, harness: str) -> dict[str, Any]:
        return json.loads(self.alice(str(ALICE / ".local/bin/switchboard"), "install", harness, "--print-args",
                                     "--home", str(ALICE / ".switchboard"), timeout=30))

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
        out = open(self.build / "app-server.log", "ab")
        self.app_server = subprocess.Popen(
            codex_profile.app_server_argv(self.codex, str(CODEX_SOCK), overrides, hooks_state),
            cwd=str(PROJECT), env=clean_env(self.path), stdin=subprocess.DEVNULL, stdout=out, stderr=out,
            start_new_session=True)
        wait(self.app_server_answers, 30, "the private Codex app-server", step=0.3)

    def app_server_answers(self) -> bool:
        from switchboard.adapters.codex_rpc import one_shot
        try:
            asyncio.run(one_shot(str(CODEX_SOCK), lambda r: r.loaded_threads(), timeout=3))
            return True
        except Exception:
            return False

    # ----------------------------------------------------------- terminals
    def pane(self, name: str, fn: str, argv: list[str], env: dict[str, str]) -> None:
        """A bash in tmux whose `fn` runs argv: the viewer only ever sees `claude` or `codex` typed."""
        rc = self.build / f"{name}-rc.sh"
        rc.write_text("PS1='$ '\n" + fn + "() { " + " ".join(q(a) for a in argv) + ' "$@"; }\n')
        env = dict(env, COLORTERM="truecolor")
        self.tmux.new_session(name, str(PROJECT), env, ["bash", "--noprofile", "--rcfile", str(rc), "-i"],
                              width=COLS, height=ROWS)
        # no status line; the viewers attach at the pane's own size, so it never changes
        # (a global window-size=manual would crash tmux 3.6 at the next new-session)
        self.tmux.run("set-option", "-g", "status", "off")
        self.tmux.run("set-option", "-ga", "terminal-overrides", ",xterm-256color:Tc")
        self.tl.panes.append(name)
        wait(lambda: self.screen(name).rstrip().endswith("$"), 10, f"{name} prompt")
        self.tmux.run("send-keys", "-t", name, "clear", "Enter")
        time.sleep(0.3)

    def screen(self, name: str) -> str:
        return self.tmux.capture(name)

    def type(self, name: str, text: str, cps: float = 22) -> None:
        for ch in text:
            self.tmux.send_text(name, ch)
            time.sleep(1 / cps)
        time.sleep(0.35)
        self.tmux.key(name, "Enter")

    def claude_pane(self) -> None:
        pa = self.print_args("claude")
        (self.build / "mcp.json").write_text(json.dumps(profiles.claude_mcp_config(pa)))
        settings = profiles.claude_settings(pa, None)
        settings["permissions"] = {"allow": list(CLAUDE_BASH_ALLOW), "deny": list(profiles.TEST_DENY) + CLAUDE_DENY}
        (self.build / "settings.json").write_text(json.dumps(settings))
        argv = [str(self.bin / "claude"), "--model", self.claude_model, "--setting-sources", "project,local",
                "--strict-mcp-config", "--permission-mode", "acceptEdits",
                "--allowedTools", ",".join(profiles.SWITCHBOARD_TOOLS + CLAUDE_BASH_ALLOW),
                "--mcp-config", str(self.build / "mcp.json"), "--settings", str(self.build / "settings.json")]
        self.pane("claude", "claude", argv, clean_env(self.path, DISABLE_AUTOUPDATER="1"))

    def codex_pane(self) -> None:
        self.pane("codex", "codex", codex_profile.tui_argv(str(self.bin / "codex"), str(CODEX_SOCK)),
                  clean_env(self.path))

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
                self.tl.mark("claude-ready")
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
                    self.tl.mark("codex-ready")
                    return
            else:
                ready_since = None
            time.sleep(0.4)
        raise TimeoutError("codex never became ready:\n" + self.screen("codex"))

    def idle(self, pane: str) -> bool:
        s = self.screen(pane)
        return "esc to interrupt" not in s and "Working" not in s

    # --------------------------------------------------------------- views
    def open_views(self, link: str, port: int) -> None:
        w, h, s = UI
        self.rig.display(DISPLAYS["ui"], int(w * s), int(h * s))
        self.ui = self.rig.browser(DISPLAYS["ui"], link.split("/login")[0] + "/", w, h, s, "ui")
        w, h, s = TV
        for name in ("claude", "codex"):
            self.rig.display(DISPLAYS[name], int(w * s), int(h * s))
            url = (HERE / "termview.html").as_uri() + f"?name={name}&cols={COLS}&rows={ROWS}&port={port}"
            self.views[name] = self.rig.browser(DISPLAYS[name], url, w, h, s, name)
            wait(lambda: self.views[name].eval("window.__cell || null"), 30, f"the {name} view")
        cells = {name: v.eval("window.__cell") for name, v in self.views.items()}
        (self.build / "views.json").write_text(json.dumps({"ui": UI, "tv": TV, "cols": COLS, "rows": ROWS,
                                                            "cells": cells}, indent=1))
        assert self.ui is not None
        # the UI follows the system's light or dark setting: dark, like the terminals beside it
        self.ui.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "dark"}])
        self.ui.call("Page.navigate", url=link)
        wait(lambda: self.js("document.getElementById('st-conn') && "
                             "document.getElementById('st-conn').textContent.trim()") == "Connected", 20, "the web UI")

    def js(self, expr: str) -> Any:
        assert self.ui is not None
        return self.ui.eval(expr)

    def members(self) -> str:
        return self.js("document.getElementById('buddy-list').textContent") or ""

    def create_room(self) -> None:
        wait(lambda: self.js("!!document.getElementById('create-build') && "
                             "document.getElementById('create-build').offsetParent !== null"), 10, "the welcome form")
        time.sleep(0.6)
        self.js("document.getElementById('create-build').click()")
        wait(lambda: "build" in (self.js("document.getElementById('room-title').textContent") or ""), 10, "#build")
        self.tl.mark("room")

    # ----------------------------------------------------------- the story
    def join(self) -> None:
        self.type("claude", "claude")
        self.claude_ready()
        time.sleep(0.8)
        self.type("claude", "join switchboard room #build as claude-1")
        wait(lambda: "claude-1" in self.members(), 120, "claude-1 in Members", step=0.6)
        wait(lambda: self.idle("claude"), 90, "claude to finish joining", step=0.6)
        self.type("codex", "codex")
        self.codex_ready()
        time.sleep(0.8)
        self.type("codex", "join switchboard room #build as codex-1")
        wait(lambda: "codex-1" in self.members() and "verifying" not in self.members(), 180,
             "codex-1 verified in Members", step=0.6)
        wait(lambda: self.idle("codex"), 120, "codex to finish joining", step=0.6)
        self.tl.mark("joined")

    def chat(self) -> list[dict[str, Any]]:
        return self.js("[...document.querySelectorAll('#log .line.k-chat')].map(l => {"
                       " const r = l.getBoundingClientRect();"
                       " return {who: l.dataset.from || '', text: l.textContent.trim(),"
                       "         box: [r.left, r.top, r.right, r.bottom]}; })") or []

    def say(self, text: str) -> None:
        """Type into the composer at a human's pace and press Enter."""
        assert self.ui is not None
        self.js("document.getElementById('input').focus()")
        for ch in text:
            self.ui.call("Input.insertText", text=ch)
            time.sleep(1 / CPS)
        time.sleep(0.9)
        for typ in ("keyDown", "keyUp"):
            self.ui.call("Input.dispatchKeyEvent", type=typ, key="Enter", code="Enter",
                         windowsVirtualKeyCode=13, nativeVirtualKeyCode=13)

    def talk(self, max_s: float) -> None:
        time.sleep(3)
        composer = self.js("(() => { const r = document.getElementById('composer').getBoundingClientRect();"
                           " return [r.left, r.top, r.right, r.bottom]; })()")
        self.tl.mark("before-task", chat=self.chat(), composer=composer)
        self.js("document.getElementById('input').focus()")
        assert self.ui is not None
        self.tl.mark("typing")
        self.say(TASK)
        self.tl.mark("posted")
        seen = 0
        quiet_since = time.monotonic()
        deadline = time.monotonic() + max_s
        prompt_since: dict[str, float] = {}
        while time.monotonic() < deadline:
            msgs = self.chat()
            if len(msgs) != seen:
                for m in msgs[seen:]:
                    self.tl.mark("message", who=m["who"], text=m["text"], chat=msgs)
                seen = len(msgs)
                quiet_since = time.monotonic()
            for pane in ("claude", "codex"):
                # an approval prompt is never answered yes: declined with Esc after 10 s
                if APPROVAL.search(self.screen(pane)):
                    prompt_since.setdefault(pane, time.monotonic())
                    if time.monotonic() - prompt_since[pane] > 10:
                        self.tmux.key(pane, "Escape")
                        self.tl.mark("declined", pane=pane)
                        prompt_since.pop(pane)
                else:
                    prompt_since.pop(pane, None)
            agents = {m["who"] for m in msgs if m["who"] != "alice"}
            paused = "Paused" in (self.js("document.getElementById('st-state').textContent") or "")
            if paused or ({"claude-1", "codex-1"} <= agents and self.idle("claude") and self.idle("codex")
                          and time.monotonic() - quiet_since > 25):
                break
            time.sleep(0.4)
        # the human calls it, and the agents may react
        time.sleep(2)
        self.tl.mark("closing")
        self.say(CLOSING)
        self.tl.mark("closed")
        quiet_since = time.monotonic()
        seen = len(self.chat())
        while time.monotonic() - quiet_since < 12 and time.monotonic() < deadline + 60:
            msgs = self.chat()
            if len(msgs) != seen:
                for m in msgs[seen:]:
                    self.tl.mark("message", who=m["who"], text=m["text"], chat=msgs)
                seen = len(msgs)
                quiet_since = time.monotonic()
            time.sleep(0.4)
        (self.build / "chat.json").write_text(json.dumps(self.chat(), indent=1))
        time.sleep(2)
        self.tl.mark("end", chat=self.chat())

    # ------------------------------------------------------------ teardown
    def teardown(self) -> None:
        self.rig.stop_recording()
        self.tl.close()
        try:
            self.alice("switchboard", "stop", timeout=30)
        except Exception:
            pass
        for c in [self.ui, *self.views.values()]:
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
        self.rig.close()
        if self.app_server is not None and self.app_server.poll() is None:
            try:
                os.killpg(self.app_server.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.tmux.kill_server()
        subprocess.run(["pkill", "-f", str(DEMO)], check=False)
        for name in ("ui", "claude", "codex"):
            shutil.rmtree(self.build / f"chrome-{name}", ignore_errors=True)
        fail, info = drift.compare(self.drift0, drift.snapshot())
        bad, tui = drift.codex_config_diff(self.codex_cfg0, drift.codex_config())
        fail = [f for f in fail if f != "~/.codex/config.toml"] + [f"~/.codex/config.toml: {k}" for k in bad]
        meta = {"claude_model": self.claude_model, "codex_model": codex_profile.MODEL, "task": TASK,
                "fps": FPS, "drift_fail": fail, "drift_info": info + [f"~/.codex/config.toml: [{k}]" for k in tui]}
        for name, argv in (("claude_version", [self.claude, "--version"]), ("codex_version", [self.codex, "--version"])):
            meta[name] = subprocess.run(argv, capture_output=True, text=True, env=clean_env(self.path)).stdout.strip()
        (self.build / "meta.json").write_text(json.dumps(meta, indent=1))
        if fail:
            sys.exit(f"your harness config changed during the recording: {fail}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--build", type=Path, required=True, help="where to write the recordings and timeline.jsonl")
    ap.add_argument("--claude-model", default="sonnet", help="Claude model for claude-1 (default: sonnet)")
    ap.add_argument("--talk-s", type=float, default=420, help="the longest the agents may talk (s)")
    args = ap.parse_args()
    build = args.build.resolve()
    build.mkdir(parents=True, exist_ok=True)
    demo = Demo(build, args.claude_model)
    view_port = 18750 + os.getpid() % 1000
    try:
        demo.setup()
        link = demo.install()
        demo.rig.spawn([sys.executable, str(HERE / "termview.py"), demo.tmux.sock, str(view_port)], log_name="termview")
        demo.claude_pane()
        demo.codex_pane()
        demo.open_views(link, view_port)
        demo.create_room()
        for name, (n, (w, h, s)) in {"ui": (DISPLAYS["ui"], UI), "claude": (DISPLAYS["claude"], TV),
                                     "codex": (DISPLAYS["codex"], TV)}.items():
            demo.rig.record(name, n, int(w * s), int(h * s))
        demo.tl.mark("recording")
        demo.join()
        demo.talk(args.talk_s)
    finally:
        demo.teardown()
    log("recorded:", build)


if __name__ == "__main__":
    main()
