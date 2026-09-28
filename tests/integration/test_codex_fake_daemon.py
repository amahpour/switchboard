"""Codex against a fake app-server (DESIGN.md §9.3, §12.2).

A stand-in ``codex`` process (``fakes/fake_harness.py`` copied to a path
ending in ``/codex``) runs ``switchboard mcp`` and the hook commands as its
children, and can hold a "TUI" connection to the fake daemon, so the broker's
real checks (ancestry, the lsof client check) pass without a live CLI.

Covered: idle ``turn/start``; busy ``turn/steer`` with ``expectedTurnId``;
``-32600`` and a finished turn re-route to ``turn/start``; the
``waitingOnApproval`` hold; a steer lost at a declined approval comes back;
an unsolicited approval request is never answered; the thread proof gates
the push tier; the liveness guard (SessionEnd, the TUI detaching); the
loaded list picks the queue tier; the queue argv; the queue refused when
auto-start is on and the socket is down; no override field, no method
outside the allowlist; a canary from ``thread/read`` reaches no log, the DB
or events. Review fixes: a steer that keeps being refused can't spin; the
thread proof is retried at the end of a long join turn; another TUI on the
same daemon doesn't keep a thread whose TUI quit "attached"; the fresh lsof
check before a send; the fresh status read before ``turn/start``; no queue
tier without a daemon while the proof is required. Daemon restarts
(2026-09-25): a restart with a new app-server pid and the same thread keeps
the member (one reconnect notice, the tier back to ``codex:daemon``, queued
messages delivered); a daemon that doesn't come back ends it after the grace
window; a join after SessionEnd isn't stuck on "session ended"; the same MCP
process reconnecting keeps its credentials; a vanished codex binary is found
again. Review fixes: the new app-server named by the lsof listener; a re-join
from a new MCP server inside the grace window is visible and only counts as a
reconnect once its thread is proven.
Read before pass (§24): a
peer's @mention arrives in ``turn/start`` as a stub, pass() is refused until
read() has shown it, then passes.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from fakes.fake_claude import FakeClaude
from fakes.fake_codex_daemon import CANARY, FakeCodexDaemon
from switchboard.adapters import codex as codex_mod
from switchboard.adapters import codex_rpc
from switchboard.broker import agents as agents_mod
from switchboard.broker import proc
from switchboard.config import Config
from switchboard.install.common import hook_command
from switchboard.paths import hook_sha12

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
TID = "019a0000-0000-7000-8000-00000000c0de"
FAKE_BIN = """#!{py}
import json, os, sys
d = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps({{"argv": sys.argv, "env": dict(os.environ)}}) + "\\n")
"""


def wait_for(fn: Any, timeout: float = 8.0, step: float = 0.05) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


class W:
    def __init__(self, home: Path, *, daemon: bool = True, cfg: Config = FAST):
        self.home = home
        self.b = InProcBroker(home, cfg)
        bindir = home / "fakebin"
        bindir.mkdir()
        self.fake_bin = bindir / "codex"
        self.fake_bin.write_text(FAKE_BIN.format(py=sys.executable))
        self.fake_bin.chmod(0o755)
        self.b.cfg = self.b.cfg.replace(codex=dataclasses.replace(self.b.cfg.codex, bin=str(self.fake_bin)))
        self.sock = self.b.cfg.codex.control_socket
        self.d = FakeCodexDaemon(self.sock).start() if daemon else None
        self.b.start()
        self.web = self.b.web_client()
        assert self.web.post("/api/rooms", json={"name": "#build"}, headers=self.b.write_headers()).status_code == 200
        self.cx: FakeClaude | None = None

    @property
    def adapter(self) -> codex_mod.CodexAdapter:
        return self.b.state.engine.adapters["codex"]

    def close(self) -> None:
        if self.cx is not None:
            self.cx.close()
        self.web.close()
        self.b.stop()
        if self.d is not None:
            self.d.stop()

    # ---------------------------------------------------------------- parts
    def say(self, text: str) -> int:
        r = self.web.post("/api/rooms/build/say", json={"text": text}, headers=self.b.write_headers())
        assert r.status_code == 200, r.text
        return r.json()["id"]

    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        con = sqlite3.connect(f"file:{self.b.paths.db}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def part(self) -> sqlite3.Row:
        [row] = self.q("SELECT * FROM participants WHERE harness='codex'")
        return row

    def member(self) -> dict[str, Any]:
        ms = self.web.get("/api/rooms/build/members").json()["members"]
        return next(m for m in ms if m["name"] == "codex-1")

    def batches(self, path: str | None = None) -> list[sqlite3.Row]:
        if path:
            return self.q("SELECT * FROM batches WHERE path=? ORDER BY id", path)
        return self.q("SELECT * FROM batches ORDER BY id")

    def delivery(self, mid: int) -> str:
        return self.q("SELECT state FROM deliveries WHERE message_id=?", mid)[0][0]

    def join(self, *, attach: bool = True, prove: bool = True, loaded: bool = True) -> dict[str, Any]:
        if self.d is not None:
            self.d.add_thread(TID, loaded=loaded)
        self.cx = FakeClaude(self.b, as_harness="codex")
        if attach and self.d is not None:
            self.cx.p.stdin.write(json.dumps({"op": "attach", "path": self.sock}) + "\n")
            self.cx.p.stdin.flush()
            assert self.cx.recv()["attached"]
        r = self.cx.tool("join", meta={"threadId": TID}, room="#build", screen_name="codex-1")
        assert r["ok"], r
        assert "yk:j" in r["text"]
        if prove and self.d is not None:
            self.d.prove(TID, r["text"])
        return r

    def detach(self) -> None:
        assert self.cx is not None
        self.cx.p.stdin.write(json.dumps({"op": "detach"}) + "\n")
        self.cx.p.stdin.flush()
        assert self.cx.recv()["detached"]

    def hook(self, event: str, **extra: Any) -> str:
        assert self.cx is not None
        payload = {"hook_event_name": event, "session_id": TID, "cwd": "/ws", "permission_mode": "default",
                   **extra}
        cmd = hook_command(sys.executable, str(self.b.paths.home), hook_sha12(), "codex", event)
        self.cx.p.stdin.write(json.dumps({"op": "hook", "command": cmd, "payload": payload}) + "\n")
        self.cx.p.stdin.flush()
        r = self.cx.recv()
        assert r["rc"] == 0, r
        return r["stdout"]

    def tier(self) -> tuple[str, str | None]:
        p = self.part()
        return p["tier"], p["tier_note"]

    def daemon_tier(self) -> None:
        assert wait_for(lambda: self.tier() == ("codex:daemon", None)), self.tier()
        assert wait_for(lambda: self.part()["status"] == "idle"), self.part()["status"]

    def queue_calls(self) -> list[dict[str, Any]]:
        f = self.fake_bin.parent / "calls.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_mod, "PROOF_AT_S", (0.2, 0.5, 1.0))
    monkeypatch.setattr(codex_mod, "LOADED_POLL_S", 0.3)
    monkeypatch.setattr(codex_mod, "CLIENTS_POLL_S", 0.3)
    monkeypatch.setattr(codex_mod, "LINK_BACKOFF_S", (0.1, 0.3))
    monkeypatch.setattr(agents_mod, "LIVENESS_S", 0.3)


@pytest.fixture
def w(tmp_home: Path):
    world = W(tmp_home)
    try:
        yield world
    finally:
        world.close()


# ------------------------------------------------------------------ tests
def test_link_is_up_and_unsubscribed(w: W) -> None:
    assert wait_for(lambda: w.adapter.link_state == "up")
    st = w.b.call("sys.status")
    assert st["codex_link"].startswith("up since")
    assert w.d.methods() <= codex_rpc.ALLOWED_METHODS
    first = w.d.received[0][1]
    assert first["method"] == "initialize" and first["params"]["capabilities"] == {"experimentalApi": False}


def test_idle_wake_is_a_turn_start_with_exactly_three_keys(w: W) -> None:
    w.join()
    w.daemon_tier()
    assert w.part()["thread_proof"] == 1
    mid = w.say("please add validation to parse_port")
    b = wait_for(lambda: [x for x in w.batches("turn_start") if x["state"] == "confirmed"])
    assert b, w.batches()
    b = b[0]
    [call] = w.d.calls("turn/start")
    assert set(call) == {"threadId", "input", "clientUserMessageId"}
    assert call["threadId"] == TID and call["clientUserMessageId"] == f"yk-b{b['id']}"
    [item] = call["input"]
    assert item["type"] == "text" and item["text_elements"] == [] and item["text"].startswith("[switchboard]")
    assert "parse_port" in item["text"] and f"id={mid}" in item["text"]
    assert b["evidence"] == "rpc:turn/start" and b["turn_start_at"] is not None and b["budget_counted"] == 1
    assert w.delivery(mid) == "in_context"
    # the status connection saw the turn start: busy
    assert wait_for(lambda: w.part()["status"] == "busy")


def test_mid_task_priority_is_a_steer_confirmed_by_history(w: W) -> None:
    w.join()
    w.daemon_tier()
    turn = w.d.begin_turn(TID)
    assert wait_for(lambda: w.part()["status"] == "busy")
    mid = w.say("stop: use the other parser")
    assert wait_for(lambda: w.d.calls("turn/steer"))
    [call] = w.d.calls("turn/steer")
    assert set(call) == {"threadId", "expectedTurnId", "input", "clientUserMessageId"}
    assert call["expectedTurnId"] == turn and f"id={mid}" in call["input"][0]["text"]
    [b] = w.batches("steer")
    assert b["state"] == "offered" and b["kind"] == "priority" and b["budget_counted"] == 0
    assert w.d.calls("turn/start") == []
    w.d.end_turn(TID)
    assert wait_for(lambda: w.batches("steer")[0]["state"] == "confirmed")
    assert w.batches("steer")[0]["evidence"] == "rpc:steer+history"
    assert w.delivery(mid) == "in_context"


def test_a_steer_lost_at_a_declined_approval_comes_back(w: W) -> None:
    w.join()
    w.daemon_tier()
    w.d.steers_land = False  # accepted, then dropped when the human declines (FINDINGS §4a 3.4)
    w.d.begin_turn(TID)
    assert wait_for(lambda: w.part()["status"] == "busy")
    mid = w.say("priority while busy")
    assert wait_for(lambda: w.d.calls("turn/steer"))
    w.d.set_status(TID, "active", ["waitingOnApproval"])
    assert wait_for(lambda: w.part()["status"] == "waiting-approval")
    w.d.end_turn(TID, "interrupted")
    assert wait_for(lambda: w.batches("steer") and w.batches("steer")[0]["state"] == "expired")
    assert w.batches("steer")[0]["expire_reason"] == "steer_lost"
    # re-delivered as an idle wake, the same message id
    assert wait_for(lambda: w.d.calls("turn/start"))
    assert f"id={mid}" in w.d.calls("turn/start")[0]["input"][0]["text"]


def test_minus_32600_and_a_finished_turn_reroute_to_turn_start(w: W) -> None:
    w.join()
    w.daemon_tier()
    w.d.begin_turn(TID)
    assert wait_for(lambda: w.part()["status"] == "busy")
    # the turn ended but no idle notification arrived yet: the steer is refused
    with w.d.lock:
        w.d.threads[TID]["status"] = {"type": "idle"}
    w.d.fail_next["turn/steer"] = (-32600, "no active turn to steer")
    mid = w.say("racing the end of the turn")
    assert wait_for(lambda: w.d.calls("turn/start"), 10), w.batches()
    assert f"id={mid}" in w.d.calls("turn/start")[0]["input"][0]["text"]
    steer = w.batches("steer")
    assert steer and steer[0]["state"] == "expired" and steer[0]["expire_reason"] == "reroute"
    assert w.part()["push_expiries"] == 0  # a re-route is not a failure
    # and a turn that is simply over (no turn in progress): straight to turn/start
    w.d.end_turn(TID)
    assert wait_for(lambda: w.part()["status"] == "idle")
    w.d.begin_turn(TID)
    assert wait_for(lambda: w.part()["status"] == "busy")
    with w.d.lock:
        for t in w.d.threads[TID]["turns"]:
            t["status"] = "completed"
        w.d.threads[TID]["status"] = {"type": "idle"}
    n = len(w.d.calls("turn/start"))
    mid2 = w.say("second race")
    assert wait_for(lambda: len(w.d.calls("turn/start")) > n, 10)
    assert f"id={mid2}" in w.d.calls("turn/start")[-1]["input"][0]["text"]


def test_waiting_on_approval_holds_every_delivery(w: W) -> None:
    w.join()
    w.daemon_tier()
    w.d.begin_turn(TID)
    w.d.set_status(TID, "active", ["waitingOnApproval"])
    assert wait_for(lambda: w.part()["status"] == "waiting-approval")
    mid = w.say("held until the prompt closes")
    time.sleep(1.2)
    assert w.d.calls("turn/start") == [] and w.d.calls("turn/steer") == []
    assert w.delivery(mid) == "pending"
    assert all(b["path"] not in ("turn_start", "steer") for b in w.batches())
    assert w.hook("PostToolUse", tool_name="Bash", tool_response="ok") == ""  # no context either
    w.d.end_turn(TID, "interrupted")  # declined: the turn ends
    assert wait_for(lambda: w.d.calls("turn/start"))
    assert f"id={mid}" in w.d.calls("turn/start")[0]["input"][0]["text"]


def test_an_approval_request_is_never_answered(w: W) -> None:
    w.join()
    w.daemon_tier()
    rid = w.d.send_server_request()
    w.say("something to do meanwhile")
    assert wait_for(lambda: w.d.calls("turn/start"))
    time.sleep(0.5)
    assert w.d.answers == []
    assert not any(m.get("id") == rid for _c, m in w.d.received)
    # every message switchboard sent was an allowlisted request or notification
    assert all("method" in m for _c, m in w.d.received)
    assert w.d.methods() <= codex_rpc.ALLOWED_METHODS


def test_thread_proof_gates_the_push_tier(w: W) -> None:
    w.join(prove=False)
    assert lines(w, "join") == ["joined (codex, verifying...)"]  # pending: not "mcp-only"
    time.sleep(1.5)  # every proof attempt (0.2, 0.5, 1.0 s) failed
    assert w.tier() == ("mcp-only", "unverified thread") and w.part()["thread_proof"] == 0
    # failed: no "verified" notice; /who and the buddy list are back to today's wording
    assert verified(w) == []
    assert "  codex-1  codex  idle  mcp-only (unverified thread)" in who_text(w)
    assert w.member()["tier_note"] == "unverified thread"
    ev = w.q("SELECT data FROM events WHERE kind='bind' ORDER BY id DESC LIMIT 1")[0][0]
    assert json.loads(ev) == {"what": "thread_proof", "ok": False}
    mid = w.say("idle, but unverified")
    time.sleep(0.8)
    assert w.d.calls("turn/start") == [] and w.delivery(mid) == "pending"
    assert "unverified thread" in (w.member().get("parked_reason") or "")
    # mid-task it still gets hook context (best effort, developer role)
    w.hook("UserPromptSubmit", prompt="work on it")
    out = w.hook("PostToolUse", tool_name="Bash", tool_response="ok")
    ctx = json.loads(out)["hookSpecificOutput"]
    assert ctx["hookEventName"] == "PostToolUse" and f"id={mid}" in ctx["additionalContext"]
    assert wait_for(lambda: w.delivery(mid) == "in_context")


def test_session_end_stops_every_rpc(w: W) -> None:
    w.join()
    w.daemon_tier()
    w.hook("SessionEnd", reason="other")
    assert wait_for(lambda: w.part()["status"] == "offline")
    w.d.set_status(TID, "idle")  # the thread lingers ~60 s after its TUI quit
    mid = w.say("after the TUI quit")
    time.sleep(1.0)
    assert w.d.calls("turn/start") == [] and w.d.calls("turn/steer") == []
    assert w.delivery(mid) == "pending" and w.part()["status"] == "offline"


def test_a_detached_tui_stops_every_rpc(w: W) -> None:
    w.join()
    w.daemon_tier()
    w.detach()  # the TUI quit without a SessionEnd hook
    assert wait_for(lambda: w.tier() == ("codex:daemon", "detached?"))
    mid = w.say("nobody is watching")
    time.sleep(1.0)
    assert w.d.calls("turn/start") == [] and w.delivery(mid) == "pending"
    assert (w.member().get("parked_reason") or "").startswith("detached?")
    # the thread closes about a minute later: offline
    w.d.close_thread(TID)
    assert wait_for(lambda: w.part()["status"] == "offline")


def test_the_loaded_list_picks_the_queue_tier_and_its_argv(w: W) -> None:
    w.join(loaded=False, attach=False)  # an embedded TUI: the daemon doesn't have the thread
    assert wait_for(lambda: w.tier() == ("codex:queue", None)), w.tier()
    mid = w.say("before any hook")
    time.sleep(0.5)
    assert w.queue_calls() == []  # nothing could confirm it: parked
    assert "switchboard install codex" in (w.member().get("parked_reason") or "")
    w.hook("UserPromptSubmit", prompt="hi")
    w.hook("Stop", stop_hook_active=False)
    assert wait_for(lambda: w.queue_calls())
    [call] = w.queue_calls()
    text = call["argv"][-1]
    assert call["argv"] == [os.path.realpath(w.fake_bin), "queue", "--remote", f"unix://{w.sock}", "--thread",
                            TID, "--message", text]
    assert text.startswith("[switchboard]") and f"id={mid}" in text
    # (macOS adds __CF_USER_TEXT_ENCODING; Python's C-locale coercion adds LC_CTYPE in the child)
    assert set(call["env"]) - {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"} == {"PATH", "HOME"}
    [b] = w.batches("queue")
    assert b["state"] == "offered"
    # the queued prompt starts a turn: its UserPromptSubmit carries the token
    w.hook("UserPromptSubmit", prompt=text)
    b = w.batches("queue")[0]
    assert b["state"] == "confirmed" and b["turn_start_at"] is not None
    assert w.d.calls("turn/start") == []


def test_queue_is_refused_while_auto_start_is_on_and_the_socket_is_down(tmp_home: Path) -> None:
    cfg = FAST.replace(codex=dataclasses.replace(FAST.codex, require_thread_proof=False))
    home = Path(os.environ["HOME"])
    (home / ".codex").mkdir(exist_ok=True)
    (home / ".codex" / "config.toml").write_text("[features]\ndaemon_auto_start = true\n")
    w = W(tmp_home, daemon=False, cfg=cfg)
    try:
        w.join()
        w.hook("UserPromptSubmit", prompt="hi")
        w.hook("Stop", stop_hook_active=False)
        assert wait_for(lambda: "daemon_auto_start" in (w.tier()[1] or "")), w.tier()
        assert w.tier()[0] == "mcp-only"
        mid = w.say("no daemon")
        time.sleep(0.6)
        assert w.queue_calls() == [] and w.delivery(mid) == "pending"
        # auto-start off: the bare queue form (no daemon to target) is allowed
        (home / ".codex" / "config.toml").write_text("[features]\ndaemon_auto_start = false\n")
        assert wait_for(lambda: w.queue_calls(), 10), w.tier()
        assert w.queue_calls()[0]["argv"][:4] == [os.path.realpath(w.fake_bin), "queue", "--thread", TID]
    finally:
        w.close()


def test_canary_from_thread_read_reaches_no_log_db_or_event(w: W, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    w.join()
    w.daemon_tier()
    w.say("one")
    assert wait_for(lambda: w.d.calls("turn/start"))
    w.d.end_turn(TID)
    w.d.begin_turn(TID)
    assert wait_for(lambda: w.part()["status"] == "busy")
    w.say("two")
    assert wait_for(lambda: w.d.calls("turn/steer"))
    w.d.end_turn(TID)
    assert wait_for(lambda: all(b["state"] != "offered" for b in w.batches()))
    assert CANARY not in caplog.text
    con = sqlite3.connect(f"file:{w.b.paths.db}?mode=ro", uri=True)
    try:
        for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for row in con.execute(f"SELECT * FROM {table}").fetchall():  # noqa: S608
                assert CANARY not in json.dumps(row, default=str), table
    finally:
        con.close()
    for p in w.home.rglob("*"):
        if p.is_file() and not p.name.endswith((".db", ".db-wal", ".db-shm", ".sock")) and \
                not stat.S_ISSOCK(p.stat().st_mode):
            assert CANARY not in p.read_text(errors="replace"), p


def test_no_override_field_ever_went_out(w: W) -> None:
    w.join()
    w.daemon_tier()
    w.say("a")
    assert wait_for(lambda: w.d.calls("turn/start"))
    w.d.end_turn(TID)
    w.d.begin_turn(TID)
    assert wait_for(lambda: w.part()["status"] == "busy")
    w.say("b")
    assert wait_for(lambda: w.d.calls("turn/steer"))
    from switchboard import guardrails

    for _c, m in w.d.received:
        params = m.get("params") or {}
        assert not set(params) & set(guardrails.CODEX_OVERRIDE_FIELDS), m["method"]
        if m.get("method") in ("turn/start", "turn/steer"):
            assert set(params) == codex_rpc.PARAM_KEYS[m["method"]]


# ------------------------------------------------------------ review fixes
def _other_tui(w: W) -> FakeClaude:
    """Another Codex TUI on the same daemon (not in any room)."""
    other = FakeClaude(w.b, as_harness="codex")
    other.p.stdin.write(json.dumps({"op": "attach", "path": w.sock}) + "\n")
    other.p.stdin.flush()
    assert other.recv()["attached"]
    return other


def test_a_steer_that_keeps_being_refused_cannot_spin(w: W) -> None:
    w.join()
    w.daemon_tier()
    orig = w.d._answer

    def answer(method: str, p: dict[str, Any]) -> dict[str, Any]:  # e.g. a turn Codex won't steer
        if method == "turn/steer":
            return w.d._err(-32600, "active turn is not steerable")
        return orig(method, p)

    w.d._answer = answer
    w.d.begin_turn(TID)
    assert wait_for(lambda: w.part()["status"] == "busy")
    w.hook("UserPromptSubmit", prompt="human prompt")
    mid = w.say("priority while an unsteerable turn runs")
    time.sleep(2.0)
    assert 1 <= len(w.batches("steer")) <= 2, len(w.batches("steer"))
    assert len(w.d.calls("turn/steer")) <= 2 and w.d.calls("turn/start") == []
    # the rest of that turn gets it as PostToolUse context instead
    out = w.hook("PostToolUse", tool_name="Bash", tool_response="ok")
    assert f"id={mid}" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert wait_for(lambda: w.delivery(mid) == "in_context")
    assert w.part()["push_expiries"] == 0


def test_active_without_a_turn_in_progress_cannot_spin(w: W) -> None:
    w.join()
    w.daemon_tier()
    w.d.set_status(TID, "active")  # active, but thread/read shows no inProgress turn
    assert wait_for(lambda: w.part()["status"] == "busy")
    n0 = len(w.d.calls("thread/read"))
    w.say("priority in an odd state")
    time.sleep(2.0)
    assert len(w.batches("steer")) <= 2 and len(w.d.calls("thread/read")) - n0 <= 4
    assert w.d.calls("turn/start") == []


def test_the_proof_is_retried_when_the_join_turn_outlasts_the_first_tries(w: W) -> None:
    """0.156.1 lists an in-progress turn without its items, so a join made
    early in a long turn is only readable after that turn ends."""
    r = w.join(prove=False)
    w.d.begin_turn(TID)
    w.d.prove(TID, r["text"])  # the join result lands in the running turn: hidden for now
    time.sleep(1.5)  # every first try (0.2, 0.5, 1.0 s) is done
    assert w.part()["thread_proof"] == 0 and w.tier() == ("mcp-only", "unverified thread")
    w.d.end_turn(TID)  # busy -> idle: tried again
    assert wait_for(lambda: w.part()["thread_proof"] == 1), "proof not retried at the turn end"
    assert wait_for(lambda: w.tier()[0] == "codex:daemon")
    # the first proof of the session, although not one of the first tries: announced once
    assert wait_for(lambda: verified(w)) and len(verified(w)) == 1
    assert verified(w)[0].startswith("codex-1 is verified: codex:")


def test_a_nonce_outside_the_join_result_proves_nothing(w: W) -> None:
    r = w.join(prove=False)
    nonce = r["text"].split("yk:j", 1)[1][:16]
    w.d.add_item(TID, {"type": "commandExecution", "id": "c1", "status": "completed",
                       "aggregatedOutput": f"cat notes.txt: yk:j{nonce}"})
    time.sleep(1.5)
    assert w.part()["thread_proof"] == 0


def test_another_tui_on_the_daemon_does_not_keep_a_quit_thread_attached(w: W) -> None:
    w.join()
    w.daemon_tier()
    other = _other_tui(w)
    try:
        time.sleep(0.8)  # the second TUI is seen
        w.detach()  # codex-1's own TUI quits; its thread lingers (60-65 s in real Codex)
        assert wait_for(lambda: w.tier() == ("codex:daemon", "detached?")), w.tier()
        mid = w.say("hello, anyone?")
        time.sleep(1.2)
        assert w.d.calls("turn/start") == [] and w.delivery(mid) == "pending"
        assert "TUI disconnected" in (w.member().get("parked_reason") or "")
        ev = w.q("SELECT data FROM events WHERE kind='codex_hold'")
        assert ev and json.loads(ev[-1][0])["why"] == "a TUI disconnected"
    finally:
        other.close()


def test_a_hold_is_released_when_the_orphaned_thread_unloads(w: W) -> None:
    orphan = "019a0000-0000-7000-8000-0000000000ff"
    w.d.add_thread(orphan)  # another TUI's thread (not in any room)
    other = _other_tui(w)
    w.join()  # first look: 2 TUIs, 2 loaded threads
    w.daemon_tier()
    other.p.stdin.write(json.dumps({"op": "detach"}) + "\n")  # the other TUI quits
    other.p.stdin.flush()
    assert other.recv()["detached"]
    try:
        assert wait_for(lambda: w.tier() == ("codex:daemon", "detached?")), w.tier()
        mid = w.say("held for now")
        time.sleep(0.8)
        assert w.d.calls("turn/start") == []
        w.d.close_thread(orphan)  # ~60 s later in real Codex: now 1 TUI covers 1 loaded thread
        assert wait_for(lambda: w.d.calls("turn/start")), w.tier()
        assert f"id={mid}" in w.d.calls("turn/start")[0]["input"][0]["text"]
    finally:
        other.close()


def test_the_threads_own_human_ends_the_hold(w: W) -> None:
    w.join()
    w.daemon_tier()
    other = _other_tui(w)
    time.sleep(0.8)
    other.p.stdin.write(json.dumps({"op": "detach"}) + "\n")
    other.p.stdin.flush()
    assert other.recv()["detached"]
    try:
        assert wait_for(lambda: w.tier() == ("codex:daemon", "detached?")), w.tier()
        w.hook("UserPromptSubmit", prompt="typed by the human")  # no switchboard token: a human is there
        w.hook("Stop", stop_hook_active=False)
        assert wait_for(lambda: w.tier() == ("codex:daemon", None)), w.tier()
        w.say("now it goes")
        assert wait_for(lambda: w.d.calls("turn/start"))
    finally:
        other.close()


def test_the_fresh_lsof_check_stops_a_turn_start_right_after_a_detach(
        w: W, monkeypatch: pytest.MonkeyPatch) -> None:
    w.join()
    w.daemon_tier()
    monkeypatch.setattr(codex_mod, "CLIENTS_POLL_S", 60.0)  # the cached check keeps saying "attached"
    time.sleep(0.6)  # the clients loop is now in its long sleep
    w.detach()
    assert w.tier() == ("codex:daemon", None)  # the cache still says attached
    mid = w.say("posted right after the TUI quit")
    time.sleep(1.2)
    assert w.d.calls("turn/start") == [] and w.delivery(mid) == "pending"
    assert [b["expire_reason"] for b in w.batches("turn_start")] == ["reroute"]


def test_turn_start_needs_a_fresh_idle_status(w: W) -> None:
    """The cached view says idle, but the thread is active (the notification
    hasn't arrived): no turn/start (it would act as a steer, or land in an
    approval wait)."""
    w.join()
    w.daemon_tier()
    with w.d.lock:  # a turn begins without a status notification reaching switchboard
        w.d.threads[TID]["turns"].append({"id": "turn-x", "status": "inProgress", "items": []})
        w.d.threads[TID]["status"] = {"type": "active", "activeFlags": ["waitingOnApproval"]}
    mid = w.say("idle? not really")
    time.sleep(1.0)
    assert w.d.calls("turn/start") == [] and w.d.calls("turn/steer") == []
    assert w.delivery(mid) == "pending"
    assert w.part()["status"] == "waiting-approval"  # learned from the read
    w.d.end_turn(TID, "interrupted")
    assert wait_for(lambda: w.d.calls("turn/start"))


def test_no_queue_tier_without_a_daemon_while_the_proof_is_required(tmp_home: Path) -> None:
    """With no socket there is nothing to read the proof through: the default
    (require_thread_proof) keeps such a thread mcp-only (DESIGN §9.3)."""
    home = Path(os.environ["HOME"])
    (home / ".codex").mkdir(exist_ok=True)
    (home / ".codex" / "config.toml").write_text("[features]\ndaemon_auto_start = false\n")
    w = W(tmp_home, daemon=False)
    try:
        w.join()
        w.hook("UserPromptSubmit", prompt="hi")
        w.hook("Stop", stop_hook_active=False)
        time.sleep(1.5)
        assert w.tier() == ("mcp-only", "unverified thread")
        w.say("no daemon")
        time.sleep(0.6)
        assert w.queue_calls() == []
    finally:
        w.close()


# ------------------------------------------------------------------- /pause (M6)
def cmd(w: W, text: str) -> None:
    r = w.web.post("/api/rooms/build/command", json={"text": text}, headers=w.b.write_headers())
    assert r.status_code == 200 and r.json()["ok"], r.text


def ticks(w: W, n: int = 2) -> None:
    """Wait until the runner's tick (1 s) has run ``n`` times from now. Each tick
    re-evaluates every pending delivery and sends what it may, so after two, anything
    the engine would send (the RPC of the first included) has gone out."""
    eng = w.b.state.engine
    seen: list[int] = []
    orig = eng.tick

    def counted(*a: Any, **kw: Any) -> Any:
        out = orig(*a, **kw)
        seen.append(1)
        return out

    eng.tick = counted  # type: ignore[method-assign]
    try:
        assert wait_for(lambda: len(seen) >= n, 5)
    finally:
        del eng.tick


def test_pause_stops_turn_start_and_steer_until_resume(w: W) -> None:
    w.join()
    w.daemon_tier()
    cmd(w, "/pause")
    mid = w.say("idle, but the room is paused")
    assert w.delivery(mid) == "pending"  # the engine has it for the member...
    ticks(w)
    assert w.d.calls("turn/start") == [] and w.batches("turn_start") == []  # ...and offers nothing
    assert w.delivery(mid) == "pending"
    w.d.begin_turn(TID)  # the thread's own human starts a turn
    assert wait_for(lambda: w.part()["status"] == "busy")
    mid2 = w.say("busy, and still paused")
    ticks(w)
    assert w.d.calls("turn/steer") == [] and w.batches("steer") == []
    assert w.delivery(mid2) == "pending"
    assert w.hook("PostToolUse", tool_name="Bash", tool_input={}, tool_response="ok") == ""  # no context
    cmd(w, "/resume")
    assert wait_for(lambda: w.d.calls("turn/steer"))
    text = w.d.calls("turn/steer")[0]["input"][0]["text"]
    assert f"id={mid}" in text and f"id={mid2}" in text


# ------------------------------------------------ daemon restarts (2026-09-25)
RESTARTING = ("mcp-only", "Codex daemon restarting")


def daemon_goes_away(w: W) -> None:
    """The managed daemon auto-updates: its hooks fire SessionEnd while it shuts
    down, then it exits and takes its MCP servers along (the link drops)."""
    w.hook("SessionEnd", reason="other")
    assert wait_for(lambda: w.part()["status"] == "offline")
    w.d.stop()
    w.cx.close()
    w.cx = None


def daemon_comes_back(w: W) -> FakeClaude:
    """A new app-server (a new pid) on the same control socket; the TUI reconnects
    and resumes the same thread, which starts that thread's MCP server there."""
    w.d = FakeCodexDaemon(w.sock).start()
    w.d.add_thread(TID, loaded=False)
    w.cx = FakeClaude(w.b, as_harness="codex")
    w.cx.p.stdin.write(json.dumps({"op": "attach", "path": w.sock}) + "\n")
    w.cx.p.stdin.flush()
    assert w.cx.recv()["attached"]
    with w.d.lock:
        w.d.loaded.add(TID)
    w.d.set_status(TID, "idle")
    return w.cx


