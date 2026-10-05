"""Cursor contract (DESIGN.md §9.4, §12.2, §13 M5): recorded M0 payloads replayed
through the real hook script, from a stand-in ``cursor-agent`` process, against
the real broker. Covers the join-nonce binding (and its rejection from a
foreign ancestry), stop status gating, the park (fill, supersede, pause and
human-prompt release, the hook dying), the follow-up's confirmation, "degraded"
after two unconfirmed follow-ups, postToolUse context, and the provisional tier."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker
from fakes.fake_agent import ids_in
from fakes.fake_cli import CONV, FakeCli, fixture

from switchboard.adapters import cursor as cursor_adapter
from switchboard.config import Config

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
PARK_WAIT = 38  # --max-wait for the stop hook in these tests: the broker parks 8 s


@pytest.fixture
def broker(tmp_home: Path):
    b = InProcBroker(tmp_home, FAST).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web
    yield b
    web.close()
    b.stop()


@pytest.fixture
def cur(broker: InProcBroker):
    c = FakeCli(broker, "cursor")
    yield c
    c.close()


def say(b: InProcBroker, text: str) -> int:
    return b.web.post("/api/rooms/build/say", json={"text": text}, headers=b.write_headers()).json()["id"]


def command(b: InProcBroker, text: str) -> dict[str, Any]:
    r = b.web.post("/api/rooms/build/command", json={"text": text}, headers=b.write_headers())
    assert r.status_code == 200, r.text
    return r.json()


def q(b: InProcBroker, sql: str, *args: Any) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{b.paths.db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def part(b: InProcBroker, agent_pid: int | None = None) -> sqlite3.Row:
    rows = q(b, "SELECT * FROM participants WHERE harness='cursor' ORDER BY id")
    if agent_pid is not None:
        rows = [r for r in rows if r["agent_pid"] == agent_pid]
    return rows[0]


def member(b: InProcBroker, name: str = "cursor-1") -> dict[str, Any]:
    ms = b.web.get("/api/rooms/build/members").json()["members"]
    return next(m for m in ms if m["name"] == name)


def state(b: InProcBroker, mid: int) -> str:
    return q(b, "SELECT state FROM deliveries WHERE message_id=?", mid)[0][0]


def batch_for(b: InProcBroker, mid: int) -> sqlite3.Row | None:
    rows = q(b, "SELECT b.* FROM batches b JOIN deliveries d ON d.batch_id=b.id WHERE d.message_id=?", mid)
    return rows[0] if rows else None


def parks(b: InProcBroker) -> list[Any]:
    return b.app.state.broker.engine.sinks.parks()


def wait_for(fn, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.03)
    return fn()


def join_payload(j: dict[str, Any], conv: str = CONV) -> dict[str, Any]:
    """Cursor's postToolUse for ``MCP:join``: tool_output is the MCP result as a JSON string."""
    out = json.dumps({"content": [{"type": "text", "text": json.dumps(j)}], "isError": False})
    return fixture(
        "cursor",
        "postToolUse_mcp",
        conv=conv,
        tool_name="MCP:join",
        tool_input={"room": "#build", "screen_name": "cursor-1"},
        tool_output=out,
    )


def join_and_bind(b: InProcBroker, c: FakeCli, name: str = "cursor-1", conv: str = CONV) -> dict[str, Any]:
    j = c.tool("join", room="#build", screen_name=name)
    assert j["ok"], j
    assert c.hook(join_payload(j, conv)) == ""
    p = part(b, c.pid)
    assert p["bind_state"] == "bound" and p["session_key"] == f"cursor:{conv}", dict(p)
    return j


def stop_payload(status: str = "completed", loop: int = 0) -> dict[str, Any]:
    return fixture("cursor", "stop", status=status, loop_count=loop)


def wait_parked(b: InProcBroker, n: int = 1) -> None:
    assert wait_for(lambda: len(parks(b)) == n, 10), parks(b)


