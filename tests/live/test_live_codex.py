"""Live Codex CLI (DESIGN.md §0, §12.4, §13 M4).

Opt-in: ``SWITCHBOARD_LIVE=codex uv run pytest -m live tests/live/test_live_codex.py -s``.

Real ``codex`` on the user's real ``CODEX_HOME`` (its login used as-is), but
never the user's shared daemon: a **private** ``codex app-server --listen
unix://<short tmp path>`` (``A``) started in the scratch workspace with
``-c`` overrides only (``harness/codex_profile.py``), and the TUI attached
with ``codex --remote unix://<A>`` in a private tmux server. A plain ``codex``
or a bare ``codex queue`` is never run. The broker runs in test mode in a
temp ``SWITCHBOARD_HOME`` with ``codex.control_socket`` = A; the driver plays the
human through the web API (and through tmux for what a human types).

Scenarios, in order:
1. join: the thread binds (agent = A), the thread proof, tier ``codex:daemon``;
2. idle wake, n=5: ``turn/start``, turn start (status -> active) p50 < 1 s;
3. a steer during ``sleep 7`` lands at the tool boundary (``turn/steer``);
4. approval hold: a human post is held while an approval prompt is open;
   the driver declines with Esc (never approves), and the post arrives after;
5. the queue tier: a second TUI on a second private app-server (``B``): its
   thread isn't loaded in A, so wakes go through ``codex queue --remote
   unix://<A>`` (n=2), and a mid-task message arrives as PostToolUse context;
6. the app-server restarts (as the managed daemon does when it auto-updates):
   the private app-server A is killed and started again on the same socket
   while TUI 1 is attached with ``--remote``; the member is re-bound to the
   new app-server (same membership, one reconnect notice, no leave) and the
   next idle wake works;
7. the TUI quits: a post within 60 s sends no RPC; what arrives (hooks,
   notifications) is recorded until the thread unloads.

Teardown: every process started here is killed (tmux, both app-servers, the
broker); the drift check allows changes to ``~/.codex/config.toml`` only in
the TUI's own ``notice``/``tui`` tables; the user's daemon state is unchanged.
Results go to ``<run home>/results.json`` and stdout.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from harness import codex_profile, codex_trust, drift, preflight
from harness.tmuxdrv import REAL_HOME, Tmux, clean_env

pytestmark = pytest.mark.live

LIVE = {x.strip() for x in os.environ.get("SWITCHBOARD_LIVE", "").split(",") if x.strip()}
REAL_PATH = os.environ.get("PATH", "/usr/bin:/bin")
T1, T2 = "cx1", "cx2"
SOCKDIR = Path(f"/tmp/yk-cx-live-{os.getuid()}")  # short (sun_path), reused: codex keeps a lock per path
USER_DAEMON_SOCK = Path(REAL_HOME) / ".codex" / "app-server-control" / "app-server-control.sock"
READY = "Ask Codex to do anything"
APPROVAL = "Would you like to run"


def _skip_reason() -> str | None:
    if "codex" not in LIVE:
        return "set SWITCHBOARD_LIVE=codex to run the live Codex test"
    if not shutil.which("codex", path=REAL_PATH):
        return "codex not on PATH"
    if not shutil.which("tmux", path=REAL_PATH):
        return "tmux not on PATH"
    return None


if _skip_reason():
    pytest.skip(_skip_reason() or "", allow_module_level=True)


def pctl(xs: list[float], q: float) -> float:
    if len(xs) == 1:
        return xs[0]
    return statistics.quantiles(xs, n=100, method="inclusive")[int(q) - 1]


# ---------------------------------------------------------------- the world
class Live:
    def __init__(self) -> None:
        base = os.environ.get("SWITCHBOARD_LIVE_DIR") or tempfile.gettempdir()
        self.home = Path(tempfile.mkdtemp(prefix="yk-home-", dir=base))
        (self.home / ".switchboard-test").touch()
        self.ws = Path(tempfile.mkdtemp(prefix="yk-ws-", dir=base))
        self.run = self.home
        self.results: dict[str, Any] = {"scenarios": {}}
        self.codex = os.path.realpath(shutil.which("codex", path=REAL_PATH) or "codex")
        self.tmux = Tmux("codex", REAL_PATH)
        self.broker: subprocess.Popen[bytes] | None = None
        self.servers: dict[str, subprocess.Popen[bytes]] = {}
        self.web: httpx.Client | None = None
        self.port = 0
        self.drift_before = drift.snapshot()
        self.codex_cfg_before = drift.codex_config()
        self.user_daemon_before = USER_DAEMON_SOCK.exists()
        self.sock_a = str(SOCKDIR / "a.sock")
        self.sock_b = str(SOCKDIR / "b.sock")
        self.mids: dict[str, int] = {}  # screen name -> membership id
        self.tids: dict[str, str] = {}  # screen name -> thread id

    # ------------------------------------------------------------- setup
    def _sockdir(self) -> None:
        SOCKDIR.mkdir(mode=0o700, exist_ok=True)
        st = os.lstat(SOCKDIR)
        assert stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid(), "unsafe socket dir"
        os.chmod(SOCKDIR, 0o700)
        for name in ("a.sock", "b.sock"):  # a crashed earlier run's links (literal names only)
            with_path = SOCKDIR / name
            if os.path.lexists(with_path):
                with_path.unlink()

    def start(self) -> None:
        self._sockdir()
        env = clean_env(REAL_PATH, TMPDIR=tempfile.gettempdir())
        subprocess.run(["git", "init", "-q"], cwd=self.ws, env=env, check=True)
        (self.ws / "README.md").write_text("scratch workspace for a switchboard live test\n")
        ver = subprocess.run([self.codex, "--version"], env=env, capture_output=True, text=True, timeout=30)
        self.results["codex_version"] = ver.stdout.strip()
        pa = subprocess.run(
            [
                sys.executable,
                "-m",
                "switchboard",
                "install",
                "codex",
                "--print-args",
                "--home",
                str(self.home),
            ],
            env={**env, "SWITCHBOARD_TEST": "1"},
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        self.print_args = json.loads(pa.stdout)
        (self.ws / ".codex").mkdir()
        (self.ws / ".codex" / "hooks.json").write_text(self.print_args["files"]["hooks.json"])
        (self.home / "config.toml").write_text(  # human_name: the name the prompts use
            f'human_name = "alice"\n[codex]\ncontrol_socket = "{self.sock_a}"\nbin = "{self.codex}"\n'
        )
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
        self.command("/budget 40")
        # the launch profile: overrides, then hook trust for this launch only
        self.overrides = codex_profile.base_overrides(self.ws, self.print_args)
        self.hooks_state, hooks = codex_trust.hooks_state(self.codex, REAL_PATH, self.ws, self.overrides)
        project = [h for h in hooks if h["source"] == "project"]
        assert len(project) == 5, project
        self.results["hooks"] = {"project": len(project), "others_disabled": len(hooks) - len(project)}
        self.preflight_config()

    def preflight_config(self) -> None:
        """Abort unless the effective config prompts for approvals, sandboxes the
        workspace and runs no MCP server or plugin but switchboard's."""
        ov = self.overrides + ["-c", f"hooks.state={self.hooks_state}"]
        [res] = codex_trust.stdio_calls(
            self.codex,
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

    def start_server(self, name: str, sock: str) -> int:
        out = open(self.run / f"appserver-{name}.out", "ab")
        p = subprocess.Popen(
            codex_profile.app_server_argv(self.codex, sock, self.overrides, self.hooks_state),
            cwd=str(self.ws),
            env=clean_env(REAL_PATH),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            start_new_session=True,
        )
        self.servers[name] = p
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not self.answers(sock):
            assert p.poll() is None, f"app-server {name} exited"
            time.sleep(0.2)
        assert self.answers(sock), f"app-server {name} did not listen"
        return p.pid

    def answers(self, sock: str) -> bool:
        """The app-server at ``sock`` answers (a stale link from a killed one doesn't)."""
        from switchboard.adapters.codex_rpc import one_shot

        try:
            asyncio.run(one_shot(sock, lambda r: r.loaded_threads(), timeout=3))
            return True
        except Exception:
            return False

    def launch_tui(self, session: str, sock: str) -> None:
        env = clean_env(REAL_PATH)
        self.tmux.new_session(session, str(self.ws), env, codex_profile.tui_argv(self.codex, sock))
        deadline = time.monotonic() + 90
        ready_since: float | None = None
        while time.monotonic() < deadline:
            s = self.tmux.capture(session)
            if self._skip_hook_dialogs(session, s):
                ready_since = None
                continue
            if "Trust this folder" in s or "trust this directory" in s.lower():
                raise AssertionError("a folder-trust dialog appeared; never answered:\n" + s)
            if READY in s:
                # the hooks dialog can pop up just after the prompt shows: wait for a
                # quiet 3 s before typing, or "/status" + Enter lands in the dialog
                ready_since = ready_since or time.monotonic()
                if time.monotonic() - ready_since >= 3.0:
                    break
            else:
                ready_since = None
            time.sleep(0.5)
        else:
            raise AssertionError("codex never became ready:\n" + self.tmux.screen(session))
        # preflight: the session prompts for approvals and runs on our private server
        self.tmux.type(session, "/status")

        def status_shown() -> str | None:
            x = self.tmux.capture(session)
            if self._skip_hook_dialogs(session, x):
                self.tmux.type(session, "/status")
                return None
            return x if "Permissions:" in x else None

        s = self.wait(status_shown, 30, "/status", session=session)
        perm = next(line for line in s.splitlines() if "Permissions:" in line)
        server = next(line for line in s.splitlines() if "Server:" in line)
        assert "Ask for approval" in perm and "YOLO" not in s and "Full access" not in perm, perm
        assert sock in server, server
        self.results.setdefault("tui_permissions", perm.split("Permissions:")[1].strip(" │"))

    def _skip_hook_dialogs(self, session: str, s: str) -> bool:
        """Any untrusted user hook (even a disabled one) is listed at startup. Esc on the
        "Hooks need review" dialog = continue without trusting (it declines and
        trusts nothing); Esc on the /hooks panel just closes it. Never "t" (trust all)."""
        if "Hooks need review" in s:
            key = "hooks_dialog_skipped"
        elif "esc close" in s and "trust all" in s:
            key = "hooks_panel_closed"
        else:
            return False
        self.tmux.key(session, "Escape")
        self.results[key] = self.results.get(key, 0) + 1
        time.sleep(1.5)
        return True

    def stop(self) -> None:
        try:
            if self.web is not None:
                self.command("/pause")
        except Exception:
            pass
        self.tmux.kill_server()
        for p in list(self.servers.values()) + ([self.broker] if self.broker else []):
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for p in list(self.servers.values()) + ([self.broker] if self.broker else []):
            try:
                p.wait(15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if self.web is not None:
            self.web.close()
        left: list[str] = []
        for needle in (str(self.home), str(SOCKDIR)):
            left += subprocess.run(
                ["/usr/bin/pgrep", "-f", needle], capture_output=True, text=True
            ).stdout.split()
        for pid in left:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (ProcessLookupError, ValueError, PermissionError):
                pass
        for name in ("a.sock", "b.sock"):
            if os.path.lexists(SOCKDIR / name):
                (SOCKDIR / name).unlink()
        self.results["leftover_processes"] = len(left)
        fail, info = drift.compare(self.drift_before, drift.snapshot())
        bad, tui = drift.codex_config_diff(self.codex_cfg_before, drift.codex_config())
        # ~/.codex/config.toml may change only in the TUI's own tables
        self.results["drift_fail"] = [f for f in fail if f != "~/.codex/config.toml"] + (
            [f"~/.codex/config.toml: {k}" for k in bad]
        )
        self.results["drift_info"] = info + [
            f"~/.codex/config.toml: [{k}] (written by the Codex TUI)" for k in tui
        ]
        self.results["user_daemon_unchanged"] = USER_DAEMON_SOCK.exists() == self.user_daemon_before
        (self.run / "results.json").write_text(json.dumps(self.results, indent=1))
        print("\nLIVE RESULTS", json.dumps(self.results, indent=1))

    # ------------------------------------------------------------ the human
    def say(self, text: str) -> int:
        r = self.web.post("/api/rooms/build/say", json={"text": text}, headers=self.hdr)
        assert r.status_code == 200, r.text
        return r.json()["id"]

    def command(self, text: str) -> dict[str, Any]:
        r = self.web.post("/api/rooms/build/command", json={"text": text}, headers=self.hdr)
        assert r.status_code == 200, r.text
        return r.json()

    # ----------------------------------------------------------- observing
    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        con = sqlite3.connect(f"file:{self.paths.db}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def part(self, name: str) -> sqlite3.Row | None:
        rows = self.q(
            "SELECT p.* FROM participants p JOIN memberships m ON m.participant_id=p.id"
            " WHERE m.screen_name=? ORDER BY m.id DESC",
            name,
        )
        return rows[0] if rows else None

    def member(self, name: str) -> dict[str, Any]:
        ms = self.web.get("/api/rooms/build/members").json()["members"]
        return next((m for m in ms if m["name"] == name), {})

    def read_thread(self, sock: str, tid: str, turns: bool = False) -> dict[str, Any]:
        from switchboard.adapters.codex_rpc import one_shot

        return asyncio.run(one_shot(sock, lambda r: r.read_thread(tid, turns)))

    def wait(self, fn: Any, timeout: float, what: str, step: float = 0.1, session: str = T1) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            v = fn()
            if v:
                return v
            time.sleep(step)
        raise AssertionError(f"timed out waiting for {what}\n--- screen ---\n{self.tmux.screen(session)}")

    def decline_stray_prompt(self, session: str) -> None:
        if APPROVAL in self.tmux.capture(session):
            self.tmux.key(session, "Escape")  # decline; the driver never approves
            self.results["stray_prompts_declined"] = self.results.get("stray_prompts_declined", 0) + 1
            time.sleep(1.0)

    def settled(self, name: str, sock: str) -> bool:
        p = self.part(name)
        if p is None or p["status"] != "idle":
            return False
        if self.read_thread(sock, self.tids[name]).get("status", {}).get("type") != "idle":
            return False
        # a peer's message shown as a "call read()" stub stays pending for read() by design
        # (notified, never a wake): not something to wait for
        busy = self.q(
            "SELECT COUNT(*) FROM deliveries WHERE membership_id=? AND (state='offered' OR"
            " (state='pending' AND notified_at IS NULL))",
            self.mids[name],
        )[0][0]
        return busy == 0

    def resume_if_loop_guarded(self) -> None:
        """Two agents answering each other can trip the loop guard; the human resumes (and it's recorded)."""
        r = self.q("SELECT paused, paused_reason FROM rooms WHERE name='#build'")[0]
        if r["paused"] and r["paused_reason"] == "loop guard":
            self.command("/resume")
            self.results["loop_guard_resumes"] = self.results.get("loop_guard_resumes", 0) + 1

    def wait_settled(self, name: str, sock: str, session: str, timeout: float = 120) -> None:
        def ok() -> bool:
            self.decline_stray_prompt(session)
            self.resume_if_loop_guarded()
            return bool(self.settled(name, sock) and (time.sleep(1.5) or self.settled(name, sock)))

        self.wait(ok, timeout, f"{name} to settle", step=0.4, session=session)

    def batch_for(self, msg_id: int, name: str, state: str = "confirmed") -> sqlite3.Row | None:
        rows = self.q(
            "SELECT b.* FROM batches b JOIN deliveries d ON d.batch_id=b.id"
            " WHERE d.message_id=? AND d.membership_id=? AND b.state=?",
            msg_id,
            self.mids[name],
            state,
        )
        return rows[0] if rows else None

    def batches_since(self, name: str, t: float) -> list[sqlite3.Row]:
        return self.q(
            "SELECT * FROM batches WHERE membership_id=? AND created_at>? ORDER BY id", self.mids[name], t
        )

    def msg_ts(self, msg_id: int) -> float:
        return self.q("SELECT ts FROM messages WHERE id=?", msg_id)[0][0]

    def agent_said(self, pattern: str, after: int, name: str | None = None) -> bool:
        rows = self.q(
            "SELECT text, sender_name FROM messages WHERE id>? AND sender_kind='agent' AND kind='chat'", after
        )
        return any(re.search(pattern, r[0]) and (name is None or r[1] == name) for r in rows)

    def sleeping(self, server: str, secs: int) -> bool:
        pid = self.servers[server].pid
        return any(
            re.search(rf"(^|[\s'])sleep {secs}([\s']|$)", a) for _p, _pp, a in preflight.descendants(pid)
        )

    def join(self, session: str, name: str, sock: str) -> sqlite3.Row:
        self.tmux.type(
            session,
            f'Use the switchboard MCP tools: call join with room "#build" and screen_name "{name}".'
            " Then reply with one word: joined. Later, switchboard will relay messages that I (alice)"
            " post in #build; answer each one with the switchboard say tool, or pass, exactly as the"
            " message asks. Keep it short.",
        )
        self.wait(lambda: self.part(name), 120, f"{name} to join", session=session)
        self.mids[name] = self.q("SELECT id FROM memberships WHERE screen_name=? AND left_at IS NULL", name)[
            0
        ][0]
        p = self.part(name)
        self.tids[name] = p["session_key"].removeprefix("codex:")
        self.wait(lambda: self.part(name)["thread_proof"] == 1, 30, f"{name}'s thread proof", session=session)
        return self.part(name)


@pytest.fixture(scope="module")
def live():
    w = Live()
    try:
        w.start()
        w.start_server("a", w.sock_a)
        w.launch_tui(T1, w.sock_a)
        yield w
    finally:
        w.stop()
    assert w.results["drift_fail"] == [], w.results["drift_fail"]
    assert w.results["user_daemon_unchanged"], "the user's Codex daemon state changed"


# ------------------------------------------------------------------ scenarios
def test_1_join_binds_the_thread_with_the_daemon_tier(live: Live) -> None:
    p = live.join(T1, "codex-1", live.sock_a)
    assert p["agent_pid"] == live.servers["a"].pid, (p["agent_pid"], live.servers["a"].pid)
    live.wait(
        lambda: live.part("codex-1")["tier"] == "codex:daemon" and live.part("codex-1")["tier_note"] is None,
        20,
        "the codex:daemon tier",
    )
    live.wait_settled("codex-1", live.sock_a, T1)
    p = live.part("codex-1")
    assert p["approval_mode"] == "prompting" and p["hooks_seen_at"] is not None, dict(p)
    bad = preflight.problems(live.servers["a"].pid)
    assert bad == [], f"preflight: unexpected MCP servers/helpers under the app-server: {bad}"
    ev = live.q("SELECT data FROM events WHERE kind='bind' AND participant_id=? ORDER BY id", p["id"])
    proof = [json.loads(x[0]) for x in ev if json.loads(x[0]).get("what") == "thread_proof"]
    live.results["scenarios"]["join"] = {
        "tier": p["tier"],
        "approval_mode": p["approval_mode"],
        "thread_proof": proof[-1] if proof else None,
    }
    assert proof and proof[-1]["ok"] is True


def test_2_idle_wake_turn_start_p50_under_1s(live: Live) -> None:
    lat, posted, replies = [], [], 0
    for i in range(1, 6):
        live.wait_settled("codex-1", live.sock_a, T1)
        mid = live.say(f"ping {i}: reply in #build with the switchboard say tool, text exactly: pong {i}")
        b = live.wait(lambda mid=mid: live.batch_for(mid, "codex-1"), 30, f"idle wake {i}")
        assert (
            b["path"] == "turn_start" and b["wake_kind"] == "idle_wake" and b["evidence"] == "rpc:turn/start"
        )
        ts = live.msg_ts(mid)
        lat.append(b["turn_start_at"] - ts)
        posted.append(b["posted_at"] - ts)
    live.wait_settled("codex-1", live.sock_a, T1)
    replies = sum(1 for i in range(1, 6) if live.agent_said(rf"\bpong {i}\b", 0, "codex-1"))
    res = {
        "n": len(lat),
        "turn_start_ms": [round(x * 1000, 1) for x in lat],
        "turn_start_p50_ms": round(pctl(lat, 50) * 1000, 1),
        "turn_start_p95_ms": round(pctl(lat, 95) * 1000, 1),
        "posted_p50_ms": round(pctl(posted, 50) * 1000, 1),
        "replied_with_say": replies,
    }
    live.results["scenarios"]["idle_wake"] = res
    print("idle wake", res)
    assert pctl(lat, 50) < 1.0, res


def test_3_steer_lands_at_the_tool_boundary(live: Live) -> None:
    live.wait_settled("codex-1", live.sock_a, T1)
    tag = secrets.token_hex(3)
    live.tmux.type(
        T1,
        f"Run the shell command `sleep 7`, then run the shell command `echo done-{tag}`, then reply"
        " with one word: finished.",
    )
    live.wait(lambda: live.sleeping("a", 7), 60, "the sleep to start")
    t_sleep = time.time()
    time.sleep(1.0)
    mid = live.say(
        f"MID-{tag}: when you read this, call the switchboard say tool with the text: ack MID-{tag}"
    )
    ts = live.msg_ts(mid)
    b = live.wait(lambda: live.batch_for(mid, "codex-1"), 40, "the steer to be confirmed")
    live.wait_settled("codex-1", live.sock_a, T1, 150)
    th = live.read_thread(live.sock_a, live.tids["codex-1"], True)
    turn = next(
        t for t in reversed(th["turns"]) if any(i.get("clientId") == f"yk-b{b['id']}" for i in t["items"])
    )
    kinds = [
        (i.get("type"), i.get("clientId") == f"yk-b{b['id']}", "sleep 7" in json.dumps(i.get("command")))
        for i in turn["items"]
    ]
    i_steer = next(k for k, x in enumerate(kinds) if x[1])
    i_sleep = next(k for k, x in enumerate(kinds) if x[0] == "commandExecution" and x[2])
    res = {
        "path": b["path"],
        "evidence": b["evidence"],
        "posted_ms": round((b["posted_at"] - ts) * 1000, 1),
        "in_context_ms": round((b["confirmed_at"] - ts) * 1000, 1),
        "sleep_to_in_context_s": round(b["confirmed_at"] - t_sleep, 2),
        "steer_after_sleep_in_same_turn": i_steer > i_sleep,
        "items": [x[0] for x in kinds],
        "model_acked": live.agent_said(rf"ack MID-{tag}", mid, "codex-1"),
    }
    live.results["scenarios"]["steer"] = res
    print("steer", res)
    assert b["path"] == "steer" and b["kind"] == "priority", res
    assert i_steer > i_sleep and turn["items"][-1]["type"] == "agentMessage", res
    assert b["confirmed_at"] - t_sleep >= 6.0, res  # not before the sleep ended: at the boundary


def test_4_approval_hold_then_decline_with_esc(live: Live) -> None:
    live.wait_settled("codex-1", live.sock_a, T1, 150)
    tag = secrets.token_hex(3)
    outside = live.run / f"yk-perm-{tag}.txt"  # outside the workspace: the sandbox needs an escalation
    live.tmux.type(
        T1,
        f"Run exactly this shell command: `touch {outside}`. It writes outside the workspace, so"
        " request escalated permissions for that one command. Then reply done.",
    )
    live.wait(lambda: APPROVAL in live.tmux.capture(T1), 90, "the approval prompt")
    live.wait(lambda: live.part("codex-1")["status"] == "waiting-approval", 10, "the waiting-approval hold")
    mid = live.say(f"HOLD-{tag}: call the switchboard say tool with the text: got HOLD-{tag}")
    t_post = live.msg_ts(mid)
    time.sleep(7.0)
    held = {
        "status": live.part("codex-1")["status"],
        "delivery": live.q(
            "SELECT state FROM deliveries WHERE message_id=? AND membership_id=?", mid, live.mids["codex-1"]
        )[0][0],
        "batches_since": len(live.batches_since("codex-1", t_post - 0.01)),
        "prompt_still_open": APPROVAL in live.tmux.capture(T1),
    }
    assert held == {
        "status": "waiting-approval",
        "delivery": "pending",
        "batches_since": 0,
        "prompt_still_open": True,
    }, held
    t_esc = time.time()
    live.tmux.key(T1, "Escape")  # decline; the driver never approves
    b = live.wait(lambda: live.batch_for(mid, "codex-1"), 40, "the held message after the Esc")
    res = {
        "held_while_prompt_open": held,
        "path": b["path"],
        "esc_to_turn_start_ms": round((b["turn_start_at"] - t_esc) * 1000, 1) if b["turn_start_at"] else None,
        "file_created": outside.exists(),
        "reroutes": len(
            [x for x in live.batches_since("codex-1", t_esc - 0.01) if x["expire_reason"] == "reroute"]
        ),
    }
    live.wait_settled("codex-1", live.sock_a, T1, 150)
    res["model_acked"] = live.agent_said(rf"got HOLD-{tag}", mid, "codex-1")
    live.results["scenarios"]["approval_hold"] = res
    print("approval hold", res)
    assert b["path"] == "turn_start" and not res["file_created"], res


def test_5_queue_tier_on_a_second_app_server(live: Live) -> None:
    live.wait_settled("codex-1", live.sock_a, T1, 150)
    live.command("/hold codex-1")  # keep codex-1 out of this scenario (its messages wait)
    live.start_server("b", live.sock_b)
    live.launch_tui(T2, live.sock_b)
    p = live.join(T2, "codex-2", live.sock_b)
    assert p["agent_pid"] == live.servers["b"].pid
    live.wait(
        lambda: live.part("codex-2")["tier"] == "codex:queue" and live.part("codex-2")["tier_note"] is None,
        30,
        "the codex:queue tier",
        session=T2,
    )
    lat = []
    for i in range(1, 3):
        live.wait_settled("codex-2", live.sock_b, T2)
        mid = live.say(
            f"@codex-2 q{i}: reply in #build with the switchboard say tool, text exactly: qpong {i}"
        )
        b = live.wait(lambda mid=mid: live.batch_for(mid, "codex-2"), 40, f"queue wake {i}", session=T2)
        assert b["path"] == "queue" and b["evidence"] == "hook:UserPromptSubmit" and b["turn_start_at"], dict(
            b
        )
        lat.append(b["turn_start_at"] - live.msg_ts(mid))
    live.wait_settled("codex-2", live.sock_b, T2)
    # mid-task: no steer path on the queue tier; PostToolUse context (best effort)
    tag = secrets.token_hex(3)
    live.tmux.type(
        T2,
        f"Run the shell command `sleep 7`, then run the shell command `echo done-{tag}`, then reply"
        " with one word: finished.",
    )
    live.wait(lambda: live.sleeping("b", 7), 60, "the sleep to start", session=T2)
    time.sleep(1.0)
    mid = live.say(
        f"@codex-2 PTU-{tag}: when you read this, call the switchboard say tool with the text: ack PTU-{tag}"
    )
    b = live.wait(lambda: live.batch_for(mid, "codex-2"), 40, "the PostToolUse context", session=T2)
    live.wait_settled("codex-2", live.sock_b, T2, 150)
    res = {
        "n": len(lat),
        "queue_turn_start_ms": [round(x * 1000, 1) for x in lat],
        "queue_turn_start_p50_ms": round(pctl(lat, 50) * 1000, 1),
        "replied_with_say": sum(1 for i in (1, 2) if live.agent_said(rf"\bqpong {i}\b", 0, "codex-2")),
        "posttooluse": {
            "path": b["path"],
            "evidence": b["evidence"],
            "in_context_ms": round((b["confirmed_at"] - live.msg_ts(mid)) * 1000, 1),
            "model_acked": live.agent_said(rf"ack PTU-{tag}", mid, "codex-2"),
        },
    }
    live.results["scenarios"]["queue"] = res
    print("queue", res)
    assert b["path"] == "hook_ctx" and b["evidence"] == "hook_ack", res
    live.command("/release codex-1")
    live.wait_settled("codex-1", live.sock_a, T1, 150)


def test_6_app_server_restart_rebinds_the_member(live: Live) -> None:
    """The managed daemon auto-updates and restarts (a new pid, the same socket);
    its TUIs reconnect and keep their thread id (seen on macOS with 0.157.0).
    Here with the PRIVATE app-server A (never the user's daemon): killed and
    started again on the same socket while TUI 1 is attached with --remote."""
    live.wait_settled("codex-1", live.sock_a, T1, 150)
    live.command("/hold codex-2")  # keep codex-2 (on server B) out of it: its messages wait
    tid = live.tids["codex-1"]
    p0 = live.part("codex-1")
    [m0] = live.q("SELECT id, cred_hash FROM memberships WHERE screen_name='codex-1' AND left_at IS NULL")
    old = live.servers["a"]
    assert p0["agent_pid"] == old.pid
    t_kill = time.time()
    os.killpg(
        old.pid, signal.SIGTERM
    )  # the app-server and its children (MCP server, hooks), as a restart does
    old.wait(15)
    if os.path.lexists(SOCKDIR / "a.sock"):
        (SOCKDIR / "a.sock").unlink()  # its stale link (a literal path in our own socket dir)
    new_pid = live.start_server("a", live.sock_a)
    t_new = time.time()
    saw = {"restarting": False}

    def rebound() -> bool:
        p = live.part("codex-1")
        saw["restarting"] |= p["tier_note"] == "Codex daemon restarting"
        live.decline_stray_prompt(T1)
        return p["agent_pid"] == new_pid

    live.wait(rebound, 60, "codex-1 to be re-bound to the new app-server", step=0.2)
    t_rebound = time.time()
    live.wait(
        lambda: (live.part("codex-1")["tier"], live.part("codex-1")["tier_note"]) == ("codex:daemon", None),
        40,
        "the codex:daemon tier after the restart",
    )
    [m1] = live.q("SELECT id, cred_hash FROM memberships WHERE screen_name='codex-1' AND left_at IS NULL")
    leaves = live.q("SELECT text FROM messages WHERE kind='leave' AND sender_name='codex-1' AND ts>?", t_kill)
    notes = [
        r[0]
        for r in live.q("SELECT text FROM messages WHERE kind='notice' AND ts>?", t_kill)
        if "reconnected after a Codex daemon restart" in r[0]
    ]
    ev = [
        json.loads(r[0])
        for r in live.q("SELECT data FROM events WHERE kind='codex_restart' AND ts>? ORDER BY id", t_kill)
    ]
    bad = preflight.problems(new_pid)
    assert bad == [], f"preflight: unexpected MCP servers/helpers under the new app-server: {bad}"
    # a wake after the restart (the new MCP server holds no credential: the agent may join again first)
    live.wait_settled("codex-1", live.sock_a, T1, 150)
    tag = secrets.token_hex(3)
    mid = live.say(
        f"RESTART-{tag}: reply in #build with the switchboard say tool, text exactly:"
        f" back {tag}. If switchboard"
        ' says you are not in #build, call join with room "#build" and screen_name "codex-1" first.'
    )
    b = live.wait(lambda: live.batch_for(mid, "codex-1"), 40, "the wake after the restart")
    live.wait_settled("codex-1", live.sock_a, T1, 150)
    res = {
        "old_app_server_killed_to_new_listening_s": round(t_new - t_kill, 2),
        "kill_to_rebound_s": round(t_rebound - t_kill, 2),
        "shown_restarting": saw["restarting"],
        "same_membership": (m1["id"], m1["cred_hash"]) == (m0["id"], m0["cred_hash"]),
        "same_thread": live.part("codex-1")["session_key"] == f"codex:{tid}",
        "leave_lines": len(leaves),
        "reconnect_notices": len(notes),
        "events": [(e.get("what"), e.get("via")) for e in ev],
        "wake": {
            "path": b["path"],
            "evidence": b["evidence"],
            "turn_start_ms": round((b["turn_start_at"] - live.msg_ts(mid)) * 1000, 1)
            if b["turn_start_at"]
            else None,
        },
        "model_replied": live.agent_said(rf"back {tag}", mid, "codex-1"),
        "rejoined": len(
            live.q("SELECT id FROM events WHERE kind='bind' AND ts>? AND data LIKE '%rotate%'", t_rebound)
        )
        > 0,
    }
    live.results["scenarios"]["app_server_restart"] = res
    print("app-server restart", res)
    assert res["same_membership"] and res["same_thread"] and res["leave_lines"] == 0, res
    assert res["reconnect_notices"] == 1, res
    assert any(e[0] == "rebound" for e in res["events"]), res
    assert b["path"] == "turn_start" and b["evidence"] == "rpc:turn/start", res


def test_7_tui_quit_sends_no_rpc(live: Live) -> None:
    from switchboard.adapters.codex_rpc import CodexRpc

    live.wait_settled("codex-1", live.sock_a, T1, 150)
    tid = live.tids["codex-1"]
    turns_before = len(live.read_thread(live.sock_a, tid, True)["turns"])
    notes: list[tuple[float, str, Any]] = []

    async def observe(secs: float) -> None:
        rpc = CodexRpc(live.sock_a, on_notification=lambda m, p, t: notes.append((t, m, p.get("status"))))
        await rpc.connect()
        await asyncio.sleep(secs)
        await rpc.close()

    import threading

    obs = threading.Thread(target=lambda: asyncio.run(observe(80.0)), daemon=True)
    obs.start()
    time.sleep(1.0)
    t_quit = time.time()
    live.tmux.type(T1, "/quit")
    live.wait(lambda: live.tmux.pane_dead(T1), 30, "the TUI to exit")
    live.wait(lambda: live.part("codex-1")["tier_note"] is not None, 20, "codex-1 to show as detached")
    mid = live.say("@codex-1 after-quit: reply with the switchboard say tool: here")
    time.sleep(3.0)
    turns_mid = len(live.read_thread(live.sock_a, tid, True)["turns"])
    live.wait(lambda: live.part("codex-1")["status"] == "offline", 80, "the thread to unload")
    obs.join(90)
    sent = [dict(b) for b in live.batches_since("codex-1", t_quit)]
    res = {
        "turns_before": turns_before,
        "turns_after_post": turns_mid,
        "push_batches_after_quit": [(b["path"], b["state"], b["expire_reason"]) for b in sent],
        "delivery": live.q(
            "SELECT state FROM deliveries WHERE message_id=? AND membership_id=?", mid, live.mids["codex-1"]
        )[0][0],
        "member_after_quit": {k: live.member("codex-1").get(k) for k in ("status", "tier", "tier_note")},
        "notifications_after_quit": [(round(t - t_quit, 1), m, s) for t, m, s in notes if t >= t_quit],
        "session_end_hook_s": next(
            (
                round(r["ts"] - t_quit, 1)
                for r in live.q(
                    "SELECT ts, data FROM events WHERE kind='status' AND participant_id=?"
                    " AND ts>=? ORDER BY id",
                    live.part("codex-1")["id"],
                    t_quit,
                )
                if "hook:SessionEnd" in r["data"]
            ),
            None,
        ),  # this quit's
    }
    live.results["scenarios"]["tui_quit"] = res
    print("tui quit", res)
    assert turns_mid == turns_before, res  # no turn/start reached the app-server
    assert all(
        b["state"] != "confirmed"
        and b["path"] in ("turn_start", "steer", "queue")
        and b["expire_reason"] in ("reroute", "offline")
        for b in sent
    ), res
    assert res["delivery"] == "pending", res