def lines(w: W, kind: str) -> list[str]:
    return [r["text"] for r in w.q("SELECT text FROM messages WHERE kind=? ORDER BY id", kind)]


def verified(w: W) -> list[str]:
    """The "<name> is verified: <tier>" notices (a Codex session's first thread proof)."""
    return [x for x in lines(w, "notice") if " is verified: " in x]


def who_text(w: W) -> str:
    r = w.web.post("/api/rooms/build/command", json={"text": "/who"}, headers=w.b.write_headers())
    assert r.status_code == 200, r.text
    return r.json()["text"]


def new_mcp_server(w: W) -> None:
    """The session's MCP server restarts (a new process, attached to the daemon)."""
    assert w.cx is not None
    w.cx.close()
    w.cx = FakeClaude(w.b, as_harness="codex")
    w.cx.p.stdin.write(json.dumps({"op": "attach", "path": w.sock}) + "\n")
    w.cx.p.stdin.flush()
    assert w.cx.recv()["attached"]


def settled(w: W) -> None:
    """A round trip through the broker's loop: whatever the proof that just passed was going
    to post is posted by now (it posts in the same step that sets the tier)."""
    who_text(w)


def test_the_first_thread_proof_is_announced_once(w: W, monkeypatch: pytest.MonkeyPatch) -> None:
    """While the first proof tries run, the join line, /who and the buddy list say
    "verifying..." instead of "mcp-only"; the first proof posts one "is verified" notice
    with the tier it has then; the same membership proven again (a join from a new MCP
    server, which keeps it: "re-joined", no join line) posts none."""
    monkeypatch.setattr(codex_mod, "PROOF_AT_S", (0.2, 0.5, 4.0))  # time to look before the last try
    r = w.join(prove=False)
    assert lines(w, "join") == ["joined (codex, verifying...)"]
    assert w.tier() == ("mcp-only", "verifying...") and w.member()["tier_note"] == "verifying..."
    line = next(x for x in who_text(w).splitlines() if x.startswith("  codex-1 "))
    assert line.split("  ")[4] == "verifying..." and "mcp-only" not in line
    assert verified(w) == []
    w.d.prove(TID, r["text"])
    assert wait_for(lambda: verified(w)), lines(w, "notice")
    assert verified(w) == ["codex-1 is verified: codex:daemon"]
    w.daemon_tier()
    assert "verifying" not in who_text(w)
    # the MCP server restarts: a join from the new one proves the thread again
    new_mcp_server(w)
    r2 = w.cx.tool("join", meta={"threadId": TID}, room="#build", screen_name="codex-1")
    assert r2["ok"], r2
    assert w.part()["thread_proof"] == 0 and w.tier() == ("mcp-only", "verifying...")
    w.d.prove(TID, r2["text"])
    assert wait_for(lambda: w.part()["thread_proof"] == 1)
    assert wait_for(lambda: w.tier()[1] != "verifying..."), w.tier()
    settled(w)
    assert verified(w) == ["codex-1 is verified: codex:daemon"]  # not repeated
    assert len(lines(w, "join")) == 1 and [x for x in lines(w, "notice") if "re-joined" in x]