# ------------------------------------------------------------------ binding
def test_join_binds_by_nonce_and_conversation_id(broker: InProcBroker, cur: FakeCli) -> None:
    j = cur.tool("join", room="#build", screen_name="cursor-1")
    assert j["ok"] and j["tier"] == "mcp-only" and "yk:j" in j["text"]
    assert "follow-up message when you stop" in j["text"]
    p = part(broker)
    assert p["bind_state"] == "pending" and p["session_key"].startswith(f"cursor:agent:{cur.pid}@")
    assert p["agent_pid"] == cur.pid and p["tier"] == "mcp-only" and p["tier_note"] == "binding"
    # before the bind, hooks of the conversation are inert (no key to match)
    assert cur.hook(fixture("cursor", "postToolUse_shell")) == ""
    assert part(broker)["hooks_seen_at"] is None
    assert cur.hook(join_payload(j)) == ""
    p = part(broker)
    assert p["bind_state"] == "bound" and p["session_key"] == f"cursor:{CONV}" and p["session_id"] == CONV
    assert (p["tier"], p["tier_note"]) == ("cursor:stop-park", "provisional")
    m = wait_for(lambda: (x := member(broker))["tier"] == "cursor:stop-park" and x)
    assert m["tier_note"] == "provisional"
    assert "provisional" in command(broker, "/who")["text"]
    assert (
        "cursor-1: " in (st := command(broker, "/status")["text"]) and "cursor:stop-park (provisional)" in st
    )
    # the join code is single use: replaying it can't re-key the session to another conversation
    assert part(broker)["bind_nonce"] is None
    assert cur.hook(join_payload(j, conv="00000000-0000-4000-8000-00000000beef")) == ""
    assert part(broker)["session_key"] == f"cursor:{CONV}"
    # a re-join from the same session keeps the participant (and its binding)
    j2 = cur.tool("join", room="#build", screen_name="cursor-1")
    assert j2["ok"] and j2["text"].startswith("[switchboard] You rejoined") and part(broker)["id"] == p["id"]
    assert cur.hook(join_payload(j2)) == "" and part(broker)["session_key"] == f"cursor:{CONV}"


def test_a_bind_from_a_foreign_ancestry_is_rejected(broker: InProcBroker, cur: FakeCli) -> None:
    j = cur.tool("join", room="#build", screen_name="cursor-1")
    other = FakeCli(broker, "cursor")
    try:
        # another agent process replays the first one's join result (its nonce)
        assert other.hook(join_payload(j, conv="00000000-0000-4000-8000-00000000f00d")) == ""
        p = part(broker, cur.pid)
        assert p["bind_state"] == "pending" and p["session_key"].startswith("cursor:agent:")
        # ... even once it has joined itself (its own nonce differs)
        j2 = other.tool("join", room="#build", screen_name="cursor-2")
        assert other.hook(join_payload(j, conv="00000000-0000-4000-8000-00000000f00d")) == ""
        assert part(broker, other.pid)["bind_state"] == "pending"
        assert part(broker, cur.pid)["bind_state"] == "pending"
        # the right process with its own nonce binds
        assert cur.hook(join_payload(j)) == "" and part(broker, cur.pid)["bind_state"] == "bound"
        assert other.hook(join_payload(j2, conv="00000000-0000-4000-8000-00000000f00d")) == ""
        assert part(broker, other.pid)["session_key"] == "cursor:00000000-0000-4000-8000-00000000f00d"
        # a conversation id already held by a live session can't be taken (and the human is told)
        j3 = other.tool("join", room="#build", screen_name="cursor-2")
        assert other.hook(join_payload(j3, conv=CONV)) == ""
        assert part(broker, other.pid)["session_key"].endswith("f00d")
        notices = [
            x["text"]
            for x in broker.web.get("/api/rooms/build/messages").json()["messages"]
            if x["kind"] == "notice"
        ]
        assert any("cursor-2: can't bind" in t for t in notices), notices
    finally:
        other.close()


