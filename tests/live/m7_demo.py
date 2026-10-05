"""M7: the live demo, as an automated rehearsal (DESIGN.md §13 M7, §12.4, §12.5).

Opt-in (never collected by a plain ``pytest`` run: the file name doesn't match
``test_*.py``, so it runs only when named)::

    SWITCHBOARD_LIVE=demo SWITCHBOARD_LIVE_DIR=<a temp dir> uv run pytest -m live tests/live/m7_demo.py -s

What it does:
1. a scratch git repo with **no remote** (``yk-ws-m7-*`` under the per-user
    temp dir, mode 0700, not the world-writable ``/tmp``) holding a small
    ``parse_port()`` without validation plus a test, one
   pre-created worktree per agent (``.worktrees/<name>`` on branch ``<name>``),
   and a private Python env with pytest for the agents (``python -m pytest``);
2. a test-mode broker in a temp ``SWITCHBOARD_HOME`` (budget 40, hop limit 6); the
   driver signs in with the test token and posts as the human (alice) through
   the web API;
3. the agents, each in a private tmux server with a clean ``env -i`` env,
   each told "join #build as <name>, stay in the room, use your own worktree
   under .worktrees/<name>":
   - Claude Code ``--model sonnet`` (haiku if sonnet can't start),
     ``--permission-mode acceptEdits`` and narrow allow rules (switchboard's
     tools, ``Bash(git worktree:*)``, ``Bash(git diff:*)``, ``Bash(git add:*)``,
     ``Bash(git commit:*)``, ``Bash(python -m pytest:*)``, ``Bash(python3 -m pytest:*)``),
     and deny rules for edits to any harness's project config and to ``.git``;
   - Codex on a **private** app-server on the real ``CODEX_HOME``, exactly the
     M4 live profile (on-request / workspace-write, only switchboard's MCP server),
     TUI attached with ``--remote``, ``-m gpt-5.5`` (gpt-6-luna if the account
     doesn't list it); never a bare ``codex`` or ``codex queue``;
   - Devin ``swe-1-6-slow`` with the same narrow allows (``Exec(...)``) in
     accept-edits mode; listed "not run: <reason>" if its quota or the
     sandbox blocks it;
   - Cursor: "not run: not yet tested live";
4. the script: the task at T0, interjections at T0+4 and T0+8 min, a wrap-up
   request at T0+15 min, then ``/pause``. After each human post, if the loop
   guard paused the room, ``/resume`` (recorded). Hard limits: 20 min of wall
   clock from T0, room budget 40, hop limit 6;
5. an approval prompt open for 60 s is recorded as **stalled** and then
   declined with Esc. The driver never approves anything; a poke into a parked
   Devin presses Enter only after checking that no selector is on screen;
6. teardown: ``/pause``, every tmux server, the private app-server and the
   broker killed, a process sweep, the drift check on user-level config, the
   user's Codex daemon state unchanged, and a check that no agent changed the
   workspace's harness config or git hooks (``workspace_config_changed``);

Caution: acceptEdits plus pre-approved ``python -m pytest``/``git commit`` is
unprompted code execution in the workspace (an agent can write a test or a
conftest.py and run it). Run the rehearsal in the SANDBOX.md VM when you can.
7. ``switchboard report --room '#build'`` (markdown and JSON) into the run dir,
   plus ``results.json`` (the timeline, stalls, resumes, worktree outcomes)
   and ``transcript.txt`` (the room, for the write-up; scratch only).

Knobs (for a cheap tooling smoke, not the rehearsal): ``SWITCHBOARD_M7_AGENTS``
(default ``claude,codex,devin``), ``SWITCHBOARD_M7_SCALE`` (time scale, default
1.0), ``SWITCHBOARD_M7_CLAUDE_MODEL`` (sonnet), ``SWITCHBOARD_M7_CODEX_MODEL``
(gpt-5.5), ``SWITCHBOARD_M7_CODEX_EFFORT`` (medium).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from harness import codex_profile, codex_trust, drift, preflight, profiles, screens
from harness.tmuxdrv import REAL_HOME, Tmux, clean_env

pytestmark = pytest.mark.live

LIVE = {x.strip() for x in os.environ.get("SWITCHBOARD_LIVE", "").split(",") if x.strip()}
REAL_PATH = os.environ.get("PATH", "/usr/bin:/bin")
AGENTS = [
    x.strip() for x in os.environ.get("SWITCHBOARD_M7_AGENTS", "claude,codex,devin").split(",") if x.strip()
]
SCALE = float(os.environ.get("SWITCHBOARD_M7_SCALE", "1.0"))
CLAUDE_MODEL = os.environ.get("SWITCHBOARD_M7_CLAUDE_MODEL", "sonnet")
CODEX_MODEL = os.environ.get("SWITCHBOARD_M7_CODEX_MODEL", "gpt-5.5")
CODEX_EFFORT = os.environ.get("SWITCHBOARD_M7_CODEX_EFFORT", "medium")
NAMES = {"claude": "claude-1", "codex": "codex-1", "devin": "devin-1"}
SOCKDIR = Path(
    f"/tmp/yk-cx-live-{os.getuid()}"
)  # short (sun_path); codex keeps a lock per path: reuse a.sock
USER_DAEMON_SOCK = Path(REAL_HOME) / ".codex" / "app-server-control" / "app-server-control.sock"
STALL_S = 60.0
BUDGET = 40
HOP_LIMIT = 6

# the narrow allow rules (DESIGN.md §13 M7), test-only
CLAUDE_BASH_ALLOW = [
    "Bash(git worktree:*)",
    "Bash(git diff:*)",
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(python -m pytest:*)",
    "Bash(python3 -m pytest:*)",
]
DEVIN_EXEC_ALLOW = [
    "Exec(git worktree)",
    "Exec(git diff)",
    "Exec(git add)",
    "Exec(git commit)",
    "Exec(python -m pytest)",
    "Exec(python3 -m pytest)",
]
# with acceptEdits, Claude may not edit any harness's project config (another agent's permissions
# or hooks) or git's own files (hooks run on the pre-approved `git commit`), nor let `git diff`
# write a file; Devin has no verified deny syntax: the teardown compares these files instead
CLAUDE_DENY = [
    "Edit(.claude/**)",
    "Edit(.devin/**)",
    "Edit(.codex/**)",
    "Edit(.git/**)",
    "Bash(git diff --output:*)",
]
WS_CONFIG = (".claude", ".devin", ".codex", ".git/hooks", ".git/config")
# typed into a parked Devin (as the README tells a human); no digits: nothing a selector could take
POKE_TEXT = (
    "There are messages for you in #build: read them with the switchboard read tool, act on them, then call"
    " wait again with the same room and timeout as before."
)

# what an open approval prompt looks like on each harness's screen
PROMPT_RE = {
    "claude": re.compile(r"Do you want to (proceed|make|create|allow|run)"),
    "codex": re.compile(r"Would you like to (run|make|apply|allow|grant)"),
    "devin": re.compile(r"(?i)(switch to bypass|allow (once|always|for)|do you want to (run|allow|proceed))"),
}

# the scratch project (a small parse_port() without validation, plus a test)
PORTPARSE = '''"""Parse TCP port numbers from configuration values."""


def parse_port(value):
    """Return the port number in ``value`` (a string or an int)."""
    return int(value)
'''
TEST_PORTPARSE = """from portparse import parse_port