def test_a_rejoin_that_says_verifying_is_announced_again(w: W, monkeypatch: pytest.MonkeyPatch) -> None:
    """A verified session leaves, and joins again from a new MCP server (e.g. after `codex
    resume`): a new join line says "verifying...", so the proof that passes posts a new
    "is verified" notice, once."""
    monkeypatch.setattr(codex_mod, "PROOF_AT_S", (0.2, 0.5, 4.0))
    w.join()
    assert wait_for(lambda: verified(w) == ["codex-1 is verified: codex:daemon"]), lines(w, "notice")
    assert w.cx is not None and w.cx.tool("leave", meta={"threadId": TID}, room="#build")["ok"]
    new_mcp_server(w)
    r = w.cx.tool("join", meta={"threadId": TID}, room="#build", screen_name="codex-1")
    assert r["ok"], r
    assert lines(w, "join") == ["joined (codex, verifying...)"] * 2
    w.d.prove(TID, r["text"])
    assert wait_for(lambda: len(verified(w)) == 2), lines(w, "notice")
    assert wait_for(lambda: w.tier()[1] != "verifying..."), w.tier()
    settled(w)
    first, second = verified(w)  # the tier it has then (the new MCP server may not be seen attached yet)
    assert first == "codex-1 is verified: codex:daemon" and second.startswith("codex-1 is verified: codex:daemon")
    ev = [json.loads(r[0]) for r in w.q("SELECT data FROM events WHERE kind='join' ORDER BY id")]
    assert [e.get("verifying") for e in ev] == [True, True]


