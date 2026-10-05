"""Live Devin CLI (DESIGN.md §0, §12.4, §13 M5). Opt-in:
``SWITCHBOARD_LIVE=devin SWITCHBOARD_LIVE_DIR=<tmp> uv run pytest -m live tests/live/test_live_devin.py -s``.

One real ``devin --model swe-1-6-slow --respect-workspace-trust false`` in a
private tmux server (clean env), in a scratch git workspace whose project-local
``.devin/config.json`` and ``.devin/mcp_config.json`` come from ``switchboard
install devin --print-args`` plus the test's isolation (``read_config_from``
all false, so nothing is imported from Claude/Cursor/... configs). Nothing
user-level is written. The broker runs in test mode in a temp ``SWITCHBOARD_HOME``;
the driver plays the human through the web API.

Devin usage is quota-limited, so the run is short:
1. join, then the agent enters the wait loop (tier ``devin:wait-loop``);
2. wait-loop wakes, n=5: first action (the next PreToolUse after the wait's
   PostToolUse) p50 < 2 s;
3. one PostToolUse context check (a human post while the agent reads files);
4. one Stop re-arm (the agent ends its turn; the Stop hook asks it to wait()
   again, and it does).

Never: Esc Esc then Enter on an idle Devin REPL (the revert picker), any
answer to a permission prompt. Devin must run outside the Claude Code Bash
sandbox (found in the M0 experiments). Results go to
``<run home>/results.json`` and stdout.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from harness import drift, preflight, profiles
from harness.tmuxdrv import Tmux, clean_env

pytestmark = pytest.mark.live

LIVE = {x.strip() for x in os.environ.get("SWITCHBOARD_LIVE", "").split(",") if x.strip()}
REAL_PATH = os.environ.get("PATH", "/usr/bin:/bin")
SESSION = "dv1"
READY = "Ask Devin to"
WAIT = "mcp__switchboard__wait"
N_WAKES = int(os.environ.get("SWITCHBOARD_LIVE_DEVIN_N", "5"))


def _skip_reason() -> str | None:
    if "devin" not in LIVE:
        return "set SWITCHBOARD_LIVE=devin to run the live Devin test"
    if not shutil.which("devin", path=REAL_PATH):
        return "devin not on PATH"
    if not shutil.which("tmux", path=REAL_PATH):
        return "tmux not on PATH"
    return None


if _skip_reason():
    pytest.skip(_skip_reason() or "", allow_module_level=True)


def pctl(xs: list[float], q: float) -> float:
    if len(xs) == 1:
        return xs[0]
    return statistics.quantiles(xs, n=100, method="inclusive")[int(q) - 1]


class Live:
    def __init__(self) -> None:
        base = os.environ.get("SWITCHBOARD_LIVE_DIR") or tempfile.gettempdir()
        self.home = Path(tempfile.mkdtemp(prefix="yk-home-", dir=base))
        (self.home / ".switchboard-test").touch()
        self.ws = Path(tempfile.mkdtemp(prefix="yk-ws-", dir=base))
        self.results: dict[str, Any] = {"scenarios": {}}
        self.devin_bin = shutil.which("devin", path=REAL_PATH) or "devin"
        self.tmux = Tmux("devin", REAL_PATH)
        self.broker: subprocess.Popen[bytes] | None = None
        self.web: httpx.Client | None = None
        self.port = 0
        self.drift_before = drift.snapshot()
        self.acp_pid: int | None = None
        self.mid: int | None = None
        self.params = self.home / "params"

    # ------------------------------------------------------------- setup
    def start(self) -> None:
        env = clean_env(REAL_PATH, TMPDIR=tempfile.gettempdir())
        subprocess.run(["git", "init", "-q"], cwd=self.ws, env=env, check=True)
        (self.ws / "README.md").write_text("scratch workspace for a switchboard live test\n")
        for n in ("one", "two", "three"):
            (self.ws / f"{n}.txt").write_text(f"this is file {n}\n")
        ver = subprocess.run(
            [self.devin_bin, "--version"], env=env, capture_output=True, text=True, timeout=30
        ).stdout.strip()
        self.results["devin_version"] = ver
        (self.home / "config.toml").write_text('human_name = "alice"\n')  # a fixed name, not the login
        benv = {**env, "SWITCHBOARD_TEST": "1", "SWITCHBOARD_RECORD_PAYLOADS": str(self.params)}
        out = open(self.home / "broker.stdout", "ab")
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
        self.command("/budget 30")
        pa = subprocess.run(
            [
                sys.executable,
                "-m",
                "switchboard",
                "install",
                "devin",
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
        print_args = json.loads(pa.stdout)
        d = self.ws / ".devin"
        d.mkdir()
        (d / "config.json").write_text(json.dumps(profiles.devin_config(print_args), indent=2))
        (d / "mcp_config.json").write_text(print_args["files"][".devin/mcp_config.json"])

    def launch(self) -> None:
        self.tmux.new_session(
            SESSION, str(self.ws), clean_env(REAL_PATH), profiles.devin_argv(self.devin_bin)
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            s = self.tmux.capture(SESSION)
            if re.search(r"connection failed|GetCliModelConfigs", s, re.I):
                pytest.skip("not run: sandbox (devin can't reach its service from here)")
            if READY in s:
                break
            time.sleep(0.5)
        else:
            raise AssertionError("devin never became ready:\n" + self.tmux.screen(SESSION))
        self.results["quota_remaining_at_start"] = self.quota()

    def quota(self) -> str | None:
        """The "NN% remaining" shown in the banner, if it is on screen."""
        for _ in range(10):
            m = re.search(r"(\d+)% remaining", self.tmux.capture(SESSION))
            if m:
                return f"{m.group(1)}%"
            time.sleep(0.3)
        return None

    def find_acp(self) -> int | None:
        root = self.tmux.pane_pid(SESSION)
        if root is None:
            return None
        for pid, _pp, args in preflight.descendants(root):
            if re.search(r"(^|/)devin(\s.*)?\sacp(\s|$)", args):
                return pid
        return None

    def stop(self) -> None:
        try:
            if self.web is not None:
                self.command("/pause")  # an open wait() returns "paused"; no re-arm while paused
                time.sleep(3)
        except Exception:
            pass
        self.results["screen_at_end"] = self.tmux.screen(SESSION, 6)
        self.tmux.kill_server()
        if self.broker is not None and self.broker.poll() is None:
            self.broker.send_signal(signal.SIGTERM)
            try:
                self.broker.wait(15)
            except subprocess.TimeoutExpired:
                self.broker.kill()
        if self.web is not None:
            self.web.close()
        time.sleep(1)
        left = subprocess.run(
            ["/usr/bin/pgrep", "-f", str(self.home)], capture_output=True, text=True
        ).stdout.split()
        left += [
            str(p) for p, _pp, a in preflight.descendants(1) if str(self.ws) in a and re.search(r"devin", a)
        ]
        for pid in left:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (ProcessLookupError, ValueError, PermissionError):
                pass
        self.results["leftover_processes"] = len(left)
        fail, info = drift.compare(self.drift_before, drift.snapshot())
        self.results["drift_fail"] = fail
        self.results["drift_info"] = info
        (self.home / "results.json").write_text(json.dumps(self.results, indent=1))
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

    # ------------------------------------------------------------ observing
    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        con = sqlite3.connect(f"file:{self.paths.db}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def part(self) -> sqlite3.Row | None:
        rows = self.q(
            "SELECT * FROM participants WHERE harness='devin' AND ended_at IS NULL ORDER BY id DESC"
        )
        return rows[0] if rows else None

    def events(self) -> list[dict[str, Any]]:
        """The hook params the broker received (allowlisted fields), in arrival order."""
        try:
            return [
                json.loads(x) for x in (self.params / "devin.jsonl").read_text().splitlines() if x.strip()
            ]
        except OSError:
            return []

    def listening(self) -> bool:
        """The last hook is the PreToolUse of a wait() call: the agent is blocked in it."""
        ev = self.events()
        return bool(ev) and ev[-1]["event"] == "PreToolUse" and ev[-1]["params"].get("tool") == WAIT

    def wait(self, fn: Any, timeout: float, what: str, step: float = 0.1) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            v = fn()
            if v:
                return v
            time.sleep(step)
        raise AssertionError(f"timed out waiting for {what}\n--- screen ---\n{self.tmux.screen(SESSION)}")

    def settled(self) -> bool:
        if not self.listening():
            return False
        open_ = self.q(
            "SELECT COUNT(*) FROM deliveries WHERE membership_id=? AND state IN ('pending','offered')",
            self.mid,
        )[0][0]
        return open_ == 0

    def wait_settled(self, timeout: float = 120) -> None:
        self.wait(
            lambda: self.settled() and (time.sleep(0.8) or self.settled()),
            timeout,
            "the agent to sit in wait()",
            step=0.3,
        )

    def batch_for(self, msg_id: int, state: str | None = "confirmed") -> sqlite3.Row | None:
        sql = (
            "SELECT b.* FROM batches b JOIN deliveries d ON d.batch_id=b.id WHERE d.message_id=?"
            " AND d.membership_id=?"
        )
        if state:
            sql += f" AND b.state='{state}'"
        rows = self.q(sql, msg_id, self.mid)
        return rows[0] if rows else None

    def msg_ts(self, msg_id: int) -> float:
        return self.q("SELECT ts FROM messages WHERE id=?", msg_id)[0][0]

    def agent_said(self, pattern: str, after: int) -> bool:
        rows = self.q("SELECT text FROM messages WHERE id>? AND sender_kind='agent' AND kind='chat'", after)
        return any(re.search(pattern, r[0]) for r in rows)


@pytest.fixture(scope="module")
def live():
    w = Live()
    try:
        w.start()
        w.launch()
        yield w
    finally:
        w.stop()
    assert w.results["drift_fail"] == [], w.results["drift_fail"]


# ------------------------------------------------------------------ scenarios
def test_1_join_and_enter_the_wait_loop(live: Live) -> None:
    live.tmux.type(
        SESSION,
        (
            'Use the switchboard MCP tools. Call join with room "#build" and screen_name'
            ' "devin-1". Then call wait with room "#build" and timeout_s 600. Each time wait'
            " returns messages, do exactly what they ask (reply with the switchboard say tool),"
            " then call wait again with the same arguments. If wait returns timeout, call it"
            " again. If it returns paused, end your turn. Keep replies short."
        ),
    )
    live.wait(
        lambda: (
            live.part() is not None
            and live.q("SELECT id FROM memberships WHERE screen_name='devin-1' AND left_at IS NULL")
        ),
        180,
        "the join",
    )
    live.mid = live.q("SELECT id FROM memberships WHERE screen_name='devin-1' AND left_at IS NULL")[0][0]
    p = live.part()
    live.acp_pid = live.find_acp()
    assert p["agent_pid"] == live.acp_pid, (p["agent_pid"], live.acp_pid)
    assert p["tier"] == "devin:wait-loop"
    bad = preflight.problems(live.tmux.pane_pid(SESSION) or -1)
    assert bad == [], f"preflight: unexpected MCP servers/helpers under devin: {bad}"
    live.wait_settled(180)
    p = live.part()
    assert p["hooks_seen_at"] is not None and p["session_id"]
    live.results["scenarios"]["join"] = {
        "tier": p["tier"],
        "approval_mode": p["approval_mode"],
        "session_id_seen": bool(p["session_id"]),
    }


def test_2_wait_loop_wake_first_action_p50_under_2s(live: Live) -> None:
    first, in_ctx, returned = [], [], []
    for i in range(1, N_WAKES + 1):
        live.wait_settled(120)
        mid = live.say(f"ping {i}: reply in #build with the switchboard say tool, text exactly: pong {i}")
        b = live.wait(
            lambda mid=mid: (x := live.batch_for(mid)) is not None and x["first_action_at"] is not None and x,
            60,
            f"wake {i}: confirmed and acted on",
        )
        ts = live.msg_ts(mid)
        assert b["path"] == "wait" and b["wake_kind"] == "wait_return"
        first.append(b["first_action_at"] - ts)
        in_ctx.append(b["turn_start_at"] - ts)
        returned.append(b["created_at"] - ts)
    live.wait_settled(120)
    replies = sum(1 for i in range(1, N_WAKES + 1) if live.agent_said(rf"\bpong {i}\b", 0))
    res = {
        "n": len(first),
        "first_action_ms": [round(x * 1000, 1) for x in first],
        "first_action_p50_ms": round(pctl(first, 50) * 1000, 1),
        "first_action_p95_ms": round(pctl(first, 95) * 1000, 1),
        "in_context_p50_ms": round(pctl(in_ctx, 50) * 1000, 1),
        "wait_returned_p50_ms": round(pctl(returned, 50) * 1000, 1),
        "replied_with_say": replies,
        "redeliveries": live.q("SELECT COUNT(*) FROM events WHERE kind='requeue'")[0][0],
    }
    live.results["scenarios"]["wait_loop_wake"] = res
    print("wait-loop wake", res)
    assert pctl(first, 50) < 2.0, res


def test_3_posttooluse_context_mid_task(live: Live) -> None:
    live.wait_settled(120)
    tag = secrets.token_hex(3)
    a = live.say(
        f"CTX-{tag}: use your read tool to read one.txt, then two.txt, then three.txt in this folder,"
        f" one call at a time. Then reply with the switchboard say tool: read done {tag}. Then call wait"
        " again."
    )
    live.wait(lambda: live.batch_for(a), 60, "the task message to reach context")
    b_mid = live.say(f"MID-{tag}: when you see this, also reply with the switchboard say tool: ack MID-{tag}")
    b = live.wait(lambda: live.batch_for(b_mid), 90, "the mid-task message to reach context")
    res = {
        "path": b["path"],
        "evidence": b["evidence"],
        "in_context_ms": round((b["confirmed_at"] - live.msg_ts(b_mid)) * 1000, 1),
    }
    live.wait_settled(150)
    res["model_acked"] = live.agent_said(rf"ack MID-{tag}", b_mid)
    live.results["scenarios"]["posttooluse_context"] = res
    print("PostToolUse context", res)
    assert b["path"] == "hook_ctx" and b["evidence"] == "hook_ack", res


def test_4_stop_rearms_the_wait_loop(live: Live) -> None:
    live.wait_settled(150)
    tag = secrets.token_hex(3)
    rearms0 = live.q("SELECT COUNT(*) FROM events WHERE kind='rearm'")[0][0]
    n0 = len(live.events())
    live.say(
        f"STOP-{tag}: this tests the room's re-arm. First reply with the switchboard say"
        f" tool: bye {tag}. Then"
        " end your turn now, without calling wait. switchboard will then ask you to call wait again: do that."
    )
    live.wait(
        lambda: live.q("SELECT COUNT(*) FROM events WHERE kind='rearm'")[0][0] > rearms0,
        120,
        "the Stop hook's re-arm",
    )
    stops = [e for e in live.events()[n0:] if e["event"] == "Stop"]
    live.wait(lambda: live.listening(), 90, "the agent to call wait() again")
    p = live.part()
    res = {
        "rearm_events": live.q("SELECT COUNT(*) FROM events WHERE kind='rearm'")[0][0] - rearms0,
        "stops_seen": len(stops),
        "rearms_in_gen": p["rearms_in_gen"],
        "listening_again": True,
        "said_bye": live.agent_said(rf"bye {tag}", 0),
    }
    live.results["scenarios"]["stop_rearm"] = res
    print("Stop re-arm", res)
    assert res["rearm_events"] >= 1
