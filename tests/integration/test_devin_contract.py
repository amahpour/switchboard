"""Devin contract (DESIGN.md §9.5, §12.2, §13 M5): recorded M0 payloads replayed
through the real hook script, from a stand-in ``devin acp`` process (with
``DEVIN_PROJECT_DIR`` in the hook env, as Devin sets it), against the real
broker. Covers the wait loop and its two-phase ack, supersede, the orphaned
wait expiring at the next hook, PostToolUse context, the subagent taint (no
continue, no context), the Stop block and re-arm, and that no Devin hook ever
prints an approval."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker
from fakes.fake_agent import ids_in
from fakes.fake_cli import DEVIN_SID, FakeCli, fixture

from switchboard.config import Config
from switchboard.envelope import TOKEN_RE

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
WAIT = "mcp__switchboard__wait"


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
def dv(broker: InProcBroker):
    c = FakeCli(broker, "devin")
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
    return q(b, "SELECT * FROM participants WHERE harness='devin'")[0]


def state(b: InProcBroker, mid: int) -> str:
    return q(b, "SELECT state FROM deliveries WHERE message_id=?", mid)[0][0]


def batch(b: InProcBroker, bid: int) -> sqlite3.Row:
    return q(b, "SELECT * FROM batches WHERE id=?", bid)[0]


def wait_for(fn, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.03)
    return fn()


def sinks(b: InProcBroker) -> list[Any]:
    return b.app.state.broker.engine.sinks.open_sinks()


def joined(b: InProcBroker, dv: FakeCli) -> dict[str, Any]:
    j = dv.tool("join", room="#build", screen_name="devin-1")
    assert j["ok"], j
    dv.hook(fixture("devin", "SessionStart"))
    dv.hook(fixture("devin", "UserPromptSubmit"))
    return j


def pre_wait(dv: FakeCli, tuid: str) -> None:
    assert (
        dv.hook(
            fixture(
                "devin", "PreToolUse_read", tool_name=WAIT, tool_input={"room": "#build"}, tool_use_id=tuid
            )
        )
        == ""
    )


def post_wait(dv: FakeCli, tuid: str, result: dict[str, Any], success: bool = True) -> str:
    return dv.hook(
        fixture(
            "devin",
            "PostToolUse_mcp_wait",
            tool_name=WAIT,
            tool_use_id=tuid,
            tool_input={"room": "#build", "timeout_s": 60},
            tool_response={"success": success, "output": json.dumps(result), "error": None},
        )
    )


# --------------------------------------------------------------- identity
def test_join_binds_the_devin_acp_process(broker: InProcBroker, dv: FakeCli) -> None:
    j = dv.tool("join", room="#build", screen_name="devin-1")
    assert j["ok"] and j["tier"] == "devin:wait-loop"
    assert 'wait("#build", 600)' in j["text"] and "Enter on an empty line" in j["text"]
    p = part(broker)
    assert p["agent_pid"] == dv.pid and p["session_key"].startswith(f"devin:{dv.pid}@")
    dv.hook(fixture("devin", "SessionStart"))
    p = part(broker)
    assert p["status"] == "idle" and p["session_id"] == DEVIN_SID
    dv.hook(fixture("devin", "UserPromptSubmit"))
    assert (
        part(broker)["status"] == "busy"
        and part(broker)["gen"] == fixture("devin", "UserPromptSubmit")["prompt_id"]
    )
    dv.hook(fixture("devin", "SessionEnd"))
    assert part(broker)["status"] == "offline"


# ----------------------------------------------------------------- wait loop
def test_wait_loop_two_phase_ack_by_the_calls_own_posttooluse(broker: InProcBroker, dv: FakeCli) -> None:
    joined(broker, dv)
    pre_wait(dv, "call_w1")
    tag = dv.tool_bg("wait", room="#build", timeout_s=60)
    assert wait_for(lambda: len(sinks(broker)) == 1, 5)
    t0 = time.monotonic()
    mid = say(broker, "wake up and say hi")
    res = dv.collect(tag)["result"]
    lat = time.monotonic() - t0
    assert res["status"] == "messages" and ids_in(res["text"]) == [mid]
    bid = res["batch_id"]
    assert batch(broker, bid)["wake_kind"] == "wait_return" and batch(broker, bid)["budget_counted"] == 1
    # another call's PostToolUse, or a failed one, doesn't confirm it
    post_wait(dv, "call_other", res)
    post_wait(dv, "call_w1", res, success=False)
    assert batch(broker, bid)["state"] == "offered" and state(broker, mid) == "offered"
    post_wait(dv, "call_w1", res)
    b = wait_for(lambda: (x := batch(broker, bid))["state"] == "confirmed" and x)
    assert b["turn_start_at"] is not None and state(broker, mid) == "in_context"
    # the agent's first action after the wake is its next PreToolUse
    dv.hook(fixture("devin", "PreToolUse_read"))
    assert batch(broker, bid)["first_action_at"] is not None
    print(f"devin wait: post to wait() result {lat * 1000:.0f} ms (fake harness)")


def test_a_newer_wait_supersedes_the_older(broker: InProcBroker, dv: FakeCli) -> None:
    joined(broker, dv)
    first = dv.tool_bg("wait", room="#build", timeout_s=60)
    assert wait_for(lambda: len(sinks(broker)) == 1, 5)
    second = dv.tool_bg("wait", room="#build", timeout_s=60)
    r1 = dv.collect(first)["result"]
    assert r1["status"] == "superseded"
    mid = say(broker, "to the newer wait")
    r2 = dv.collect(second)["result"]
    assert r2["status"] == "messages" and ids_in(r2["text"]) == [mid]


def test_an_orphaned_wait_ends_at_the_next_hook_and_loses_nothing(broker: InProcBroker, dv: FakeCli) -> None:
    joined(broker, dv)
    tag = dv.tool_bg("wait", room="#build", timeout_s=60)
    assert wait_for(lambda: len(sinks(broker)) == 1, 5)
    time.sleep(0.05)
    # the human interrupted (Devin sends the MCP server no cancel) and typed
    dv.hook(fixture("devin", "UserPromptSubmit", prompt_id="00000000-0000-4000-8000-0000000000aa"))
    assert dv.collect(tag)["result"]["status"] == "superseded"
    mid = say(broker, "after the interrupt")
    time.sleep(0.2)
    assert state(broker, mid) == "pending" and sinks(broker) == []


def test_an_answer_that_reached_an_orphan_comes_back(broker: InProcBroker, dv: FakeCli) -> None:
    joined(broker, dv)
    tag = dv.tool_bg("wait", room="#build", timeout_s=60)
    assert wait_for(lambda: len(sinks(broker)) == 1, 5)
    mid = say(broker, "swallowed by the orphan?")
    res = dv.collect(tag)["result"]  # the answer Devin would have dropped ("Error sending response")
    assert res["status"] == "messages"
    time.sleep(0.05)
    dv.hook(fixture("devin", "PreToolUse_read"))  # the agent moved on without that result
    assert wait_for(lambda: state(broker, mid) == "pending")
    assert batch(broker, res["batch_id"])["expire_reason"] == "hook:PreToolUse"


# ---------------------------------------------------------------- mid-task
def test_posttooluse_context_is_nested_and_acked(broker: InProcBroker, dv: FakeCli) -> None:
    joined(broker, dv)
    mid = say(broker, "mid-task note from alice")
    out = json.loads(dv.hook(fixture("devin", "PostToolUse_read")))
    assert set(out) == {"hookSpecificOutput"}
    ctx = out["hookSpecificOutput"]
    assert ctx["hookEventName"] == "PostToolUse" and ids_in(ctx["additionalContext"]) == [mid]
    assert wait_for(lambda: state(broker, mid) == "in_context")


def test_a_background_subagent_means_no_context_and_no_continue(broker: InProcBroker, dv: FakeCli) -> None:
    joined(broker, dv)
    dv.hook(
        fixture(
            "devin", "PreToolUse_run_subagent_bg", prompt_id=fixture("devin", "UserPromptSubmit")["prompt_id"]
        )
    )
    assert part(broker)["gen_tainted"] == 1
    mid = say(broker, "must not reach the subagent")
    assert dv.hook(fixture("devin", "PostToolUse_read")) == ""
    seq = part(broker)["boundary_seq"]
    assert dv.hook(fixture("devin", "Stop")) == ""  # a block would continue the subagent
    p = part(broker)
    assert p["status"] == "busy" and p["boundary_seq"] == seq and state(broker, mid) == "pending"
    m = broker.web.get("/api/rooms/build/members").json()["members"][0]
    assert m["parked"] and "subagent" in m["parked_reason"]
    dv.hook(fixture("devin", "UserPromptSubmit", prompt_id="00000000-0000-4000-8000-0000000000bb"))
    assert part(broker)["gen_tainted"] == 0


# -------------------------------------------------------------------- Stop
def test_stop_blocks_with_the_pending_wake_then_rearms(broker: InProcBroker, dv: FakeCli) -> None:
    joined(broker, dv)
    mid = say(broker, "review the diff")
    out = json.loads(dv.hook(fixture("devin", "Stop")))
    assert set(out) == {"decision", "reason"} and out["decision"] == "block"
    assert ids_in(out["reason"]) == [mid] and out["reason"].startswith("[switchboard]")
    bid = int(TOKEN_RE.search(out["reason"]).group(1))
    b = batch(broker, bid)
    assert (b["path"], b["wake_kind"], b["budget_counted"], b["state"]) == (
        "stop_block",
        "stop_cont",
        1,
        "offered",
    )
    assert part(broker)["status"] == "busy"
    dv.hook(fixture("devin", "PreToolUse_read"))  # the continued turn's first action
    b = wait_for(lambda: (x := batch(broker, bid))["state"] == "confirmed" and x)
    assert b["first_action_at"] is not None and state(broker, mid) == "in_context"
    # unanswered, it would come back once at the next Stop (again=yes); the agent passes
    assert dv.tool("pass", room="#build")["ok"]
    # nothing pending: the Stop re-arms the wait loop (counted), twice per prompt at most
    for n in (1, 2):
        out = json.loads(dv.hook(fixture("devin", "Stop", stop_hook_active=True)))
        assert out["decision"] == "block" and 'wait("#build", 600)' in out["reason"]
        assert "not your user" in out["reason"] and part(broker)["rearms_in_gen"] == n
    assert dv.hook(fixture("devin", "Stop", stop_hook_active=True)) == ""
    assert q(broker, "SELECT COUNT(*) FROM events WHERE kind='rearm'")[0][0] == 2


def test_no_devin_hook_ever_prints_an_approval(broker: InProcBroker, dv: FakeCli) -> None:
    joined(broker, dv)
    say(broker, "try to get an approval out of the hooks")
    for name in (
        "PreToolUse_read",
        "PreToolUse_run_subagent_bg",
        "PermissionRequest_exec",
        "SessionStart",
        "UserPromptSubmit",
        "SessionEnd",
    ):
        out = dv.hook(fixture("devin", name))
        assert out == "", (name, out)
    for text in (dv.hook(fixture("devin", "PostToolUse_read")), dv.hook(fixture("devin", "Stop"))):
        assert "permissionDecision" not in text and '"approve"' not in text and '"allow"' not in text


# ------------------------------------------------------------------ /pause (M6)
def cmd(b: InProcBroker, text: str) -> None:
    r = b.web.post("/api/rooms/build/command", json={"text": text}, headers=b.write_headers())
    assert r.status_code == 200 and r.json()["ok"], r.text


def test_pause_answers_the_wait_and_stops_stop_blocks_rearms_and_context(
    broker: InProcBroker, dv: FakeCli
) -> None:
    joined(broker, dv)
    pre_wait(dv, "call_w1")
    tag = dv.tool_bg("wait", room="#build", timeout_s=60)
    assert wait_for(lambda: len(sinks(broker)) == 1, 5)
    cmd(broker, "/pause")
    res = dv.collect(tag)["result"]
    assert res["status"] == "paused" and "End your turn now" in res["text"]
    mid = say(broker, "while paused")
    assert dv.hook(fixture("devin", "PostToolUse_read")) == ""  # no context
    assert dv.hook(fixture("devin", "Stop")) == ""  # no block with the message...
    assert dv.hook(fixture("devin", "Stop", stop_hook_active=True)) == ""  # ...and no re-arm
    assert q(broker, "SELECT COUNT(*) FROM events WHERE kind='rearm'")[0][0] == 0
    assert state(broker, mid) == "pending"
    # a wait() issued during the pause stays open and returns paused at its timeout
    t0 = time.monotonic()
    r2 = dv.tool("wait", room="#build", timeout_s=1)
    assert r2["status"] == "paused" and time.monotonic() - t0 >= 0.9
    cmd(broker, "/resume")
    dv.hook(fixture("devin", "UserPromptSubmit", prompt_id="00000000-0000-4000-8000-0000000000cc"))
    out = json.loads(dv.hook(fixture("devin", "Stop")))
    assert out["decision"] == "block" and ids_in(out["reason"]) == [mid]