def restart_events(w: W) -> list[tuple[str, str | None]]:
    return [(e["what"], e.get("via")) for e in (json.loads(r[0]) for r in w.q(
        "SELECT data FROM events WHERE kind='codex_restart' ORDER BY id"))]


def as_app_server(monkeypatch: pytest.MonkeyPatch, pids: set[int]) -> None:
    """``pids`` look like the managed daemon (``codex app-server --listen unix://``) to
    the re-bind's app-server check. Here the stand-in ``codex`` is the TUI and the
    MCP servers' parent at once (its argv is a TUI's), and the fake app-server
    listens inside this test process."""
    real = proc.argv
    monkeypatch.setattr(proc, "argv", lambda pid, start: "/opt/homebrew/bin/codex app-server --listen unix://"
                        if pid in pids else real(pid, start))


def test_a_daemon_restart_keeps_the_member_and_delivers_after_it(w: W, monkeypatch: pytest.MonkeyPatch) -> None:
    servers: set[int] = set()
    as_app_server(monkeypatch, servers)
    w.join()
    w.daemon_tier()
    [m0] = w.q("SELECT id, cred_hash FROM memberships WHERE left_at IS NULL")
    mcp0 = (w.part()["mcp_pid"], w.part()["mcp_start"])
    daemon_goes_away(w)
    assert wait_for(lambda: w.tier() == RESTARTING), w.tier()
    mid = w.say("posted while the daemon restarts")
    time.sleep(0.6)
    assert w.delivery(mid) == "pending" and w.part()["ended_at"] is None
    assert w.member().get("tier_note") == "Codex daemon restarting"
    tui = daemon_comes_back(w)
    # no lsof listener here is a Codex app-server: the one whose MCP servers said hello
    # since the link dropped (and that is a Codex app-server) is the new daemon
    servers.add(tui.pid)
    assert wait_for(lambda: w.part()["agent_pid"] == tui.pid, 10), dict(w.part())
    assert wait_for(lambda: w.tier() == ("codex:daemon", None)), w.tier()
    # the queued message goes out as an idle wake on the new app-server
    assert wait_for(lambda: w.d.calls("turn/start")), w.batches()
    assert f"id={mid}" in w.d.calls("turn/start")[0]["input"][0]["text"]
    assert wait_for(lambda: w.delivery(mid) == "in_context")
    # the member never left: the same membership and credential, one notice, no leave line
    [m1] = w.q("SELECT id, cred_hash FROM memberships WHERE left_at IS NULL")
    assert (m1["id"], m1["cred_hash"]) == (m0["id"], m0["cred_hash"])
    assert (w.part()["mcp_pid"], w.part()["mcp_start"]) == mcp0
    assert lines(w, "leave") == []
    assert [x for x in lines(w, "notice") if "reconnected" in x] == [
        "codex-1 reconnected after a Codex daemon restart"]
    assert restart_events(w) == [("app_server_gone", None), ("rebound", "loaded")]
    # hooks of the thread run under the new app-server now, and resolve to the member
    w.hook("UserPromptSubmit", prompt="typed after the restart")
    assert wait_for(lambda: w.part()["status"] == "busy")
    # the new MCP server holds no credential yet: the agent joins again, same name, same member
    r = w.cx.tool("say", meta={"threadId": TID}, room="#build", text="back")
    assert r["ok"] is False and r["code"] == "not_member"
    r = w.cx.tool("join", meta={"threadId": TID}, room="#build", screen_name="codex-1")
    assert r["ok"], r
    assert len(w.q("SELECT id FROM memberships")) == 1 and len(lines(w, "join")) == 1  # a re-join, not a new one
    # ...but visible: another MCP process took the membership over, and its thread is proven again
    assert wait_for(lambda: [x for x in lines(w, "notice") if "re-joined" in x] == [
        "codex-1 re-joined from a new switchboard MCP server"])
    # "verifying..." while the first tries run (0.2, 0.5, 1.0 s here), then "unverified thread"
    assert w.part()["thread_proof"] == 0 and w.tier()[0] == "mcp-only"
    assert w.tier()[1] in ("verifying...", "unverified thread")
    assert w.cx.tool("say", meta={"threadId": TID}, room="#build", text="back")["ok"]
    w.d.prove(TID, r["text"])  # in the wake's turn: readable once it ends (the proof is retried then)
    w.d.end_turn(TID)
    w.hook("Stop", stop_hook_active=False)
    assert wait_for(lambda: w.tier() == ("codex:daemon", None)), w.tier()
    assert len([x for x in lines(w, "notice") if "reconnected" in x]) == 1
    # proven again, but announced only the first time (at the first join)
    assert verified(w) == ["codex-1 is verified: codex:daemon"]