def test_a_kick_sticks_to_the_conversation_across_a_resume(broker: InProcBroker, cur: FakeCli) -> None:
    """``agent --resume <id>`` in a new agent process: once it binds to the kicked
    conversation it is removed again, and its later joins are refused."""
    join_and_bind(broker, cur)
    assert command(broker, "/kick cursor-1")["ok"]
    cur.close()  # the agent exits; the human resumes the conversation in a new one
    new = FakeCli(broker, "cursor")
    try:
        j = new.tool("join", room="#build", screen_name="cursor-2")
        assert j["ok"], j
        assert new.hook(join_payload(j)) == ""  # the same conversation id
        p = part(broker, new.pid)
        assert p["session_key"] == f"cursor:{CONV}" and p["bind_state"] == "bound"
        ms = broker.web.get("/api/rooms/build/members").json()["members"]
        assert not any(m["name"] == "cursor-2" for m in ms), ms
        assert q(broker, "SELECT kicked FROM memberships WHERE participant_id=?", p["id"])[0][0] == 1
        r = new.tool("join", room="#build", screen_name="cursor-2")
        assert r["ok"] is False and r["code"] == "kicked", r
    finally:
        new.close()


# ------------------------------------------------------------------ the park
def test_a_stop_that_did_not_complete_answers_at_once(broker: InProcBroker, cur: FakeCli) -> None:
    join_and_bind(broker, cur)
    say(broker, "queued while it works")
    t0 = time.monotonic()
    out = cur.hook(
        fixture("cursor", "stop_aborted", conversation_id=CONV, session_id=CONV), max_wait=PARK_WAIT
    )
    assert out == "" and time.monotonic() - t0 < 3.0
    assert parks(broker) == [] and part(broker)["status"] == "idle"


def test_a_park_is_filled_as_a_followup_and_confirmed_by_the_next_hook(
    broker: InProcBroker, cur: FakeCli
) -> None:
    join_and_bind(broker, cur)
    tag = cur.hook_bg(stop_payload(), max_wait=PARK_WAIT)
    wait_parked(broker)
    assert wait_for(lambda: part(broker)["status"] == "idle")
    budget = command(broker, "/budget")["text"]
    t0 = time.monotonic()
    mid = say(broker, "please rerun the tests")
    r = cur.collect(tag)
    lat = time.monotonic() - t0
    out = json.loads(r["stdout"])
    assert set(out) == {"followup_message"} and out["followup_message"].startswith("[switchboard]")
    assert ids_in(out["followup_message"]) == [mid] and "please rerun the tests" in out["followup_message"]
    b = batch_for(broker, mid)
    assert (b["path"], b["kind"], b["wake_kind"], b["budget_counted"]) == (
        "stop_followup",
        "wake",
        "stop_cont",
        1,
    )
    assert b["state"] == "offered"  # printed and acked, not yet seen in a turn
    assert part(broker)["status"] == "busy" and budget != command(broker, "/budget")["text"]
    cur.hook(fixture("cursor", "postToolUse_shell"))
    b = wait_for(lambda: (x := batch_for(broker, mid))["state"] == "confirmed" and x)
    assert b["evidence"] == "hook:PostToolUse" and b["turn_start_at"] is not None
    assert state(broker, mid) == "in_context"
    print(f"cursor park: post to printed follow-up {lat * 1000:.0f} ms (fake harness, real hook)")


def test_a_newer_stop_supersedes_the_old_park(broker: InProcBroker, cur: FakeCli) -> None:
    join_and_bind(broker, cur)
    first = cur.hook_bg(stop_payload(loop=0), max_wait=PARK_WAIT)
    wait_parked(broker)
    old = parks(broker)[0].id
    second = cur.hook_bg(stop_payload(loop=1), max_wait=PARK_WAIT)
    assert wait_for(lambda: len(parks(broker)) == 1 and parks(broker)[0].id != old, 10)
    r1 = cur.collect(first, timeout=10)
    assert r1 == {"rc": 0, "stdout": ""}  # released with no continuation
    mid = say(broker, "to the newest park")
    r2 = cur.collect(second)
    assert ids_in(json.loads(r2["stdout"])["followup_message"]) == [mid]


