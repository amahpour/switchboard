"""Live Claude Code (DESIGN.md §12.4, §13 M3). Opt-in: ``SWITCHBOARD_LIVE=claude uv run pytest -m live``.

One real ``claude --model haiku`` session in a private tmux server (clean
env, per-launch flags only, nothing user-level written), a test-mode broker
in a temp ``SWITCHBOARD_HOME``; the driver plays the human through the web API
(and, for what a human types into their own terminal, through tmux).

Scenarios, in order:
1. join (the smoke test): the session binds, gets ``claude:inbox``;
2. idle wake, n=5: turn start (the confirming UserPromptSubmit) p50 < 2 s;
3. mid-task priority in default mode: PostToolUse context at the next tool boundary;
4. approval hold: a human post is held while a permission prompt is open;
   the driver declines with Esc (never approves) and the post arrives after;
5. ``/clear`` keeps the binding (and the inbox);
6. ``/exit``, then (optional) ``--resume`` plus an MCP timeout, to record the
   SessionStart(resume) and PostToolUseFailure(mcp timeout) payloads.

Latencies and outcomes go to ``<run>/results.json`` and stdout. Raw hook
payloads (test-only recorder) go to ``<run>/raw.jsonl``; the broker's
allowlisted params (``SWITCHBOARD_RECORD_PAYLOADS``) to ``<run>/params/``.
``harness/fixtures.py <run>`` turns them into sanitized fixtures.
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
from harness.tmuxdrv import REAL_HOME, Tmux, clean_env

pytestmark = pytest.mark.live

LIVE = {x.strip() for x in os.environ.get("SWITCHBOARD_LIVE", "").split(",") if x.strip()}
REAL_PATH = os.environ.get("PATH", "/usr/bin:/bin")
SESSION = "c1"


def _skip_reason() -> str | None:
    if "claude" not in LIVE:
        return "set SWITCHBOARD_LIVE=claude to run the live Claude test"
    if not shutil.which("claude", path=REAL_PATH):
        return "claude not on PATH"
    if not shutil.which("tmux", path=REAL_PATH):
        return "tmux not on PATH"
    return None


if _skip_reason():
    pytest.skip(_skip_reason() or "", allow_module_level=True)


# ---------------------------------------------------------------- the world
class Live:
    def __init__(self) -> None:
        base = os.environ.get("SWITCHBOARD_LIVE_DIR") or tempfile.gettempdir()
        self.home = Path(tempfile.mkdtemp(prefix="yk-home-", dir=base))
        (self.home / ".switchboard-test").touch()
        self.ws = Path(tempfile.mkdtemp(prefix="yk-ws-", dir=base))
        self.run = self.home  # raw recordings and results live next to the broker's files
        self.raw = self.run / "raw.jsonl"
        self.results: dict[str, Any] = {"scenarios": {}}
        self.claude_bin = shutil.which("claude", path=REAL_PATH) or "claude"
        self.tmux = Tmux("claude", REAL_PATH)
        self.broker: subprocess.Popen[bytes] | None = None
        self.web: httpx.Client | None = None
        self.port = 0
        self.drift_before = drift.snapshot()
        self.claude_pid: int | None = None
        self.mid: int | None = None  # membership id
        self.pid_row: int | None = None  # participant id
        self.sessions_dir = Path(REAL_HOME) / ".claude" / "sessions"

    # ------------------------------------------------------------- setup
    def start(self) -> None:
        env = clean_env(REAL_PATH, TMPDIR=tempfile.gettempdir())
        subprocess.run(["git", "init", "-q"], cwd=self.ws, env=env, check=True)
        (self.ws / "README.md").write_text("scratch workspace for a switchboard live test\n")
        ver = subprocess.run(
            [self.claude_bin, "--version"], env=env, capture_output=True, text=True, timeout=30
        ).stdout.strip()
        self.results["claude_version"] = ver
        self.results["ws"] = str(self.ws)  # scratch only; fixtures.py rewrites it to /ws
        (self.home / "config.toml").write_text(  # human_name: the name the prompts use
            f'human_name = "alice"\n[claude]\nsessions_dir = "{self.sessions_dir}"\n'
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
        # a few more wakes than the default test cap: 5 idle wakes plus the scenarios
        self.command("/budget 40")
        pa = subprocess.run(
            [
                sys.executable,
                "-m",
                "switchboard",
                "install",
                "claude",
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
        (self.run / "mcp.json").write_text(json.dumps(profiles.claude_mcp_config(print_args)))
        (self.run / "settings.json").write_text(json.dumps(profiles.claude_settings(print_args, self.raw)))

    def launch(self, *extra: str, extra_env: dict[str, str] | None = None) -> None:
        env = clean_env(REAL_PATH, DISABLE_AUTOUPDATER="1", **(extra_env or {}))
        argv = profiles.claude_argv(
            self.claude_bin, self.run / "mcp.json", self.run / "settings.json", *extra
        )
        self.tmux.new_session(SESSION, str(self.ws), env, argv)
        self.wait_ready()
        self.claude_pid = self.find_claude_pid()
        bad = preflight.problems(self.claude_pid or -1)
        assert bad == [], f"preflight: unexpected MCP servers/helpers under claude: {bad}"

    def find_claude_pid(self) -> int | None:
        """The pane's process if it is claude (the shell exec'd it), else its claude descendant."""
        root = self.tmux.pane_pid(SESSION)
        if root is None:
            return None
        cands = [
            (
                root,
                0,
                subprocess.run(
                    ["/bin/ps", "-o", "args=", "-p", str(root)], capture_output=True, text=True
                ).stdout.strip(),
            )
        ]
        cands += preflight.descendants(root)
        for pid, _pp, args in cands:
            if re.search(r"(^|/)claude(\s|$)", args):
                return pid
        return root

    def wait_ready(self, timeout: float = 90) -> None:
        """Answer only the workspace-trust dialog for our own scratch dir; Esc the
        Chrome offer; never touch a permission prompt."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s = self.tmux.capture(SESSION)
            if "Yes, I trust this folder" in s:
                assert "yk-ws-" in s, "trust dialog for an unexpected folder:\n" + s
                if re.search(r"❯\s*(\d\.\s*)?Yes, I trust this folder", s):
                    self.tmux.key(SESSION, "Enter")  # the cursor is on "Yes, I trust this folder"
                    time.sleep(1.5)
                else:
                    self.tmux.key(SESSION, "Down")  # from "No, exit" to "Yes, ..."; checked before Enter
                    time.sleep(0.4)
                continue
            if "Claude in Chrome" in s:
                self.tmux.key(SESSION, "Escape")
                time.sleep(1)
                continue
            if "Yes, I accept" in s:
                raise AssertionError("a bypass-mode warning dialog appeared; never answered:\n" + s)
            if re.search(r"(shortcuts|manual mode|accept edits|bypass permissions)", s):
                return
            time.sleep(0.5)
        raise AssertionError("claude never became ready:\n" + self.tmux.screen(SESSION))

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
        # sweep: nothing we started may survive (our home is on every command line)
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
        self.results["drift_fail"] = fail
        self.results["drift_info"] = info
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

    def type(self, text: str) -> None:
        self.tmux.type(SESSION, text)

    # ----------------------------------------------------------- observing
    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        con = sqlite3.connect(f"file:{self.paths.db}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def part(self) -> sqlite3.Row | None:
        rows = self.q(
            "SELECT * FROM participants WHERE harness='claude' AND ended_at IS NULL ORDER BY id DESC"
        )
        return rows[0] if rows else None

    def registry(self) -> dict[str, Any]:
        try:
            return json.loads((self.sessions_dir / f"{self.claude_pid}.json").read_text())
        except (OSError, ValueError):
            return {}

    def raw_events(self) -> list[dict[str, Any]]:
        try:
            return [json.loads(x) for x in self.raw.read_text().splitlines() if x.strip()]
        except OSError:
            return []

    def wait(self, fn: Any, timeout: float, what: str, step: float = 0.1) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            v = fn()
            if v:
                return v
            time.sleep(step)
        raise AssertionError(f"timed out waiting for {what}\n--- screen ---\n{self.tmux.screen(SESSION)}")

    def settled(self) -> bool:
        """Idle by hooks and registry, nothing offered or pending for the member."""
        p = self.part()
        if p is None or p["status"] != "idle" or self.registry().get("status") != "idle":
            return False
        busy = self.q(
            "SELECT COUNT(*) FROM deliveries WHERE membership_id=? AND state IN ('pending','offered')",
            self.mid,
        )[0][0]
        return busy == 0

    def decline_stray_prompt(self) -> None:
        """A permission prompt no scenario asked for (the model wandered off, e.g.
        after /clear wiped its instructions): decline it with Esc, never approve."""
        if "Do you want to proceed" in self.tmux.capture(SESSION):
            self.tmux.key(SESSION, "Escape")
            self.results["stray_prompts_declined"] = self.results.get("stray_prompts_declined", 0) + 1
            time.sleep(1.0)

    def wait_settled(self, timeout: float = 90) -> None:
        def ok() -> bool:
            self.decline_stray_prompt()
            return bool(self.settled() and (time.sleep(1.5) or self.settled()))

        self.wait(ok, timeout, "the session to settle", step=0.3)

    def confirmed_batch_for(self, msg_id: int) -> sqlite3.Row | None:
        rows = self.q(
            "SELECT b.* FROM batches b JOIN deliveries d ON d.batch_id=b.id"
            " WHERE d.message_id=? AND d.membership_id=? AND b.state='confirmed'",
            msg_id,
            self.mid,
        )
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


def pctl(xs: list[float], q: float) -> float:
    if len(xs) == 1:
        return xs[0]
    return statistics.quantiles(xs, n=100, method="inclusive")[int(q) - 1]


# ------------------------------------------------------------------ scenarios
def test_1_join_binds_the_session_with_the_inbox_tier(live: Live) -> None:
    live.type(
        'Use the switchboard tools: call join with room "#build" and screen_name "claude-1". Then run the Bash'
        " command `false` (it fails on purpose; that is fine), then the Bash command `echo ready`, then reply"
        " with one word: joined. Later, switchboard will relay messages that I (alice) post in #build; answer"
        " each one with the switchboard say tool, or pass, exactly as the message asks. Keep it short."
    )
    live.wait(
        lambda: (
            live.part() is not None
            and live.q("SELECT id FROM memberships WHERE screen_name='claude-1' AND left_at IS NULL")
        ),
        120,
        "the join",
    )
    p = live.part()
    live.mid = live.q("SELECT id FROM memberships WHERE screen_name='claude-1' AND left_at IS NULL")[0][0]
    live.pid_row = p["id"]
    assert p["agent_pid"] == live.claude_pid, (p["agent_pid"], live.claude_pid)
    assert p["tier"] == "claude:inbox", p["tier"]
    live.wait_settled(120)
    p = live.part()
    assert p["approval_mode"] == "prompting" and p["hooks_seen_at"] is not None
    live.results["scenarios"]["join"] = {"tier": p["tier"], "approval_mode": p["approval_mode"]}


def test_2_idle_wake_turn_start_p50_under_2s(live: Live) -> None:
    lat, posted, confirm = [], [], []
    last = 0
    for i in range(1, 6):
        live.wait_settled(120)
        mid = live.say(f"ping {i}: reply in #build with the switchboard say tool, text exactly: pong {i}")
        b = live.wait(lambda: live.confirmed_batch_for(mid), 30, f"idle wake {i} to be confirmed")
        ts = live.msg_ts(mid)
        assert b["path"] == "inbox" and b["wake_kind"] == "idle_wake" and b["turn_start_at"] is not None
        lat.append(b["turn_start_at"] - ts)
        posted.append(b["posted_at"] - ts)
        confirm.append(b["confirmed_at"] - ts)
        last = mid
    live.wait_settled(120)
    replies = sum(1 for i in range(1, 6) if live.agent_said(rf"\bpong {i}\b", 0))
    res = {
        "n": len(lat),
        "turn_start_ms": [round(x * 1000, 1) for x in lat],
        "turn_start_p50_ms": round(pctl(lat, 50) * 1000, 1),
        "turn_start_p95_ms": round(pctl(lat, 95) * 1000, 1),
        "posted_p50_ms": round(pctl(posted, 50) * 1000, 1),
        "confirmed_p50_ms": round(pctl(confirm, 50) * 1000, 1),
        "replied_with_say": replies,
        "redeliveries": live.q("SELECT COUNT(*) FROM events WHERE kind='requeue'")[0][0],
    }
    live.results["scenarios"]["idle_wake"] = res
    print("idle wake", res)
    assert pctl(lat, 50) < 2.0, res
    assert last


def test_3_mid_task_priority_arrives_at_the_next_tool_boundary(live: Live) -> None:
    live.wait_settled(120)
    tag = secrets.token_hex(3)
    n0 = len(live.raw_events())
    live.type(
        f"Run the Bash command `sleep 10`, then run the Bash command `echo mid-done-{tag}`, then reply"
        " with one word: finished."
    )
    live.wait(
        lambda: any(
            e["event"] == "PreToolUse" and "sleep" in json.dumps(e["payload"].get("tool_input"))
            for e in live.raw_events()[n0:]
        ),
        60,
        "the sleep to start",
    )
    time.sleep(2.0)
    assert live.part()["status"] == "busy"
    mid = live.say(
        f"MID-{tag}: when you read this, call the switchboard say tool with the text: ack MID-{tag}"
    )
    time.sleep(1.0)
    assert (
        live.q("SELECT COUNT(*) FROM batches WHERE path='inbox' AND created_at>?", live.msg_ts(mid))[0][0]
        == 0
    )
    b = live.wait(lambda: live.confirmed_batch_for(mid), 30, "the mid-task message to reach context")
    ts = live.msg_ts(mid)
    sleep_end = next(
        (
            e["t"]
            for e in live.raw_events()[n0:]
            if e["event"] == "PostToolUse" and "sleep" in json.dumps(e["payload"].get("tool_input"))
        ),
        None,
    )
    res = {
        "path": b["path"],
        "evidence": b["evidence"],
        "in_context_ms": round((b["confirmed_at"] - ts) * 1000, 1),
        "after_tool_end_ms": round((b["confirmed_at"] - sleep_end) * 1000, 1) if sleep_end else None,
        "no_inbox_frame_mid_task": True,
    }
    assert b["path"] == "hook_ctx" and b["evidence"] == "hook_ack", res
    # "at the next tool boundary": claimed by the sleep's own PostToolUse, before
    # the model's next tool call (the echo) even started
    echo_pre = next(
        (
            e["t"]
            for e in live.raw_events()[n0:]
            if e["event"] == "PreToolUse" and f"mid-done-{tag}" in json.dumps(e["payload"].get("tool_input"))
        ),
        None,
    )
    assert res["after_tool_end_ms"] is not None and 0 <= res["after_tool_end_ms"] < 1000, res
    assert echo_pre is None or b["confirmed_at"] < echo_pre, (res, echo_pre)
    live.wait_settled(150)
    res["model_acked"] = live.agent_said(rf"ack MID-{tag}", mid)
    res["redelivered"] = bool(
        live.q("SELECT redelivered FROM deliveries WHERE message_id=? AND membership_id=?", mid, live.mid)[0][
            0
        ]
    )
    live.results["scenarios"]["mid_task"] = res
    print("mid task", res)


def test_4_approval_hold_then_decline_with_esc(live: Live) -> None:
    live.wait_settled(150)
    tag = secrets.token_hex(3)
    fname = f"yk-perm-{tag}.txt"
    live.type(f"Run the Bash command `touch {fname}` and then reply done.")
    live.wait(lambda: "Do you want to proceed" in live.tmux.capture(SESSION), 60, "the permission prompt")
    live.wait(lambda: live.part()["status"] == "waiting-approval", 5, "the waiting-approval hold")
    reg_status = live.registry().get("status")
    mid = live.say(f"HOLD-{tag}: call the switchboard say tool with the text: got HOLD-{tag}")
    time.sleep(7.5)  # long enough for the Notification hook (about 6 s) to be recorded too
    held = {
        "status": live.part()["status"],
        "delivery": live.q(
            "SELECT state FROM deliveries WHERE message_id=? AND membership_id=?", mid, live.mid
        )[0][0],
        "batches_since": live.q("SELECT COUNT(*) FROM batches WHERE created_at>?", live.msg_ts(mid))[0][0],
        "prompt_still_open": "Do you want to proceed" in live.tmux.capture(SESSION),
    }
    assert held == {
        "status": "waiting-approval",
        "delivery": "pending",
        "batches_since": 0,
        "prompt_still_open": True,
    }, held
    t_esc = time.time()
    live.tmux.key(SESSION, "Escape")  # decline; the driver never approves
    b = live.wait(lambda: live.confirmed_batch_for(mid), 30, "the held message after the Esc")
    res = {
        "registry_status_at_prompt": reg_status,
        "held_while_prompt_open": held,
        "path": b["path"],
        "esc_to_turn_start_ms": round((b["turn_start_at"] - t_esc) * 1000, 1) if b["turn_start_at"] else None,
        "file_created": (live.ws / fname).exists(),
    }
    assert b["path"] == "inbox" and not res["file_created"], res
    live.wait_settled(150)
    res["model_acked"] = live.agent_said(rf"got HOLD-{tag}", mid)
    live.results["scenarios"]["approval_hold"] = res
    print("approval hold", res)


def test_5_clear_keeps_the_binding(live: Live) -> None:
    live.wait_settled(150)
    before = live.part()
    n0 = len(live.raw_events())
    live.type("/clear")
    live.wait(
        lambda: any(
            e["event"] == "SessionStart" and e["payload"].get("source") == "clear"
            for e in live.raw_events()[n0:]
        ),
        30,
        "SessionStart(clear)",
    )
    live.wait(lambda: live.part()["session_id"] != before["session_id"], 10, "the new session id")
    after = live.part()
    assert after["id"] == before["id"] and after["ended_at"] is None and after["tier"] == "claude:inbox"
    assert live.q("SELECT left_at FROM memberships WHERE id=?", live.mid)[0][0] is None
    live.wait_settled(60)
    tag = secrets.token_hex(3)
    mid = live.say(
        f"CLEAR-{tag}: call the switchboard say tool (room #build) with the text: ack CLEAR-{tag}."
        " Use only that tool; no shell commands."
    )
    b = live.wait(lambda: live.confirmed_batch_for(mid), 30, "a wake after /clear")
    res = {
        "same_participant": True,
        "session_id_changed": True,
        "path": b["path"],
        "turn_start_ms": round((b["turn_start_at"] - live.msg_ts(mid)) * 1000, 1),
    }
    live.wait_settled(150)
    res["model_acked"] = live.agent_said(rf"ack CLEAR-{tag}", mid)
    live.results["scenarios"]["clear"] = res
    print("clear", res)


def test_6_exit_and_resume_for_the_fixtures(live: Live) -> None:
    """/exit (SessionEnd), then --resume with a short MCP timeout (SessionStart
    resume, PostToolUseFailure for an MCP call that times out)."""
    live.wait_settled(150)
    sid = live.part()["session_id"]
    live.command("/pause")
    live.type("/exit")
    live.wait(lambda: live.tmux.pane_dead(SESSION), 30, "claude to exit")
    live.tmux.run("kill-session", "-t", SESSION)
    live.wait(
        lambda: live.part() is None or live.part()["status"] == "offline", 15, "the session to go offline"
    )
    if os.environ.get("SWITCHBOARD_LIVE_RESUME", "1") != "1":
        return
    live.command("/resume")
    live.launch("--resume", sid, extra_env={"MCP_TOOL_TIMEOUT": "5000"})
    n0 = len(live.raw_events())
    live.type(
        'Call the switchboard join tool with room "#build" and screen_name "claude-2", then call the switchboard'
        ' wait tool with room "#build" and timeout_s 30, then reply with one word: waited.'
    )
    live.wait(
        lambda: any(
            e["event"] == "PostToolUseFailure" and "wait" in str(e["payload"].get("tool_name"))
            for e in live.raw_events()[n0:]
        ),
        90,
        "the MCP timeout",
    )
    res = {
        "resumed": any(
            e["event"] == "SessionStart" and e["payload"].get("source") == "resume" for e in live.raw_events()
        )
    }
    time.sleep(3)
    live.command("/pause")
    live.type("/exit")
    live.wait(lambda: live.tmux.pane_dead(SESSION), 30, "claude to exit")
    live.results["scenarios"]["resume"] = res