def test_a_restart_rebinds_to_the_app_server_serving_the_control_socket(
        w: W, monkeypatch: pytest.MonkeyPatch) -> None:
    """The main way to name the new app-server: the one Codex app-server lsof shows
    listening on the control socket (here the fake app-server, in this process), not
    the MCP servers' hellos (the stand-in's argv is a TUI's)."""
    as_app_server(monkeypatch, {os.getpid()})
    w.join()
    w.daemon_tier()
    daemon_goes_away(w)
    assert wait_for(lambda: w.tier() == RESTARTING), w.tier()
    mid = w.say("posted while the daemon restarts")
    daemon_comes_back(w)
    assert wait_for(lambda: w.part()["agent_pid"] == os.getpid(), 10), dict(w.part())
    assert wait_for(lambda: w.tier() == ("codex:daemon", None)), w.tier()
    assert wait_for(lambda: w.d.calls("turn/start")), w.batches()
    assert f"id={mid}" in w.d.calls("turn/start")[0]["input"][0]["text"]
    assert restart_events(w) == [("app_server_gone", None), ("rebound", "loaded")]
    assert lines(w, "leave") == []
    assert [x for x in lines(w, "notice") if "reconnected" in x] == [
        "codex-1 reconnected after a Codex daemon restart"]


def test_a_rejoin_in_the_grace_window_is_a_reconnect_only_once_proven(w: W) -> None:
    """The review's case: while the member waits out a restart (old MCP and app-server
    gone), a join from a new MCP process claims its thread. It takes the membership
    over visibly, and "reconnected" is only said once its thread proof passes."""
    w.join()
    w.daemon_tier()
    daemon_goes_away(w)
    assert wait_for(lambda: w.tier() == RESTARTING), w.tier()
    w.d = FakeCodexDaemon(w.sock).start()
    w.d.add_thread(TID, loaded=False)  # the new daemon hasn't loaded the thread yet
    w.cx = FakeClaude(w.b, as_harness="codex")
    r = w.cx.tool("join", meta={"threadId": TID}, room="#build", screen_name="codex-1")
    assert r["ok"], r
    time.sleep(1.3)  # every first proof attempt (0.2, 0.5, 1.0 s) failed: nothing proven yet
    assert [x for x in lines(w, "notice") if "re-joined" in x] == ["codex-1 re-joined from a new switchboard MCP server"]
    assert not [x for x in lines(w, "notice") if "reconnected" in x]
    assert w.tier() == ("mcp-only", "unverified thread") and w.part()["thread_proof"] == 0
    assert len(w.q("SELECT id FROM memberships")) == 1 and lines(w, "leave") == []
    assert restart_events(w) == [("app_server_gone", None)]
    # the join really came from the thread: proven (retried at the join turn's end)
    w.d.prove(TID, r["text"])
    w.hook("Stop", stop_hook_active=False)
    assert wait_for(lambda: w.part()["thread_proof"] == 1)
    assert wait_for(lambda: [x for x in lines(w, "notice") if "reconnected" in x] == [
        "codex-1 reconnected after a Codex daemon restart"])
    assert restart_events(w) == [("app_server_gone", None), ("rebound", "join")]
    # its TUI attaches and the thread loads: the daemon tier again
    w.cx.p.stdin.write(json.dumps({"op": "attach", "path": w.sock}) + "\n")
    w.cx.p.stdin.flush()
    assert w.cx.recv()["attached"]
    with w.d.lock:
        w.d.loaded.add(TID)
    w.d.set_status(TID, "idle")
    assert wait_for(lambda: w.tier() == ("codex:daemon", None)), w.tier()


