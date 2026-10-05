"""Live Cursor Agent CLI (DESIGN.md §12.4, §13 M5). Not yet tested live: the body
is written and ready.
Opt-in: ``SWITCHBOARD_LIVE=cursor SWITCHBOARD_LIVE_DIR=<tmp> uv run pytest"
``-m live tests/live/test_live_cursor.py -s``.

One real ``agent --model auto`` in a private tmux server (clean env), in a
scratch git workspace whose project-local ``.cursor/mcp.json`` and
``.cursor/hooks.json`` come from ``switchboard install cursor --print-args``
(plus a test-only ``.cursor/cli.json`` allowing switchboard's tools). Workspace
trust and the project's MCP server are accepted per launch, as in M0; the
product never passes those flags. Nothing user-level is written by switchboard.

Scenarios:
1. join, and the ``postToolUse`` for ``MCP:join`` binds the conversation
   (``cursor:<conversation_id>``); the contract that the MCP server's agent
   ancestor equals the hooks' agent ancestor (the binding needs both);
2. the stop park: the agent ends its turn, the stop hook parks, a human post
   comes back as ``followup_message``; first hook after the post (n=3), and the
   follow-up is confirmed (the tier stays provisional until parks longer than
   ~40 s are proven: scenario 4);
3. postToolUse ``additional_context`` during ``sleep 8``;
4. a long park (90 s, then a post): records whether Cursor still runs a
   follow-up after a park longer than 60 s (FINDINGS §5 4.3, the open question).
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
SESSION = "cu1"
READY = re.compile(r"Add a follow-up|Plan, search, build anything|→")


def _skip_reason() -> str | None:
    if "cursor" not in LIVE:
        return "set SWITCHBOARD_LIVE=cursor to run the live Cursor test"
    if not shutil.which("agent", path=REAL_PATH):
        return "agent (Cursor Agent CLI) not on PATH"
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
        self.agent_bin = shutil.which("agent", path=REAL_PATH) or "agent"
        self.tmux = Tmux("cursor", REAL_PATH)
        self.broker: subprocess.Popen[bytes] | None = None
        self.web: httpx.Client | None = None
        self.drift_before = drift.snapshot()
        self.mid: int | None = None
        self.params = self.home / "params"

    def start(self) -> None:
        env = clean_env(REAL_PATH, TMPDIR=tempfile.gettempdir())
        subprocess.run(["git", "init", "-q"], cwd=self.ws, env=env, check=True)
        (self.ws / "README.md").write_text("scratch workspace for a switchboard live test\n")
        (self.home / "config.toml").write_text('human_name = "alice"\n')  # the name the prompts use
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
        port = 0
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not port:
            info = ping(self.paths.sock, timeout=1.0)
            port = info["port"] if info else 0
            time.sleep(0.1)
        assert port, "broker did not start"
        base = f"http://switchboard.localhost:{port}"
        self.web = httpx.Client(base_url=base, timeout=10.0, follow_redirects=False)
        tok = (self.home / "run" / "test-login-token").read_text().strip()
        assert self.web.get(f"/login?t={tok}").status_code == 303
        self.hdr = {"Origin": base, "X-Switchboard": "1", "Content-Type": "application/json"}
        assert self.web.post("/api/rooms", json={"name": "#build"}, headers=self.hdr).status_code == 200
        self.command("/budget 20")
        pa = subprocess.run(
            [
                sys.executable,
                "-m",
                "switchboard",
                "install",
                "cursor",
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
        files = json.loads(pa.stdout)["files"]
        (self.ws / ".cursor").mkdir()
        for rel, text in files.items():
            (self.ws / rel).write_text(text)
        (self.ws / ".cursor" / "cli.json").write_text(json.dumps(profiles.cursor_cli_json(), indent=1))

    def launch(self) -> None:
        self.tmux.new_session(
            SESSION, str(self.ws), clean_env(REAL_PATH), profiles.cursor_argv(self.agent_bin)
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            s = self.tmux.capture(SESSION)
            if re.search(r"free requests limit|Upgrade", s):
                pytest.skip("not run: Cursor quota")
            if READY.search(s):
                return
            time.sleep(0.5)
        raise AssertionError("agent never became ready:\n" + self.tmux.screen(SESSION))

    def stop(self) -> None:
        try:
            if self.web is not None:
                self.command("/pause")
        except Exception:
            pass
        self.tmux.kill_server()
        if self.broker is not None and self.broker.poll() is None:
            self.broker.send_signal(signal.SIGTERM)
            try:
                self.broker.wait(15)
            except subprocess.TimeoutExpired:
                self.broker.kill()
        if self.web is not None:
            self.web.close()
        left = subprocess.run(
            ["/usr/bin/pgrep", "-f", str(self.home)], capture_output=True, text=True
        ).stdout.split()
        for pid in left:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (ProcessLookupError, ValueError, PermissionError):
                pass
        self.results["leftover_processes"] = len(left)
        fail, info = drift.compare(self.drift_before, drift.snapshot())
        self.results["drift_fail"], self.results["drift_info"] = fail, info
        (self.home / "results.json").write_text(json.dumps(self.results, indent=1))
        print("\nLIVE RESULTS", json.dumps(self.results, indent=1))

    def say(self, text: str) -> int:
        r = self.web.post("/api/rooms/build/say", json={"text": text}, headers=self.hdr)
        assert r.status_code == 200, r.text
        return r.json()["id"]

    def command(self, text: str) -> dict[str, Any]:
        r = self.web.post("/api/rooms/build/command", json={"text": text}, headers=self.hdr)
        assert r.status_code == 200, r.text
        return r.json()

    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        con = sqlite3.connect(f"file:{self.paths.db}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def part(self) -> sqlite3.Row | None:
        rows = self.q(
            "SELECT * FROM participants WHERE harness='cursor' AND ended_at IS NULL ORDER BY id DESC"
        )
        return rows[0] if rows else None

    def events(self) -> list[dict[str, Any]]:
        try:
            return [
                json.loads(x) for x in (self.params / "cursor.jsonl").read_text().splitlines() if x.strip()
            ]
        except OSError:
            return []

    def parked(self) -> bool:
        """The last hook is a completed stop: the stop hook is parked on the broker."""
        ev = self.events()
        return bool(ev) and ev[-1]["event"] == "Stop" and ev[-1]["params"].get("status") == "completed"

    def wait(self, fn: Any, timeout: float, what: str, step: float = 0.1) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            v = fn()
            if v:
                return v
            time.sleep(step)
        raise AssertionError(f"timed out waiting for {what}\n--- screen ---\n{self.tmux.screen(SESSION)}")

    def batch_for(self, msg_id: int) -> sqlite3.Row | None:
        rows = self.q(
            "SELECT b.* FROM batches b JOIN deliveries d ON d.batch_id=b.id WHERE d.message_id=?"
            " AND d.membership_id=? AND b.state='confirmed'",
            msg_id,
            self.mid,
        )
        return rows[0] if rows else None

    def msg_ts(self, msg_id: int) -> float:
        return self.q("SELECT ts FROM messages WHERE id=?", msg_id)[0][0]

    def agent_ancestor(self, pid: int) -> int | None:
        """The nearest cursor-agent process above ``pid``."""
        cur = pid
        for _ in range(8):
            ppid = int(
                subprocess.run(
                    ["/bin/ps", "-o", "ppid=", "-p", str(cur)], capture_output=True, text=True
                ).stdout.strip()
                or 0
            )
            if ppid <= 1:
                return None
            args = subprocess.run(
                ["/bin/ps", "-ww", "-o", "args=", "-p", str(ppid)], capture_output=True, text=True
            ).stdout
            if "cursor-agent" in args:
                return ppid
            cur = ppid
        return None


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


def test_1_join_binds_the_conversation(live: Live) -> None:
    live.tmux.type(
        SESSION,
        'Use the switchboard MCP tools: call join with room "#build" and screen_name "cursor-1".'
        " Then reply with one word: joined. Later, switchboard will relay messages I (alice) post in"
        " #build; answer each with the switchboard say tool, exactly as it asks.",
    )
    live.wait(lambda: (p := live.part()) is not None and p["bind_state"] == "bound", 120, "the bind")
    p = live.part()
    live.mid = live.q("SELECT id FROM memberships WHERE screen_name='cursor-1' AND left_at IS NULL")[0][0]
    assert p["session_key"].startswith("cursor:") and not p["session_key"].startswith("cursor:agent:")
    assert (p["tier"], p["tier_note"]) == ("cursor:stop-park", "provisional")
    # the contract: the MCP server's agent ancestor is the hooks' agent ancestor
    mcp_pid = p["mcp_pid"]
    assert live.agent_ancestor(mcp_pid) == p["agent_pid"]
    bad = preflight.problems(live.tmux.pane_pid(SESSION) or -1)
    assert bad == [], bad
    live.results["scenarios"]["join"] = {"tier": p["tier"], "note": p["tier_note"]}


def test_2_stop_park_followups(live: Live) -> None:
    lat = []
    for i in range(1, 4):
        live.wait(live.parked, 120, f"the stop hook to park ({i})")
        time.sleep(1.0)
        mid = live.say(f"ping {i}: reply in #build with the switchboard say tool, text exactly: pong {i}")
        b = live.wait(lambda mid=mid: live.batch_for(mid), 60, f"follow-up {i} confirmed")
        assert b["path"] == "stop_followup" and b["wake_kind"] == "stop_cont"
        lat.append(b["turn_start_at"] - live.msg_ts(mid))
    res = {
        "n": len(lat),
        "first_hook_ms": [round(x * 1000, 1) for x in lat],
        "first_hook_p50_ms": round(pctl(lat, 50) * 1000, 1),
    }
    live.results["scenarios"]["stop_park"] = res
    print("stop park", res)


def test_3_posttooluse_context(live: Live) -> None:
    live.wait(live.parked, 120, "the stop hook to park")
    tag = secrets.token_hex(3)
    live.tmux.type(SESSION, f"Run the shell command `sleep 8`, then `echo done-{tag}`, then reply finished.")
    live.wait(lambda: any(e["event"] == "UserPromptSubmit" for e in live.events()[-3:]), 30, "the prompt")
    time.sleep(3)
    mid = live.say(f"MID-{tag}: reply with the switchboard say tool: ack MID-{tag}")
    b = live.wait(lambda: live.batch_for(mid), 60, "the mid-task context")
    res = {"path": b["path"], "evidence": b["evidence"]}
    live.results["scenarios"]["posttooluse_context"] = res
    assert b["path"] == "hook_ctx" and b["evidence"] == "hook_ack", res


def test_4_long_park(live: Live) -> None:
    """The open question behind "provisional": is a follow-up still run after a park > 60 s?"""
    live.wait(live.parked, 120, "the stop hook to park")
    time.sleep(90)
    mid = live.say("late ping: reply with the switchboard say tool, text exactly: late pong")
    try:
        b = live.wait(lambda: live.batch_for(mid), 60, "the follow-up after a 90 s park")
        res = {"delivered_after_90s_park": True, "path": b["path"]}
    except AssertionError:
        res = {"delivered_after_90s_park": False}
    live.results["scenarios"]["long_park"] = res
    print("long park", res)
