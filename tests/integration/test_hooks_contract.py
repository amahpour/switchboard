"""Claude hook contract: recorded fixture payloads replayed through the real hook
script, from a process whose ancestry is a (fake) verified Claude session, against
the real broker (DESIGN.md §7, §12.2, §12.3)."""

from __future__ import annotations

import json
import shlex
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import InProcBroker
from fakes.fake_agent import ids_in
from fakes.fake_claude import SID, FakeClaude, fixture
from switchboard.config import Config
from switchboard.envelope import TOKEN_RE
from switchboard.install.common import hook_command
from switchboard.paths import hook_sha12

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)


@pytest.fixture
def broker(tmp_home: Path):
    b = InProcBroker(tmp_home, FAST).start()
    # the MCP server reads the same config file the broker was built from
    (tmp_home / "config.toml").write_text(f'[claude]\nsessions_dir = "{b.cfg.claude.sessions_dir}"\n')
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web
    yield b
    web.close()
    b.stop()


@pytest.fixture
def claude(broker: InProcBroker):
    c = FakeClaude(broker)
    yield c
    c.close()


def say(b: InProcBroker, text: str) -> int:
    return b.web.post("/api/rooms/build/say", json={"text": text}, headers=b.write_headers()).json()["id"]


def q(b: InProcBroker, sql: str, *args: Any) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{b.paths.db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def part(b: InProcBroker) -> sqlite3.Row:
    [row] = q(b, "SELECT * FROM participants WHERE harness='claude'")
    return row


def member(b: InProcBroker, name: str = "claude-1") -> dict[str, Any]:
    ms = b.web.get("/api/rooms/build/members").json()["members"]
    return next(m for m in ms if m["name"] == name)


def state(b: InProcBroker, mid: int) -> str:
    return q(b, "SELECT state FROM deliveries WHERE message_id=?", mid)[0][0]


def wait_for(fn, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.03)
    return fn()


def test_unjoined_claude_session_hooks_are_inert(broker: InProcBroker, claude: FakeClaude) -> None:
    assert claude.hook(fixture("SessionStart_startup")) == ""
    assert claude.hook(fixture("PostToolUse_bash")) == ""
    assert q(broker, "SELECT COUNT(*) FROM participants")[0][0] == 0


def test_join_binds_a_verified_claude_session(broker: InProcBroker, claude: FakeClaude) -> None:
    j = claude.tool("join", room="#build", screen_name="claude-1")
    assert j["ok"] and j["tier"] == "claude:hook", j
    p = part(broker)
    assert p["session_key"].startswith(f"claude:{claude.pid}@")
    assert p["agent_pid"] == claude.pid and p["claude_socket"] == claude.inbox_path
    assert p["session_id"] == SID and p["env_leak"] == 0
    assert member(broker)["harness"] == "claude" and member(broker)["tier"] == "claude:hook"
    assert "context after your tool calls" in j["text"]


def test_the_reported_model_is_recorded_once_per_change(broker: InProcBroker, claude: FakeClaude) -> None:
    """The report names each participant's model (DESIGN.md §12.6). Claude reports it only in
    SessionStart, which comes before any join: it is kept for that process and recorded at join."""
    start = fixture("SessionStart_startup")
    assert claude.hook(start) == ""  # not joined yet: inert, but the model is remembered
    assert q(broker, "SELECT id FROM events WHERE kind='model'") == []
    claude.tool("join", room="#build", screen_name="claude-1")
    rows = q(broker, "SELECT participant_id, data FROM events WHERE kind='model'")
    assert [json.loads(r["data"]) for r in rows] == [{"model": start["model"]}]
    assert rows[0]["participant_id"] == part(broker)["id"]
    assert claude.hook(start) == ""  # the same model again: no second event
    assert len(q(broker, "SELECT id FROM events WHERE kind='model'")) == 1
    claude.hook({**start, "model": "claude-sonnet-4-6"})
    assert len(q(broker, "SELECT id FROM events WHERE kind='model'")) == 2
    claude.hook({**start, "model": "/Users/someone/not-a-model"})  # the hook drops it; nothing recorded
    assert len(q(broker, "SELECT id FROM events WHERE kind='model'")) == 2


def test_status_transitions_from_recorded_payloads(broker: InProcBroker, claude: FakeClaude) -> None:
    claude.tool("join", room="#build", screen_name="claude-1")
    assert claude.hook(fixture("SessionStart_startup")) == ""
    assert part(broker)["status"] == "idle"
    claude.hook(fixture("UserPromptSubmit"))
    p = part(broker)
    assert p["status"] == "busy" and p["gen"] == fixture("UserPromptSubmit")["prompt_id"]
    assert p["approval_mode"] == "prompting" and p["hooks_seen_at"] is not None
    seq = p["boundary_seq"]
    claude.hook(fixture("PostToolUse_bash"))
    assert part(broker)["status"] == "busy"
    claude.hook(fixture("Stop"))
    p = part(broker)
    assert p["status"] == "idle" and p["boundary_seq"] == seq + 1
    claude.hook(fixture("SessionEnd_clear"))
    assert part(broker)["status"] == "idle"  # /clear keeps the membership and the status
    claude.hook(fixture("SessionEnd_exit"))
    assert part(broker)["status"] == "offline"
    assert q(broker, "SELECT COUNT(*) FROM events WHERE kind='turn_start'")[0][0] == 1


def test_bypass_payload_marks_the_member(broker: InProcBroker, claude: FakeClaude) -> None:
    claude.tool("join", room="#build", screen_name="claude-1")
    claude.hook(fixture("PostToolUse_bypass"))
    assert part(broker)["approval_mode"] == "bypass"
    assert wait_for(lambda: member(broker)["approval_mode"] == "bypass")


def test_mid_task_priority_arrives_as_posttooluse_context_and_is_acked(broker: InProcBroker, claude: FakeClaude) -> None:
    claude.tool("join", room="#build", screen_name="claude-1")
    claude.hook(fixture("UserPromptSubmit"))
    mid = say(broker, "please also check the tests")
    out = claude.hook(fixture("PostToolUse_bash"))
    data = json.loads(out)
    ctx = data["hookSpecificOutput"]
    assert set(data) == {"hookSpecificOutput"} and ctx["hookEventName"] == "PostToolUse"
    assert ids_in(ctx["additionalContext"]) == [mid] and ctx["additionalContext"].startswith("[switchboard]")
    assert wait_for(lambda: state(broker, mid) == "in_context")
    [b] = q(broker, "SELECT * FROM batches WHERE path='hook_ctx'")
    assert b["state"] == "confirmed" and b["evidence"] == "hook_ack" and b["budget_counted"] == 0
    # the next tool boundary has nothing new
    assert claude.hook(fixture("PostToolUse_bash")) == ""


def test_failed_tools_also_carry_context(broker: InProcBroker, claude: FakeClaude) -> None:
    claude.tool("join", room="#build", screen_name="claude-1")
    claude.hook(fixture("UserPromptSubmit"))
    mid = say(broker, "heads up")
    out = json.loads(claude.hook(fixture("PostToolUseFailure_bash")))
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    assert ids_in(out["hookSpecificOutput"]["additionalContext"]) == [mid]


def test_session_start_clear_prints_a_membership_reminder(broker: InProcBroker, claude: FakeClaude) -> None:
    claude.tool("join", room="#build", screen_name="claude-1")
    assert claude.hook(fixture("SessionStart_startup")) == ""
    out = json.loads(claude.hook(fixture("SessionStart_clear")))
    assert "#build as claude-1" in out["hookSpecificOutput"]["additionalContext"]


def test_read_is_confirmed_by_the_posttooluse_that_carries_its_token(broker: InProcBroker, claude: FakeClaude) -> None:
    claude.tool("join", room="#build", screen_name="claude-1")
    claude.hook(fixture("UserPromptSubmit"))
    mid = say(broker, "x")
    # the member has hooks: the answer waits for PostToolUse, not for the next call
    r = claude.tool("read", room="#build")
    assert ids_in(r["text"]) == [mid] and state(broker, mid) == "offered"
    claude.tool("who", room="#build")
    assert state(broker, mid) == "offered"
    payload = fixture("PostToolUse_mcp", tool_name="mcp__switchboard__read",
                      tool_response=[{"type": "text", "text": json.dumps(r)}])
    claude.hook(payload)
    assert wait_for(lambda: state(broker, mid) == "in_context")


def test_foreign_token_does_not_confirm(broker: InProcBroker, claude: FakeClaude) -> None:
    claude.tool("join", room="#build", screen_name="claude-1")
    claude.hook(fixture("UserPromptSubmit"))
    mid = say(broker, "x")
    r = claude.tool("read", room="#build")
    tok = TOKEN_RE.search(r["text"])
    forged = f"yk:b{tok.group(1)}.{'0' * 8 if tok.group(2) != '0' * 8 else '1' * 8}"
    claude.hook(fixture("PostToolUse_mcp", tool_response=[{"type": "text", "text": forged}]))
    assert state(broker, mid) == "offered"
    # a different batch id with a mac for another membership: nothing either
    claude.hook(fixture("PostToolUse_mcp", tool_response=[{"type": "text", "text": f"yk:b{int(tok.group(1)) + 99}.{tok.group(2)}"}]))
    assert state(broker, mid) == "offered"


def test_stop_without_a_listener_parks_the_member(broker: InProcBroker, claude: FakeClaude) -> None:
    claude.tool("join", room="#build", screen_name="claude-1")
    claude.hook(fixture("UserPromptSubmit"))
    claude.hook(fixture("Stop"))
    say(broker, "are you there?")
    assert wait_for(lambda: member(broker)["parked"])
    assert "wait()" in member(broker)["parked_reason"]


def test_joined_hook_latency(broker: InProcBroker, claude: FakeClaude) -> None:
    claude.tool("join", room="#build", screen_name="claude-1")
    ts = []
    for _ in range(10):
        t0 = time.perf_counter()
        claude.hook(fixture("PostToolUse_bash"))
        ts.append(time.perf_counter() - t0)
    ts.sort()
    print(f"joined claude hook round trip p50={ts[5]*1000:.1f} ms max={ts[-1]*1000:.1f} ms (n=10, via fake harness)")
    assert ts[5] < 0.5


def test_a_dead_agent_ends_its_session(broker: InProcBroker, claude: FakeClaude) -> None:
    claude.tool("join", room="#build", screen_name="claude-1")
    claude.p.kill()  # the whole harness dies (its MCP server sees EOF)
    claude.p.wait(5)
    assert wait_for(lambda: part(broker)["ended_at"] is not None, 10)
    assert q(broker, "SELECT left_reason FROM memberships WHERE screen_name='claude-1'")[0][0] == "session_end"
    msgs = broker.web.get("/api/rooms/build/messages").json()["messages"]
    assert msgs[-1]["kind"] == "leave" and "session ended" in msgs[-1]["text"]
    assert broker.web.get("/api/rooms/build/members").json()["members"] == []


def test_codex_sessions_are_keyed_by_thread(broker: InProcBroker) -> None:
    cx = FakeClaude(broker, as_harness="codex")
    try:
        r = cx.tool("join", room="#build", screen_name="codex-1")
        assert r["ok"] is False and "thread" in r["error"]  # no _meta.threadId: refused
        r = cx.tool("join", meta={"threadId": "thread-A"}, room="#build", screen_name="codex-1")
        assert r["ok"], r
        r = cx.tool("join", meta={"threadId": "thread-B"}, room="#build", screen_name="codex-2")
        assert r["ok"], r
        rows = q(broker, "SELECT session_key, agent_pid, tier FROM participants WHERE harness='codex' ORDER BY id")
        assert [x[0] for x in rows] == ["codex:thread-A", "codex:thread-B"]
        assert {x[1] for x in rows} == {cx.pid} and {x[2] for x in rows} == {"mcp-only"}
        mid = say(broker, "for both threads")
        a = cx.tool("read", meta={"threadId": "thread-A"}, room="#build")
        assert ids_in(a["text"]) == [mid]
        # thread C never joined: its call carries no credential for #build
        c = cx.tool("read", meta={"threadId": "thread-C"}, room="#build")
        assert c["ok"] is False and c["code"] == "not_member"
        # a Codex hook resolves by session id among threads of the same daemon; M2 prints nothing
        cmd_payload = {"hook_event_name": "PostToolUse", "session_id": "thread-A", "tool_name": "Bash",
                       "tool_response": "ok", "permission_mode": "default", "cwd": "/ws"}
        cmd = hook_command(sys.executable, str(broker.paths.home), hook_sha12(), "codex", "PostToolUse")
        cx.p.stdin.write(json.dumps({"op": "hook", "command": cmd, "payload": cmd_payload}) + "\n")
        cx.p.stdin.flush()
        assert cx.recv()["stdout"] == ""
        rows = q(broker, "SELECT session_key, approval_mode, hooks_seen_at FROM participants WHERE harness='codex' ORDER BY id")
        assert rows[0][1] == "prompting" and rows[0][2] is not None  # thread A only
        assert rows[1][1] == "unknown" and rows[1][2] is None
    finally:
        cx.close()


def run_hook_as(cx: FakeClaude, harness: str, payload: dict[str, Any], event: str,
                wrap: str | None = None) -> str:
    """Run a hook command as a child of the stand-in (optionally via ``wrap``, a
    nested stand-in session in between)."""
    cmd = hook_command(sys.executable, str(cx.b.paths.home), hook_sha12(), harness, event)
    if wrap:
        cmd = f"{shlex.quote(sys.executable)} {shlex.quote(wrap)} {shlex.quote(cmd)}"
    cx.p.stdin.write(json.dumps({"op": "hook", "command": cmd, "payload": payload}) + "\n")
    cx.p.stdin.flush()
    r = cx.recv()
    assert r["rc"] == 0, r
    return r["stdout"]


NESTED = "import subprocess, sys\nsys.exit(subprocess.run(sys.argv[1], shell=True).returncode)\n"


def test_a_nested_sessions_hooks_are_not_credited_to_the_outer_session(broker: InProcBroker,
                                                                       claude: FakeClaude) -> None:
    """`claude -p` run from a joined Claude's Bash fires the same user-level hooks.
    They must not claim the outer member's context, flip its status or re-key it."""
    claude.tool("join", room="#build", screen_name="claude-1")
    claude.hook(fixture("UserPromptSubmit"))
    mid = say(broker, "for the outer session only")
    d = Path(tempfile.mkdtemp(prefix="yk-nest-", dir="/tmp"))
    try:
        nested = d / "claude"  # argv ends in /claude: another Claude session in between
        nested.write_text(NESTED)
        other = "00000000-0000-4000-8000-00000000beef"
        for name in ("PostToolUse_bash", "Stop", "SessionStart_startup", "SessionEnd_exit"):
            payload = fixture(name, session_id=other)
            assert run_hook_as(claude, "claude", payload, payload["hook_event_name"], wrap=str(nested)) == ""
        p = part(broker)
        assert p["status"] == "busy" and p["session_id"] == SID and state(broker, mid) == "pending"
        # the outer session's own hook still gets it
        out = json.loads(claude.hook(fixture("PostToolUse_bash")))
        assert ids_in(out["hookSpecificOutput"]["additionalContext"]) == [mid]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_an_unjoined_sibling_thread_cannot_touch_a_joined_thread(broker: InProcBroker) -> None:
    cx = FakeClaude(broker, as_harness="codex")
    try:
        assert cx.tool("join", meta={"threadId": "thread-A"}, room="#build", screen_name="codex-1")["ok"]
        rows = lambda: [tuple(x) for x in q(broker, "SELECT session_key, status, approval_mode, hooks_seen_at"  # noqa: E731
                                                     " FROM participants")]
        before = rows()
        assert before[0][3] is None
        for ev, extra in (("PostToolUse", {"tool_name": "Bash", "tool_response": "ok"}), ("Stop", {}),
                          ("SessionEnd", {"reason": "exit"})):
            payload = {"hook_event_name": ev, "session_id": "thread-B", "permission_mode": "bypassPermissions",
                       "cwd": "/ws", **extra}
            assert run_hook_as(cx, "codex", payload, ev) == ""
        assert rows() == before
        # thread A's own hook is credited to it
        run_hook_as(cx, "codex", {"hook_event_name": "PostToolUse", "session_id": "thread-A", "tool_name": "Bash",
                                  "tool_response": "ok", "permission_mode": "default", "cwd": "/ws"}, "PostToolUse")
        assert rows()[0][2] == "prompting" and rows()[0][3] is not None
    finally:
        cx.close()


def test_a_codex_thread_cannot_be_taken_over_from_another_process(broker: InProcBroker) -> None:
    a = FakeClaude(broker, as_harness="codex")
    b = FakeClaude(broker, as_harness="codex")
    try:
        assert a.tool("join", meta={"threadId": "thread-A"}, room="#build", screen_name="codex-1")["ok"]
        for name in ("codex-1", "codex-9"):
            r = b.tool("join", meta={"threadId": "thread-A"}, room="#build", screen_name=name)
            assert r["ok"] is False and r["code"] == "conflict", r
        assert a.tool("say", meta={"threadId": "thread-A"}, room="#build", text="still me")["ok"]
        assert a.tool("read", meta={"threadId": "thread-A"}, room="#build")["ok"]
        [row] = q(broker, "SELECT agent_pid FROM participants WHERE harness='codex'")
        assert row[0] == a.pid
        # once A's codex and MCP server are gone, the thread can be resumed elsewhere
        a.close()
        r = wait_for(lambda: (x := b.tool("join", meta={"threadId": "thread-A"}, room="#build",
                                          screen_name="codex-1"))["ok"] and x, 10)
        assert r and r["text"].startswith("[switchboard] You rejoined #build as codex-1")
        [row] = q(broker, "SELECT agent_pid FROM participants WHERE harness='codex'")
        assert row[0] == b.pid
    finally:
        a.close()
        b.close()