def test_a_daemon_that_does_not_come_back_ends_the_member(tmp_home: Path) -> None:
    cfg = FAST.replace(codex=dataclasses.replace(FAST.codex, restart_grace_s=1.0))
    w = W(tmp_home, cfg=cfg)
    try:
        w.join()
        w.daemon_tier()
        daemon_goes_away(w)
        assert wait_for(lambda: w.tier() == RESTARTING), w.tier()
        assert wait_for(lambda: w.part()["ended_at"] is not None, 10)
        assert lines(w, "leave") == ["left (session ended)"]
        assert not [x for x in lines(w, "notice") if "reconnected" in x]
        ev = [json.loads(r[0])["what"] for r in w.q("SELECT data FROM events WHERE kind='codex_restart'")]
        assert ev == ["app_server_gone", "gave_up"]
    finally:
        w.close()


def test_a_join_after_session_end_goes_back_to_the_daemon_tier(w: W) -> None:
    """Joining again (the user said "join #build as codex-1" once more) is evidence
    the thread runs: it must not stay "session ended" (its UserPromptSubmit came
    before the join, so it couldn't clear it)."""
    w.join()
    w.daemon_tier()
    w.hook("SessionEnd", reason="other")
    assert wait_for(lambda: w.tier() == ("mcp-only", "session ended")), w.tier()
    mid = w.say("parked until it is back")
    time.sleep(0.5)
    assert w.d.calls("turn/start") == [] and w.delivery(mid) == "pending"
    r = w.cx.tool("join", meta={"threadId": TID}, room="#build", screen_name="codex-1")
    assert r["ok"], r
    assert wait_for(lambda: w.tier() == ("codex:daemon", None)), w.tier()
    w.hook("Stop", stop_hook_active=False)  # the join's turn ends
    assert wait_for(lambda: w.d.calls("turn/start")), w.batches()
    assert f"id={mid}" in w.d.calls("turn/start")[0]["input"][0]["text"]