def test_parses_a_plain_number():
    assert parse_port("8080") == 8080


def test_accepts_an_int():
    assert parse_port(443) == 443
"""
README = """# portparse

`parse_port(value)` turns a configuration value into a TCP port number.
Run the tests with `python -m pytest -q`.
"""

# ---------------------------------------------------------------- the script
TASK = (
    "Task for #build: add input validation to parse_port() in portparse.py. It should accept an int or a"
    " numeric string (surrounding whitespace is fine) and return an int, and raise ValueError for anything"
    " else: empty or non-numeric strings, floats such as '80.5', booleans, None, and ports outside 1-65535."
    " Add tests for the new cases. Divide the work between you (for example: one implements, one writes the"
    " tests, one reviews), do it in your own worktree (.worktrees/<your name>, its own branch), run"
    " `python -m pytest -q .worktrees/<your name>`, and review each other's changes. Post a short plan"
    " first; keep messages short and pass() when you have nothing new to add."
)
INTERJECT_1 = (
    "Interjection from alice: small change of plan. Ports below 1024 must be rejected unless parse_port"
    " is called with allow_privileged=True. Please fold that into the implementation and the tests."
)
INTERJECT_2 = (
    "Checking in: who has what done, and do the tests pass in your worktree? Reviewers: please post one"
    " concrete review comment each on another agent's change (name its worktree)."
)
WRAP_UP = (
    "Wrap-up: please each post one line: what you changed, whether its tests pass, and what you reviewed."
    " I'm pausing the room in two minutes."
)
SCRIPT = [
    (0.0, "task", TASK),
    (4.0, "interjection 1", INTERJECT_1),
    (8.0, "interjection 2", INTERJECT_2),
    (15.0, "wrap-up", WRAP_UP),
]
WRAP_GRACE_MIN = 2.0
HARD_LIMIT_MIN = 20.0


def join_prompt(harness: str, name: str) -> str:
    base = (
        f"Use the switchboard MCP tools: join #build as {name} and stay in the room."
        " I (alice) will post a task"
        " there; coordinate with the other agents through the room with say() and pass(). For every file you"
        f" change, use your own git worktree at .worktrees/{name} (already created, on branch {name}); don't"
        " edit the main checkout or another agent's worktree (you may read them). Edit files with your"
        " file-editing tools, not shell commands such as cp, mv, sed or cat. Pre-approved shell commands: git"
        " worktree, git diff, git add, git commit and python -m pytest, each run as a command of its own"
        " (a chain such as `cd ... && git ...` asks for approval). Run the tests from here with"
        f" `python -m pytest -q .worktrees/{name}`; for git in your worktree, first run"
        f" `cd .worktrees/{name}` as a command of its own. Anything else may ask for an approval that"
        " nobody will answer during this rehearsal, so avoid it; if a command needs approval, skip it and"
        " say so in the room."
    )
    if harness == "codex":
        base += (
            " Committing may need an approval in your sandbox; if it does, leave your changes uncommitted."
        )
    if harness == "devin":
        base += (
            ' After joining, call wait with room "#build" and timeout_s 600. Each time wait returns, act on'
            " the messages, then call wait again with the same arguments. If it returns paused, end your"
            " turn."
        )
    else:
        base += " After joining, reply with one word: joined."
    return base


def scaled(minutes: float) -> float:
    return minutes * 60.0 * SCALE


def _skip_reason() -> str | None:
    if "demo" not in LIVE:
        return "set SWITCHBOARD_LIVE=demo to run the M7 rehearsal"
    for b in ("tmux", "git"):
        if not shutil.which(b, path=REAL_PATH):
            return f"{b} not on PATH"
    if not any(shutil.which(h, path=REAL_PATH) for h in AGENTS):
        return "no agent CLI on PATH"
    return None


if _skip_reason():
    pytest.skip(_skip_reason() or "", allow_module_level=True)


# ------------------------------------------------------------------ the demo
class Agent:
    def __init__(self, harness: str):
        self.harness = harness
        self.name = NAMES[harness]
        self.session = {"claude": "cl", "codex": "cx", "devin": "dv"}[harness]
        self.model: str | None = None
        self.not_run: str | None = None
        self.mid: int | None = None
        self.pid_row: int | None = None
        self.prompt_since: float | None = None
        self.prompt_key: Any = None
        self.last_poke = 0.0


class Demo:
    def __init__(self) -> None:
        base = os.environ.get("SWITCHBOARD_LIVE_DIR") or tempfile.gettempdir()
        self.home = Path(tempfile.mkdtemp(prefix="yk-home-m7-", dir=base))
        (self.home / ".switchboard-test").touch()
        # the workspace sits in a neutral per-user temp path (not the scratchpad, whose name spells a
        # repo path, and not /tmp: Claude keeps a trust entry for it in ~/.claude.json)
        self.ws = Path(tempfile.mkdtemp(prefix="yk-ws-m7-", dir=screens.private_tmp()))
        self.run = self.home
        self.pyenv = self.home / "pyenv"
        self.agent_path = f"{self.pyenv / 'bin'}:{REAL_PATH}"
        self.tmux = Tmux("m7", REAL_PATH)
        self.agents = {h: Agent(h) for h in ("claude", "codex", "devin")}
        for h, a in self.agents.items():
            if h not in AGENTS:
                a.not_run = "not selected (SWITCHBOARD_M7_AGENTS)"
            elif not shutil.which(h, path=REAL_PATH):
                a.not_run = f"not run: {h} not on PATH"
        self.results: dict[str, Any] = {
            "timeline": [],
            "stalls": [],
            "loop_guard_resumes": [],
            "declined_prompts": 0,
            "pokes": [],
            "scale": SCALE,
            "not_run": {"cursor": "not run: not yet tested live"},
        }
        self.broker: subprocess.Popen[bytes] | None = None
        self.server: subprocess.Popen[bytes] | None = None
        self.web: httpx.Client | None = None
        self.port = 0
        self.t0: float | None = None
        self._park_check = 0.0
        self.t_start = time.time()
        self.drift_before = drift.snapshot()
        self.codex_cfg_before = drift.codex_config()
        self.user_daemon_before = USER_DAEMON_SOCK.exists()
        self.sock = str(SOCKDIR / "a.sock")
        self.codex_bin = os.path.realpath(shutil.which("codex", path=REAL_PATH) or "codex")

    # ------------------------------------------------------------- log
    def note(self, what: str, **kw: Any) -> None:
        t = time.time()
        rel = round(t - self.t0, 1) if self.t0 else None
        entry = {"t0_s": rel, "what": what, **kw}
        self.results["timeline"].append(entry)
        print(
            f"[m7 {time.strftime('%H:%M:%S')} T0{'+' if rel is not None and rel >= 0 else ''}"
            f"{rel if rel is not None else '-'}] {what} {kw if kw else ''}",
            flush=True,
        )

    def active(self) -> list[Agent]:
        return [a for a in self.agents.values() if a.not_run is None]

    # ----------------------------------------------------------- setup
    def make_workspace(self) -> None:
        env = clean_env(REAL_PATH, TMPDIR=tempfile.gettempdir())
        git = ["git", "-c", "user.name=switchboard demo", "-c", "user.email=demo@switchboard.invalid"]
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.ws, env=env, check=True)
        subprocess.run(["git", "config", "user.name", "switchboard demo"], cwd=self.ws, env=env, check=True)
        subprocess.run(
            ["git", "config", "user.email", "demo@switchboard.invalid"], cwd=self.ws, env=env, check=True
        )
        (self.ws / "portparse.py").write_text(PORTPARSE)
        (self.ws / "test_portparse.py").write_text(TEST_PORTPARSE)
        (self.ws / "README.md").write_text(README)
        (self.ws / ".gitignore").write_text(".worktrees/\n__pycache__/\n.pytest_cache/\n.codex/\n.devin/\n")
        subprocess.run(git + ["add", "-A"], cwd=self.ws, env=env, check=True)
        subprocess.run(
            git + ["commit", "-qm", "portparse: parse_port without validation"],
            cwd=self.ws,
            env=env,
            check=True,
        )
        remotes = subprocess.run(
            ["git", "remote"], cwd=self.ws, env=env, capture_output=True, text=True
        ).stdout
        assert remotes.strip() == "", "the scratch repo must have no remote"
        for a in self.agents.values():
            subprocess.run(
                ["git", "worktree", "add", "-q", f".worktrees/{a.name}", "-b", a.name],
                cwd=self.ws,
                env=env,
                check=True,
            )
        (self.ws / ".git" / "hooks").mkdir(exist_ok=True)
        # a private Python with pytest for the agents (nothing global): `python -m pytest` works
        uv = shutil.which("uv", path=REAL_PATH) or "uv"
        subprocess.run(
            [uv, "venv", str(self.pyenv), "--python", "3.13", "-q"], env=env, check=True, timeout=120
        )
        subprocess.run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(self.pyenv / "bin" / "python"),
                "--offline",
                "-q",
                "pytest",
            ],
            env=env,
            check=True,
            timeout=120,
        )
        r = subprocess.run(
            ["python", "-m", "pytest", "-q", f".worktrees/{NAMES['claude']}"],
            cwd=self.ws,
            env=clean_env(self.agent_path),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        self.results["baseline_tests"] = r.stdout.strip().splitlines()[-1]

    def start_broker(self) -> None:
        self.sessions_dir = Path(REAL_HOME) / ".claude" / "sessions"
        cfg = (
            'human_name = "alice"\n\n'  # the name the prompts use
            f"[delivery]\nbudget_per_hour = {BUDGET}\nhop_limit = {HOP_LIMIT}\n\n"
            f'[claude]\nsessions_dir = "{self.sessions_dir}"\n\n'
            f'[codex]\ncontrol_socket = "{self.sock}"\nbin = "{self.codex_bin}"\n'
        )
        (self.home / "config.toml").write_text(cfg)
        env = clean_env(REAL_PATH, TMPDIR=tempfile.gettempdir())
        benv = {**env, "SWITCHBOARD_TEST": "1", "SWITCHBOARD_RECORD_PAYLOADS": str(self.run / "params")}
        out = open(self.run / "broker.stdout", "ab")
        self.broker = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "switchboard",
                "start",
                "--foreground",
                "--test-mode",
                "--home",
                str(self.home),
                "--port",
                "0",
            ],
            env=benv,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            start_new_session=True,
        )
        from switchboard.mcp.client import ping
        from switchboard.paths import Paths

        self.paths = Paths.from_home(self.home)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            info = ping(self.paths.sock, timeout=1.0)
            if info:
                self.port = info["port"]
                break
            time.sleep(0.1)
        assert self.port, "broker did not start"
        base = f"http://switchboard.localhost:{self.port}"
        self.web = httpx.Client(base_url=base, timeout=10.0, follow_redirects=False)
        tok = (self.home / "run" / "test-login-token").read_text().strip()
        assert self.web.get(f"/login?t={tok}").status_code == 303
        self.hdr = {"Origin": base, "X-Switchboard": "1", "Content-Type": "application/json"}
        assert self.web.post("/api/rooms", json={"name": "#build"}, headers=self.hdr).status_code == 200
        room = self.q("SELECT budget_per_hour, budget_remaining, hop_limit FROM rooms WHERE name='#build'")[0]
        assert (room["budget_per_hour"], room["budget_remaining"], room["hop_limit"]) == (
            BUDGET,
            BUDGET,
            HOP_LIMIT,
        )

    def print_args(self, harness: str) -> dict[str, Any]:
        env = clean_env(REAL_PATH, TMPDIR=tempfile.gettempdir(), SWITCHBOARD_TEST="1")
        pa = subprocess.run(
            [
                sys.executable,
                "-m",
                "switchboard",
                "install",
                harness,
                "--print-args",
                "--home",
                str(self.home),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return json.loads(pa.stdout)

    # ----------------------------------------------------------- Claude
    def launch_claude(self, a: Agent, model: str) -> None:
        pa = self.print_args("claude")
        (self.run / "mcp.json").write_text(json.dumps(profiles.claude_mcp_config(pa)))
        settings = profiles.claude_settings(pa, None)
        settings["permissions"] = {
            "allow": list(CLAUDE_BASH_ALLOW),
            "deny": list(profiles.TEST_DENY) + CLAUDE_DENY,
        }
        (self.run / "settings.json").write_text(json.dumps(settings))
        claude_bin = shutil.which("claude", path=REAL_PATH) or "claude"
        argv = [
            claude_bin,
            "--model",
            model,
            "--setting-sources",
            "project,local",
            "--strict-mcp-config",
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            ",".join(profiles.SWITCHBOARD_TOOLS + CLAUDE_BASH_ALLOW),
            "--mcp-config",
            str(self.run / "mcp.json"),
            "--settings",
            str(self.run / "settings.json"),
        ]
        self.tmux.new_session(
            a.session, str(self.ws), clean_env(self.agent_path, DISABLE_AUTOUPDATER="1"), argv
        )
        a.model = model

    def claude_ready(self, a: Agent, timeout: float = 90) -> str | None:
        """None when ready; else why not. Answers only the trust dialog for our own scratch folder."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s = self.tmux.capture(a.session)
            if "Yes, I trust this folder" in s:
                assert "yk-ws-m7-" in s, "trust dialog for an unexpected folder"
                if re.search(r"❯\s*(\d\.\s*)?Yes, I trust this folder", s):
                    self.tmux.key(a.session, "Enter")
                    time.sleep(1.5)
                else:
                    self.tmux.key(a.session, "Down")
                    time.sleep(0.4)
                continue
            if "Claude in Chrome" in s:
                self.tmux.key(a.session, "Escape")
                time.sleep(1)
                continue
            if "Yes, I accept" in s:
                raise AssertionError("a bypass-mode warning dialog appeared; never answered")
            if screens.claude_model_error(s):
                return "model unavailable"
            if self.tmux.pane_dead(a.session):
                return "claude exited"
            if re.search(r"(shortcuts|manual mode|accept edits|bypass permissions)", s):
                if "bypass permissions" in s:
                    raise AssertionError("claude came up in bypass mode")
                return None
            time.sleep(0.5)
        return "claude never became ready"

    # ------------------------------------------------------------ Codex
    def codex_setup(self) -> None:
        SOCKDIR.mkdir(mode=0o700, exist_ok=True)
        st = os.lstat(SOCKDIR)
        assert stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid(), "unsafe socket dir"
        os.chmod(SOCKDIR, 0o700)
        if os.path.lexists(SOCKDIR / "a.sock"):  # a crashed earlier run's link (literal name only)
            (SOCKDIR / "a.sock").unlink()
        pa = self.print_args("codex")
        (self.ws / ".codex").mkdir(exist_ok=True)
        (self.ws / ".codex" / "hooks.json").write_text(pa["files"]["hooks.json"])
        self.overrides = codex_profile.base_overrides(self.ws, pa)
        self.hooks_state, hooks = codex_trust.hooks_state(self.codex_bin, REAL_PATH, self.ws, self.overrides)
        project = [h for h in hooks if h["source"] == "project"]
        assert len(project) == 5, project
        ov = self.overrides + ["-c", f"hooks.state={self.hooks_state}"]
        [res] = codex_trust.stdio_calls(
            self.codex_bin,
            REAL_PATH,
            self.ws,
            ov,
            [("config/read", {"cwd": str(self.ws), "includeLayers": False})],
        )
        cfg = res["result"]["config"]
        assert cfg.get("approval_policy") == "on-request", "approvals must prompt"
        assert cfg.get("sandbox_mode") == "workspace-write", "the sandbox must be workspace-write"
        on = [
            k
            for k, v in (cfg.get("mcp_servers") or {}).items()
            if not isinstance(v, dict) or v.get("enabled", True)
        ]
        assert on == ["switchboard"], f"other MCP servers enabled: {len(on) - 1}"
        plugins_on = [
            k for k, v in (cfg.get("plugins") or {}).items() if isinstance(v, dict) and v.get("enabled")
        ]
        assert plugins_on == [], "plugins enabled"
        assert (cfg.get("features") or {}).get("apps") is False

    def launch_codex(self, a: Agent) -> None:
        self.codex_setup()
        out = open(self.run / "appserver.out", "ab")
        self.server = subprocess.Popen(
            codex_profile.app_server_argv(self.codex_bin, self.sock, self.overrides, self.hooks_state),
            cwd=str(self.ws),
            env=clean_env(self.agent_path),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            start_new_session=True,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not os.path.exists(self.sock):
            time.sleep(0.1)
        assert os.path.exists(self.sock), "the private app-server did not listen"
        slugs = codex_profile.model_slugs()
        model = CODEX_MODEL if (not slugs or CODEX_MODEL in slugs) else "gpt-6-luna"
        argv = [
            self.codex_bin,
            "--remote",
            f"unix://{self.sock}",
            "-a",
            "on-request",
            "-s",
            "workspace-write",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{CODEX_EFFORT}"',
            *codex_profile.NO_UPDATE_CHECK,
        ]
        self.tmux.new_session(a.session, str(self.ws), clean_env(self.agent_path), argv)
        a.model = f"{model} ({CODEX_EFFORT})"

    def skip_codex_dialogs(self, a: Agent, s: str) -> bool:
        if "Update available" in s and "esc skip" in s:
            key = "update_dialog_skipped"  # Esc = skip; never "Update now"
        elif "Hooks need review" in s:
            key = "hooks_dialog_skipped"
        elif "esc close" in s and "trust all" in s:
            key = "hooks_panel_closed"
        else:
            return False
        self.tmux.key(a.session, "Escape")  # continue without trusting: declines, trusts nothing
        self.results[key] = self.results.get(key, 0) + 1
        time.sleep(1.5)
        return True

    def codex_ready(self, a: Agent) -> str | None:
        deadline = time.monotonic() + 90
        ready_since: float | None = None
        while time.monotonic() < deadline:
            s = self.tmux.capture(a.session)
            if self.skip_codex_dialogs(a, s):
                ready_since = None
                continue
            if "Trust this folder" in s or "trust this directory" in s.lower():
                raise AssertionError("a folder-trust dialog appeared; never answered")
            if self.tmux.pane_dead(a.session):
                return "codex exited"
            if "Ask Codex to do anything" in s:
                ready_since = ready_since or time.monotonic()
                if time.monotonic() - ready_since >= 3.0:
                    break
            else:
                ready_since = None
            time.sleep(0.5)
        else:
            return "codex never became ready"
        self.tmux.type(a.session, "/status")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            x = self.tmux.capture(a.session)
            if self.skip_codex_dialogs(a, x):
                self.tmux.type(a.session, "/status")
                continue
            if "Permissions:" in x:
                perm = next(line for line in x.splitlines() if "Permissions:" in line)
                server = next((line for line in x.splitlines() if "Server:" in line), "")
                assert "Ask for approval" in perm and "YOLO" not in x and "Full access" not in perm, perm
                assert self.sock in server, "the TUI isn't on the private app-server"
                self.results["codex_tui_permissions"] = perm.split("Permissions:")[1].strip(" │")
                return None
            time.sleep(0.5)
        return "codex /status never showed"

    # ------------------------------------------------------------ Devin
    def launch_devin(self, a: Agent) -> None:
        pa = self.print_args("devin")
        d = self.ws / ".devin"
        d.mkdir(exist_ok=True)
        cfg = json.loads(pa["files"][".devin/config.json"])
        cfg["read_config_from"] = {k: False for k in profiles.DEVIN_READ_CONFIG_FROM}
        cfg.setdefault("permissions", {}).setdefault("allow", []).extend(DEVIN_EXEC_ALLOW)
        (d / "config.json").write_text(json.dumps(cfg, indent=2))
        (d / "mcp_config.json").write_text(pa["files"][".devin/mcp_config.json"])
        devin_bin = shutil.which("devin", path=REAL_PATH) or "devin"
        argv = [
            devin_bin,
            "--model",
            "swe-1-6-slow",
            "--respect-workspace-trust",
            "false",
            "--permission-mode",
            "accept-edits",
        ]
        self.tmux.new_session(a.session, str(self.ws), clean_env(self.agent_path), argv)
        a.model = "swe-1-6-slow"

    def devin_ready(self, a: Agent) -> str | None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            s = self.tmux.capture(a.session)
            if re.search(r"connection failed|GetCliModelConfigs", s, re.I):
                return "not run: sandbox (devin can't reach its service from here)"
            if screens.devin_quota_exhausted(s):
                return "not run: Devin quota exhausted"
            if self.tmux.pane_dead(a.session):
                return "not run: devin exited at start"
            if "Ask Devin to" in s:
                return None
            time.sleep(0.5)
        return "not run: devin never became ready"

    def ws_config(self) -> dict[str, str]:
        """Every harness's project config and git's hooks/config in the workspace, by content hash."""
        out: dict[str, str] = {}
        for rel in WS_CONFIG:
            root = self.ws / rel
            if root.is_dir():
                files = sorted(x for x in root.rglob("*") if x.is_file())
            else:
                files = [root] if root.is_file() else []
            for f in files:
                try:
                    out[str(f.relative_to(self.ws))] = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
                except OSError:
                    out[str(f.relative_to(self.ws))] = "unreadable"
        return out

    def devin_events(self) -> list[dict[str, Any]]:
        try:
            return [
                json.loads(x) for x in (self.run / "params" / "devin.jsonl").read_text().splitlines() if x
            ]
        except (OSError, ValueError):
            return []

    def devin_listening(self) -> bool:
        ev = self.devin_events()
        return (
            bool(ev)
            and ev[-1]["event"] == "PreToolUse"
            and ev[-1]["params"].get("tool") == "mcp__switchboard__wait"
        )

    # ------------------------------------------------------------ joining
    def launch_all(self) -> None:
        for a in self.active():
            try:
                if a.harness == "claude":
                    self.launch_claude(a, CLAUDE_MODEL)
                elif a.harness == "codex":
                    self.launch_codex(a)
                else:
                    self.launch_devin(a)
            except (subprocess.SubprocessError, OSError, AssertionError) as e:
                if a.harness == "codex" and isinstance(e, AssertionError):
                    raise  # a preflight failure: never run Codex outside the safe profile
                a.not_run = f"not run: launch failed ({type(e).__name__})"
                self.note("launch failed", agent=a.name, why=a.not_run)
        for a in self.active():
            if a.harness == "claude":
                why = self.claude_ready(a)
                if why == "model unavailable" and a.model != "haiku":
                    self.note("claude model unavailable; falling back", frm=a.model, to="haiku")
                    self.tmux.run("kill-session", "-t", a.session)
                    self.launch_claude(a, "haiku")
                    why = self.claude_ready(a)
            elif a.harness == "codex":
                why = self.codex_ready(a)
            else:
                why = self.devin_ready(a)
            if why is not None:
                a.not_run = why if why.startswith("not run") else f"not run: {why}"
                self.note("agent not ready", agent=a.name, why=a.not_run)
                (self.run / f"not-ready-{a.name}.txt").write_text(
                    self.tmux.capture(a.session)
                )  # scratch only
                self.tmux.run("kill-session", "-t", a.session)
                continue
            self.tmux.type(a.session, join_prompt(a.harness, a.name))
            self.note("join prompt typed", agent=a.name, model=a.model)
        self.ws_config_before = self.ws_config()  # every harness's config is written by now
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            pending = [a for a in self.active() if a.mid is None]
            if not pending:
                break
            for a in pending:
                if (
                    a.harness == "claude"
                    and a.model != "haiku"
                    and screens.claude_model_error(self.tmux.capture(a.session))
                ):
                    self.claude_fallback(a)
                    continue
                self.handle_prompt(a)
                rows = self.q(
                    "SELECT m.id, m.participant_id FROM memberships m WHERE m.screen_name=?"
                    " AND m.left_at IS NULL",
                    a.name,
                )
                if rows:
                    a.mid, a.pid_row = rows[0]["id"], rows[0]["participant_id"]
                    self.note("joined", agent=a.name, tier=self.part(a)["tier"])
            time.sleep(1.0)
        for a in self.active():
            if a.mid is None:
                a.not_run = "not run: never joined within 4 min"
                self.note("never joined", agent=a.name, screen=self.tmux.screen(a.session, 8)[-400:])
        # the tier each harness should reach, and the process-tree preflight
        for a in self.active():
            want = {"claude": "claude:inbox", "codex": "codex:daemon", "devin": "devin:wait-loop"}[a.harness]
            ok = self.wait_for(
                lambda a=a, want=want: self.part(a)["tier"] == want and not self.part(a)["tier_note"], 60
            )
            p = self.part(a)
            self.results.setdefault("tiers", {})[a.name] = {
                "tier": p["tier"],
                "note": p["tier_note"],
                "approval_mode": p["approval_mode"],
                "reached": ok,
            }
            root = (
                self.server.pid
                if a.harness == "codex" and self.server
                else (self.tmux.pane_pid(a.session) or -1)
            )
            bad = preflight.problems(root)
            assert bad == [], f"preflight: unexpected MCP servers/helpers under {a.name}: {bad}"
            if a.harness == "claude":
                assert p["approval_mode"] == "prompting", "claude must not run with approvals off"
        assert self.active(), "no agent joined"

    def claude_fallback(self, a: Agent) -> None:
        """Claude said its model can't be used after the join prompt (the first API call): relaunch
        it on haiku and type the join prompt again."""
        self.note("claude model unavailable after the join prompt; falling back", frm=a.model, to="haiku")
        self.results["claude_model_fallback"] = {"from": a.model, "to": "haiku"}
        self.tmux.run("kill-session", "-t", a.session)
        self.launch_claude(a, "haiku")
        why = self.claude_ready(a)
        if why is not None:
            a.not_run = f"not run: {why} (after falling back to haiku)"
            self.note("agent not ready", agent=a.name, why=a.not_run)
            self.tmux.run("kill-session", "-t", a.session)
            return
        self.tmux.type(a.session, join_prompt(a.harness, a.name))
        self.note("join prompt typed", agent=a.name, model=a.model)

    def wait_settled(self, timeout: float) -> bool:
        """Every active agent idle (Devin: listening in wait()) with nothing offered."""

        def ok() -> bool:
            for a in self.active():
                self.handle_prompt(a)
                p = self.part(a)
                if a.harness == "devin":
                    if not self.devin_listening():
                        return False
                elif p["status"] != "idle":
                    return False
            return True

        return self.wait_for(ok, timeout, step=1.0)

    # --------------------------------------------------------- observing
    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        con = sqlite3.connect(f"file:{self.paths.db}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def part(self, a: Agent) -> sqlite3.Row:
        return self.q("SELECT * FROM participants WHERE id=?", a.pid_row)[0]

    def wait_for(self, fn: Any, timeout: float, step: float = 0.5) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if fn():
                return True
            time.sleep(step)
        return False

    def room(self) -> sqlite3.Row:
        return self.q("SELECT * FROM rooms WHERE name='#build'")[0]

    # ----------------------------------------------------------- the human
    def say(self, text: str) -> int:
        r = self.web.post("/api/rooms/build/say", json={"text": text}, headers=self.hdr)
        assert r.status_code == 200, r.text
        return r.json()["id"]

    def command(self, text: str) -> dict[str, Any]:
        r = self.web.post("/api/rooms/build/command", json={"text": text}, headers=self.hdr)
        assert r.status_code == 200, r.text
        return r.json()

    def post(self, label: str, text: str) -> int:
        mid = self.say(text)
        self.note("human post", label=label, id=mid)
        time.sleep(0.5)
        r = self.room()
        if r["paused"] and r["paused_reason"] == "loop guard":
            self.command("/resume")
            self.results["loop_guard_resumes"].append(
                {"after": label, "t0_s": round(time.time() - self.t0, 1)}
            )
            self.note("room was loop-guard paused: /resume", after=label)
            if self.agents["devin"].not_run is None:
                time.sleep(3.0)
                self.poke_if_parked()  # its wait() returned "paused" and its Stop couldn't re-arm
        return mid

    # ------------------------------------------------------ approval prompts
    def prompt_now(self, a: Agent) -> tuple[Any, bool] | None:
        """(an identity for the open prompt, whether its text is on screen), or None.

        Claude and Codex: the prompt on screen, or switchboard's own ``waiting-approval``
        (the Claude registry, the Codex app-server). Devin reports no approval
        state: a tool whose PreToolUse has had no hook after it (a prompt, or a
        tool that hangs), keyed by that hook, so a run of quick tools never adds up."""
        if a.not_run is not None:
            return None
        s = self.tmux.capture(a.session)
        if a.harness == "codex" and self.skip_codex_dialogs(a, s):
            return None
        shown = bool(PROMPT_RE[a.harness].search(s))
        if a.harness == "devin":
            ev = self.devin_events()
            if (
                ev
                and ev[-1]["event"] == "PreToolUse"
                and ev[-1]["params"].get("tool") != "mcp__switchboard__wait"
            ):
                return ("devin", len(ev)), shown
            return None
        if shown or (a.pid_row is not None and self.part(a)["status"] == "waiting-approval"):
            return (a.harness,), shown
        return None

    def handle_prompt(self, a: Agent) -> None:
        """An approval prompt open for 60 s is a stall: recorded, then declined with Esc. Never approved."""
        now = time.time()
        got = self.prompt_now(a)
        key = got[0] if got else None
        if key != a.prompt_key:
            a.prompt_key, a.prompt_since = key, (now if key is not None else None)
            if key is not None and got and got[1]:
                self.note("approval prompt open", agent=a.name)
                (self.run / f"prompt-{a.name}-{int(now)}.txt").write_text(
                    self.tmux.capture(a.session)
                )  # scratch
            return
        if key is None or a.prompt_since is None or now - a.prompt_since < STALL_S:
            return
        shown = bool(got and got[1])
        (self.run / f"stall-{a.name}-{int(now)}.txt").write_text(self.tmux.capture(a.session))  # scratch only
        self.results["stalls"].append(
            {
                "agent": a.name,
                "harness": a.harness,
                "t0_s": round(a.prompt_since - self.t0, 1) if self.t0 else None,
                "open_s": round(now - a.prompt_since, 1),
                "prompt_on_screen": shown,
                "then": "declined with Esc",
            }
        )
        self.note(
            "stalled: declining with Esc",
            agent=a.name,
            open_s=round(now - a.prompt_since, 1),
            prompt_on_screen=shown,
        )
        self.tmux.key(a.session, "Escape")
        self.results["declined_prompts"] += 1
        a.prompt_key, a.prompt_since = None, None
        time.sleep(2.0)
        if a.harness == "devin":
            # a declined prompt ends Devin's turn with no Stop hook: nothing re-arms its wait loop
            time.sleep(4.0)
            if not self.devin_listening() and not PROMPT_RE["devin"].search(self.tmux.capture(a.session)):
                self.poke_devin(a, "after a declined prompt")

    def member(self, name: str) -> dict[str, Any]:
        ms = self.web.get("/api/rooms/build/members").json()["members"]
        return next((m for m in ms if m["name"] == name), {})

    def poke_devin(self, a: Agent, why: str) -> None:
        """What the human does for a member shown "parked — needs a poke" (README): type into
        its terminal. Devin can't be woken from outside; its wait() loop needs a turn.

        Never into a selector: Devin's approval prompt confirms its default ("Approve once") on
        Enter, so the text goes in without Enter, and Enter follows only when the screen shows no
        selector and the text landed in the input line; otherwise the line is cleared (or, with a
        selector up, left for the stall handler to decline) and the poke is skipped."""
        snippet = POKE_TEXT[:36]
        a.last_poke = time.time()  # at most one attempt a minute, whatever happens below
        s0 = self.tmux.capture(a.session)
        if screens.devin_selector_open(s0):
            self.note("poke skipped: a selector is open", agent=a.name, why=why)
            return
        self.tmux.send_text(a.session, POKE_TEXT)
        time.sleep(0.6)
        s1 = self.tmux.capture(a.session)
        if screens.devin_selector_open(s1):
            self.note("poke skipped: a selector opened while typing (no Enter)", agent=a.name, why=why)
            return
        if s1.count(snippet) <= s0.count(snippet):
            self.tmux.key(a.session, "C-u")
            self.note("poke skipped: the text didn't reach the input line", agent=a.name, why=why)
            return
        self.tmux.key(a.session, "Enter")
        self.results["pokes"].append(
            {"agent": a.name, "why": why, "t0_s": round(time.time() - self.t0, 1) if self.t0 else None}
        )
        self.note("poked devin", agent=a.name, why=why)

    def poke_if_parked(self) -> None:
        """Devin idle at its prompt (its last hook a Stop), the room live, and the buddy list saying
        parked: poke it, at most once a minute."""
        a = self.agents["devin"]
        if a.not_run is not None or a.mid is None or time.time() - a.last_poke < 60:
            return
        if self.room()["paused"]:
            return
        ev = self.devin_events()
        if not ev or ev[-1]["event"] != "Stop":
            return
        m = self.member(a.name)
        if m.get("parked"):
            self.poke_devin(a, f"parked: {m.get('parked_reason') or '?'}")

    def tick(self) -> None:
        for a in self.active():
            self.handle_prompt(a)
        if time.time() - self._park_check >= 5.0:
            self._park_check = time.time()
            self.poke_if_parked()
        r = self.room()
        paused = (bool(r["paused"]), r["paused_reason"])
        if paused != getattr(self, "_last_paused", (False, None)):
            self.note(
                "room state",
                paused=paused[0],
                reason=paused[1],
                hops=r["hop_count"],
                budget=r["budget_remaining"],
            )
            self._last_paused = paused

    # ------------------------------------------------------------- script
    def run_script(self) -> None:
        settled = self.wait_settled(120)
        self.note("agents settled before T0" if settled else "agents not all settled at T0 (going anyway)")
        self.t0 = time.time()
        self.results["t0"] = self.t0
        hard_end = self.t0 + scaled(HARD_LIMIT_MIN)
        for offset, label, text in SCRIPT:
            at = self.t0 + scaled(offset)
            while time.time() < at:
                self.tick()
                time.sleep(1.0)
            wrap_id = self.post(label, text)
        # wrap-up: give every agent up to two minutes to answer, then /pause
        pause_at = min(time.time() + max(45.0, scaled(WRAP_GRACE_MIN)), hard_end - 60)
        names = {a.name for a in self.active()}
        while time.time() < pause_at:
            self.tick()
            said = {
                r["sender_name"]
                for r in self.q(
                    "SELECT sender_name FROM messages WHERE id>? AND sender_kind='agent' AND kind='chat'",
                    wrap_id,
                )
            }
            passed = {
                r["screen_name"]
                for r in self.q(
                    "SELECT m.screen_name FROM events e JOIN memberships m ON m.id=e.membership_id"
                    " WHERE e.kind='pass' AND e.ts>?",
                    self.q("SELECT ts FROM messages WHERE id=?", wrap_id)[0][0],
                )
            }
            if names <= said | passed:
                self.note("every agent answered the wrap-up")
                break
            time.sleep(1.0)
        self.command("/pause")
        self.note("/pause")
        self.results["paused_at_t0_s"] = round(time.time() - self.t0, 1)
        time.sleep(8.0)  # open waits return "paused"; turns in flight finish their say()s
        self.results["script_s"] = round(time.time() - self.t0, 1)
        assert time.time() <= hard_end + 30, "the 20-minute hard limit was exceeded"

    # ------------------------------------------------------------ outcomes
    def worktree_outcomes(self) -> None:
        env = clean_env(self.agent_path, TMPDIR=tempfile.gettempdir())
        out: dict[str, Any] = {}
        for a in self.agents.values():
            wt = self.ws / ".worktrees" / a.name
            if not wt.is_dir():
                continue

            def git(*args: str, wt: Path = wt) -> str:
                return subprocess.run(
                    ["git", "-C", str(wt), *args], env=env, capture_output=True, text=True, timeout=30
                ).stdout.strip()

            t = subprocess.run(
                ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", f".worktrees/{a.name}"],
                cwd=self.ws,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            last = [x for x in t.stdout.strip().splitlines() if x.strip()]
            out[a.name] = {
                "commits": len([x for x in git("log", "--oneline", f"main..{a.name}").splitlines() if x]),
                "uncommitted_files": len([x for x in git("status", "--porcelain").splitlines() if x]),
                "diff_vs_main": git("diff", "--shortstat", "main"),
                "tests": last[-1] if last else f"exit {t.returncode}",
                "tests_exit": t.returncode,
            }
        self.results["worktrees"] = out

    def transcript(self) -> None:
        rows = self.q("SELECT id, ts, sender_name, sender_kind, kind, text FROM messages ORDER BY id")
        lines = []
        for r in rows:
            rel = r["ts"] - self.t0 if self.t0 else 0.0
            lines.append(
                f"[T0{rel:+7.1f}s] #{r['id']} <{r['sender_name']}> ({r['sender_kind']}/{r['kind']})"
                f" {r['text']}"
            )
        (self.run / "transcript.txt").write_text("\n".join(lines) + "\n")

    # ------------------------------------------------------------- teardown
    def stop(self) -> None:
        try:
            if self.web is not None:
                self.command("/pause")
        except Exception:
            pass
        try:
            if self.web is not None and self.t0 is not None:
                self.transcript()
                self.worktree_outcomes()
        except Exception as e:  # never let the write-up block the teardown
            self.results["outcome_error"] = repr(e)
        self.tmux.kill_server()
        procs = ([self.server] if self.server else []) + ([self.broker] if self.broker else [])
        for p in procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for p in procs:
            try:
                p.wait(15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if self.web is not None:
            self.web.close()
        time.sleep(1.0)
        left: list[str] = []
        for needle in (str(self.home), str(self.ws), str(SOCKDIR)):
            left += subprocess.run(
                ["/usr/bin/pgrep", "-f", needle], capture_output=True, text=True
            ).stdout.split()
        for pid in set(left):
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (ProcessLookupError, ValueError, PermissionError):
                pass
        if os.path.lexists(SOCKDIR / "a.sock"):
            (SOCKDIR / "a.sock").unlink()
        self.results["leftover_processes"] = len(set(left))
        fail, info = drift.compare(self.drift_before, drift.snapshot())
        bad, tui = drift.codex_config_diff(self.codex_cfg_before, drift.codex_config())
        self.results["drift_fail"] = [f for f in fail if f != "~/.codex/config.toml"] + (
            [f"~/.codex/config.toml: {k}" for k in bad]
        )
        self.results["drift_info"] = info + [
            f"~/.codex/config.toml: [{k}] (written by the Codex TUI)" for k in tui
        ]
        self.results["user_daemon_unchanged"] = USER_DAEMON_SOCK.exists() == self.user_daemon_before
        before = getattr(self, "ws_config_before", None)
        if before is not None:
            after = self.ws_config()
            self.results["workspace_config_changed"] = sorted(
                k for k in set(before) | set(after) if before.get(k) != after.get(k)
            )
        self.results["agents"] = {
            a.name: {"harness": a.harness, "model": a.model, "not_run": a.not_run}
            for a in self.agents.values()
        }
        self.results["wall_clock_s"] = round(time.time() - self.t_start, 1)
        (self.run / "results.json").write_text(json.dumps(self.results, indent=1))
        print(
            "\nM7 RESULTS", json.dumps({k: v for k, v in self.results.items() if k != "timeline"}, indent=1)
        )

    def make_report(self) -> dict[str, Any]:
        env = clean_env(REAL_PATH, TMPDIR=tempfile.gettempdir())
        for fmt, name in (("md", "M7-REPORT.md"), ("json", "report.json")):
            argv = [
                sys.executable,
                "-m",
                "switchboard",
                "report",
                "--home",
                str(self.home),
                "--room",
                "#build",
                "--out",
                str(self.run / name),
            ] + (["--json"] if fmt == "json" else [])
            subprocess.run(argv, env=env, check=True, capture_output=True, timeout=60)
        return json.loads((self.run / "report.json").read_text())


@pytest.fixture(scope="module")
def demo():
    d = Demo()
    try:
        d.make_workspace()
        d.start_broker()
        yield d
    finally:
        d.stop()
    assert d.results["drift_fail"] == [], d.results["drift_fail"]
    assert d.results["user_daemon_unchanged"], "the user's Codex daemon state changed"


def test_m7_rehearsal(demo: Demo) -> None:
    demo.launch_all()
    demo.run_script()


def test_m7_report(demo: Demo) -> None:
    """``switchboard report`` on the room as it stands after the /pause (it reads the database directly):
    a latency row for every harness that ran, and nothing personal in it."""
    rep = demo.make_report()
    ran = {a.harness for a in demo.active()}
    demo.results["report_harnesses"] = sorted({r["harness"] for r in rep["latency"]["by_harness"]})
    for h in ran:
        rows = [r for r in rep["latency"]["by_harness"] if r["harness"] == h and r["n"] > 0]
        assert rows, f"no latency rows for {h}"
    blob = (demo.run / "M7-REPORT.md").read_text()
    assert "/Users/" not in blob and "/private/" not in blob and not re.search(r"[\w.+-]+@[\w-]+\.\w+", blob)