def test_pause_releases_the_park(broker: InProcBroker, cur: FakeCli) -> None:
    join_and_bind(broker, cur)
    tag = cur.hook_bg(stop_payload(), max_wait=PARK_WAIT)
    wait_parked(broker)
    command(broker, "/pause")
    assert cur.collect(tag, timeout=10) == {"rc": 0, "stdout": ""}
    say(broker, "while paused")
    assert parks(broker) == []


def test_the_humans_prompt_releases_the_park(broker: InProcBroker, cur: FakeCli) -> None:
    join_and_bind(broker, cur)
    tag = cur.hook_bg(stop_payload(), max_wait=PARK_WAIT)
    wait_parked(broker)
    cur.hook(fixture("cursor", "beforeSubmitPrompt"))
    assert cur.collect(tag, timeout=10) == {"rc": 0, "stdout": ""}
    assert part(broker)["status"] == "busy"


def test_a_park_that_times_out_prints_nothing(broker: InProcBroker, cur: FakeCli) -> None:
    join_and_bind(broker, cur)
    t0 = time.monotonic()
    tag = cur.hook_bg(stop_payload(), max_wait=31)  # a 1 s park
    assert cur.collect(tag, timeout=15) == {"rc": 0, "stdout": ""}
    assert time.monotonic() - t0 < 10
    mid = say(broker, "after the park ended")
    assert wait_for(lambda: member(broker)["parked"], 5)
    assert state(broker, mid) == "pending"


def _hook_pids(root: int) -> list[int]:
    out = subprocess.run(["/bin/ps", "-A", "-o", "pid=,ppid=,args="], capture_output=True, text=True).stdout
    procs = [
        (int(a), int(b_), c)
        for a, b_, c in (
            ln.strip().split(None, 2) for ln in out.splitlines() if len(ln.strip().split(None, 2)) == 3
        )
    ]
    kids: dict[int, list[tuple[int, str]]] = {}
    for pid, ppid, args in procs:
        kids.setdefault(ppid, []).append((pid, args))
    found, stack = [], [root]
    while stack:
        for pid, args in kids.get(stack.pop(), []):
            stack.append(pid)
            if "switchboard_hook-" in args and "--event stop" in args:
                found.append(pid)
    return found


def test_the_park_ends_when_the_hook_process_dies(broker: InProcBroker, cur: FakeCli) -> None:
    join_and_bind(broker, cur)
    tag = cur.hook_bg(stop_payload(), max_wait=PARK_WAIT)
    wait_parked(broker)
    pids = wait_for(lambda: _hook_pids(cur.pid), 5)
    assert pids
    for pid in pids:
        os.kill(pid, signal.SIGKILL)  # e.g. Cursor's hook timeout killed it
    assert wait_for(lambda: parks(broker) == [], 5)
    cur.collect(tag, timeout=10)
    mid = say(broker, "nobody is parked")
    assert wait_for(lambda: member(broker)["parked"], 5) and state(broker, mid) == "pending"