def test_a_rejoin_after_the_session_ended_is_not_stuck_on_session_ended(tmp_home: Path) -> None:
    """The reported sequence, without the grace window: the old daemon's SessionEnd,
    the session ends, the TUI reconnects with the same thread id and the user
    re-runs join: the thread proof passes and the tier is codex:daemon again."""
    cfg = FAST.replace(codex=dataclasses.replace(FAST.codex, restart_grace_s=0.0))
    w = W(tmp_home, cfg=cfg)
    try:
        w.join()
        w.daemon_tier()
        daemon_goes_away(w)
        assert wait_for(lambda: w.part()["ended_at"] is not None)
        assert lines(w, "leave") == ["left (session ended)"]
        tui = daemon_comes_back(w)
        r = tui.tool("join", meta={"threadId": TID}, room="#build", screen_name="codex-1")
        assert r["ok"], r
        w.d.prove(TID, r["text"])
        assert wait_for(lambda: w.part()["thread_proof"] == 1)
        assert wait_for(lambda: w.tier() == ("codex:daemon", None)), w.tier()
        w.hook("Stop", stop_hook_active=False)
        mid = w.say("after the re-join")
        assert wait_for(lambda: w.d.calls("turn/start")), w.batches()
        assert f"id={mid}" in w.d.calls("turn/start")[0]["input"][0]["text"]
    finally:
        w.close()


def test_the_same_mcp_process_reconnecting_keeps_its_credentials(w: W) -> None:
    """If the thread's MCP server outlives its app-server and says hello again (its
    broker connection dropped), it is re-bound through that hello, and the
    credential it holds in memory keeps working."""
    w.join()
    w.daemon_tier()
    pid = w.part()["id"]
    mcp_pid = w.part()["mcp_pid"]
    dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL)
    try:
        info = proc.info(dummy.pid)
        assert info is not None

        def set_agent() -> None:  # the app-server the broker knows for the thread: about to die
            w.b.state.store.update_participant(pid, agent_pid=dummy.pid, agent_start=info.start)
            w.b.state.agents.refresh_index()

        w.b.on_loop(set_agent)
    finally:
        dummy.kill()
        dummy.wait(5)
    assert wait_for(lambda: w.tier() == RESTARTING), w.tier()

    def drop_mcp_conn() -> int:
        n = 0
        for c in list(w.b.state.rpc.conns):
            if c.mcp is not None and c.mcp.ident.mcp_pid == mcp_pid:
                c.writer.close()
                n += 1
        return n

    assert w.b.on_loop(drop_mcp_conn) == 1
    assert wait_for(lambda: w.part()["agent_pid"] == w.cx.pid, 10), dict(w.part())
    r = w.cx.tool("say", meta={"threadId": TID}, room="#build", text="still here")
    assert r["ok"], r
    assert w.part()["mcp_pid"] == mcp_pid and w.tier() == ("codex:daemon", None)
    assert [x for x in lines(w, "notice") if "reconnected" in x] == [
        "codex-1 reconnected after a Codex daemon restart"]
    ev = [json.loads(r[0]) for r in w.q("SELECT data FROM events WHERE kind='codex_restart' ORDER BY id")]
    assert [(e["what"], e.get("via")) for e in ev] == [("app_server_gone", None), ("rebound", "mcp_hello")]


def test_the_codex_binary_is_found_again_when_it_vanishes(w: W, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Homebrew upgrade removes the old version's directory: the next codex
    queue runs the codex now on PATH (same checks), never the vanished path."""
    monkeypatch.setattr(codex_mod, "_temp_roots", lambda: set())  # the test home is under /tmp
    newdir = w.home / "upgraded" / "bin"
    newdir.mkdir(parents=True)
    new = newdir / "codex"
    new.write_text(FAKE_BIN.format(py=sys.executable))
    new.chmod(0o755)
    monkeypatch.setenv("PATH", f"{newdir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    w.join(loaded=False, attach=False)  # the queue tier: codex queue is how it is woken
    assert wait_for(lambda: w.tier() == ("codex:queue", None)), w.tier()
    w.fake_bin.unlink()
    w.hook("UserPromptSubmit", prompt="hi")
    w.hook("Stop", stop_hook_active=False)
    mid = w.say("after the upgrade")
    f = newdir / "calls.jsonl"
    assert wait_for(lambda: f.exists()), w.tier()
    [call] = [json.loads(x) for x in f.read_text().splitlines()]
    assert call["argv"][0] == os.path.realpath(new) and call["argv"][1] == "queue"
    assert f"id={mid}" in call["argv"][-1]
    ev = [json.loads(r[0]) for r in w.q("SELECT data FROM events WHERE kind='codex_bin' ORDER BY id")]
    assert ev and ev[-1]["found"] is True and ev[-1]["changed"] is True


def test_an_ephemeral_title_thread_does_not_hold_the_member(w: W) -> None:
    """0.157.0: after a session's first prompt the app-server runs an ephemeral
    "thread_title" thread of its own; one TUI, two loaded threads, no hold."""
    w.d.add_thread("019a0000-0000-7000-8000-00000000e0e0", ephemeral=True, thread_source="thread_title")
    w.join()
    w.daemon_tier()
    time.sleep(0.8)  # a few lsof looks
    assert w.tier() == ("codex:daemon", None) and w.q("SELECT * FROM events WHERE kind='codex_hold'") == []
    mid = w.say("no hold for the title thread")
    assert wait_for(lambda: w.d.calls("turn/start")), w.tier()
    assert f"id={mid}" in w.d.calls("turn/start")[0]["input"][0]["text"]


async def test_a_turn_start_stub_must_be_read_before_pass(w: W) -> None:
    """The live bug (Codex gpt-5.5, daemon turn/start): the stub said "or pass", and the
    model passed on a peer message it never read. Now read() comes first (§24)."""
    w.join()
    w.daemon_tier()
    async with FakeAgent(w.b.home, "peer") as peer:
        await peer.join("#build", "peer-1")
        mid = (await peer.say("#build", "@codex-1 the port parser drops 0; can you check?"))["posted_id"]
        assert mid
        assert wait_for(lambda: [x for x in w.batches("turn_start") if x["state"] == "confirmed"])
        [call] = w.d.calls("turn/start")
        text = call["input"][0]["text"]
        assert f"id={mid}" in text and "drops 0" not in text  # a stub: no peer text in the user role
        assert 'Call read("#build") now to see it; after reading, reply with say() or pass()' in text
        assert ", or pass(" not in text and 'Lines "not shown here": call read("#build") first' in text
        assert w.delivery(mid) == "pending"  # notified: read() only
        r = w.cx.tool("pass", meta={"threadId": TID}, room="#build")
        assert r["ok"] is False and r["code"] == "read_first" and 'Call read("#build") now' in r["error"]
        assert w.delivery(mid) == "pending" and w.q("SELECT COUNT(*) FROM events WHERE kind='pass'")[0][0] == 0
        rd = w.cx.tool("read", meta={"threadId": TID}, room="#build")
        assert rd["ok"] and f"id={mid}" in rd["text"] and "drops 0; can you check?" in rd["text"]
        # Codex fires PostToolUse for the MCP call; its output carries the batch token
        w.hook("PostToolUse", tool_name="mcp__switchboard__read", tool_input={"room": "#build"},
               tool_response={"content": [{"type": "text", "text": json.dumps(rd)}]}, tool_use_id="call_rd1")
        assert wait_for(lambda: w.delivery(mid) == "in_context")
        r = w.cx.tool("pass", meta={"threadId": TID}, room="#build")
        assert r["ok"] and r["text"] == "[switchboard] logged, not posted."
        assert w.delivery(mid) == "handled"