def test_two_unconfirmed_followups_degrade_until_the_next_prompt(broker: InProcBroker, cur: FakeCli) -> None:
    join_and_bind(broker, cur)
    say(broker, "are you there?")
    for n in (1, 2):
        tag = cur.hook_bg(stop_payload(loop=0), max_wait=PARK_WAIT)
        r = cur.collect(tag)
        assert "followup_message" in json.loads(r["stdout"]), (n, r)
    # the follow-up never ran: the next stop's loop_count is 0 again (a reset)
    t0 = time.monotonic()
    out = cur.hook(stop_payload(loop=0), max_wait=PARK_WAIT)
    assert out == "" and time.monotonic() - t0 < 3.0  # degraded: no park
    p = part(broker)
    assert p["unconfirmed_followups"] == 2 and p["tier_note"] == "provisional, degraded"
    m = wait_for(lambda: (x := member(broker))["parked"] and x)
    assert "degraded" in m["parked_reason"] and m["tier_note"] == "provisional, degraded"
    notices = [
        x["text"]
        for x in broker.web.get("/api/rooms/build/messages").json()["messages"]
        if x["kind"] == "notice"
    ]
    assert any("not confirmed" in t for t in notices)
    cur.hook(fixture("cursor", "beforeSubmitPrompt"))
    p = part(broker)
    assert p["unconfirmed_followups"] == 0 and p["tier_note"] == "provisional"


def test_dropped_followups_degrade_across_human_prompts(
    broker: InProcBroker, cur: FakeCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sequence Cursor really produces when it ignores a follow-up (a park past
    ~38 s, F§5 4.3): no hook at all until the human types (beforeSubmitPrompt, and
    loop_count starts again at 0). The member must show parked, not busy, and the
    second miss must degrade it."""
    monkeypatch.setattr(cursor_adapter, "FOLLOWUP_CONFIRM_S", 1.0)  # 180 s in production
    join_and_bind(broker, cur)
    for n in (1, 2):
        say(broker, f"are you there? {n}")
        r = cur.collect(cur.hook_bg(stop_payload(loop=0), max_wait=PARK_WAIT))
        assert "followup_message" in json.loads(r["stdout"]), (n, r)
        # ... which Cursor never runs: no further hook; the offer expires
        assert wait_for(lambda n=n: part(broker)["unconfirmed_followups"] == n, 8), (n, dict(part(broker)))
        assert part(broker)["status"] == "idle"
        assert wait_for(lambda: member(broker)["parked"]), member(broker)
        if n == 1:
            cur.hook(fixture("cursor", "beforeSubmitPrompt"))  # the human types: the count stands
            assert part(broker)["unconfirmed_followups"] == 1
            cur.hook(fixture("cursor", "postToolUse_shell"))
    p = part(broker)
    assert p["tier_note"] == "provisional, degraded"
    assert "degraded" in wait_for(lambda: (x := member(broker))["parked"] and x)["parked_reason"]
    t0 = time.monotonic()
    assert cur.hook(stop_payload(loop=0), max_wait=PARK_WAIT) == "" and time.monotonic() - t0 < 3.0
    cur.hook(fixture("cursor", "beforeSubmitPrompt"))  # the human's next prompt ends the spell
    assert part(broker)["tier_note"] == "provisional"


# --------------------------------------------------------------- mid-task
def test_posttooluse_context_for_priority_items(broker: InProcBroker, cur: FakeCli) -> None:
    join_and_bind(broker, cur)
    cur.hook(fixture("cursor", "beforeSubmitPrompt"))
    mid = say(broker, "@cursor-1 also update the docs")
    out = json.loads(cur.hook(fixture("cursor", "postToolUse_shell")))
    assert set(out) == {"additional_context"} and len(out["additional_context"]) <= 8000
    assert ids_in(out["additional_context"]) == [mid]
    assert wait_for(lambda: state(broker, mid) == "in_context")
    [b] = q(broker, "SELECT * FROM batches WHERE path='hook_ctx'")
    assert b["evidence"] == "hook_ack" and b["budget_counted"] == 0
    # a failed tool carries it too
    mid2 = say(broker, "and one more thing")
    out = json.loads(cur.hook(fixture("cursor", "postToolUseFailure_shell")))
    assert ids_in(out["additional_context"]) == [mid2]


def test_session_end_takes_the_member_offline(broker: InProcBroker, cur: FakeCli) -> None:
    join_and_bind(broker, cur)
    cur.hook(fixture("cursor", "sessionEnd"))
    assert part(broker)["status"] == "offline"
