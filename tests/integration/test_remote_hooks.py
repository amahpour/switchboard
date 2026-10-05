"""Hooks on a remote host resolve on that host (DESIGN.md §27.5.5), and the hook-reached
harnesses work over the link: the real hook script run by stand-in harness processes on
the Pi home, their MCP servers and hooks dialing the satellite; the recorded Cursor and
Devin contract payloads replayed through it."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import time
from typing import Any

import pytest
from fakes.fake_agent import FakeAgent, ids_in
from fakes.fake_claude import FakeClaude
from fakes.fake_claude import fixture as claude_fixture
from fakes.fake_cli import CONV, DEVIN_SID, FakeCli, fixture
from fakes.fake_link import PID_SHIFT, FakeLink, wait_for

from switchboard.config import Config

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
PARK_WAIT = 38
WAIT = "mcp__switchboard__wait"


@pytest.fixture
def link() -> Any:
    lk = FakeLink(kind="inproc", broker_cfg=FAST)
    try:
        lk.start()
        yield lk
    finally:
        lk.close()


def say(link: FakeLink, text: str) -> int:
    return link.call("human.say", {"room": "#fpga", "text": text})["id"]


def q(link: FakeLink, sql: str, *args: Any) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{link.desk_paths.db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def part(link: FakeLink, harness: str) -> sqlite3.Row:
    return q(link, "SELECT * FROM participants WHERE harness=? ORDER BY id", harness)[-1]


def member(link: FakeLink, name: str) -> dict[str, Any]:
    return next(m for m in link.members() if m["name"] == name)


def engine(link: FakeLink) -> Any:
    return link.broker.state.engine


# ------------------------------------------------------------------- Claude
def test_pi_hooks_resolve_to_pi_member(link: FakeLink) -> None:
    fc = FakeClaude(None, home=link.pi, sessions_dir=link.pi_sessions)
    try:
        j = fc.tool("join", room="#fpga", screen_name="bench")
        # verified on the Pi; this stand-in serves no inbox socket, so its MCP server's guard
        # never attaches: claude:hook (the inbox over the link: test_remote_claude.py)
        assert j["ok"] and j["tier"] == "claude:hook", j
        p = part(link, "claude")
        assert p["host"] == "fpga-pi" and p["agent_pid"] == fc.pid + PID_SHIFT
        assert p["session_key"].startswith(f"claude@fpga-pi:{fc.pid + PID_SHIFT}@")
        assert p["claude_socket"] == fc.inbox_path
        # the broker's own registry dir holds nothing: only the Pi's registry vouched for it
        reg_dir = link.desk / "claude-sessions"
        assert not reg_dir.exists() or not list(reg_dir.glob("*.json"))
        m = member(link, "bench")
        assert (m["harness"], m["tier"], m["host"]) == ("claude", "claude:hook", "fpga-pi")
        assert fc.hook(claude_fixture("UserPromptSubmit")) == ""
        assert part(link, "claude")["status"] == "busy" and part(link, "claude")["hooks_seen_at"] is not None
        mid = say(link, "while you work")
        out = fc.hook(claude_fixture("PostToolUse_bash"))
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert ids_in(ctx) == [mid] and "while you work" in ctx
        fc.hook(claude_fixture("Stop"))
        assert part(link, "claude")["status"] == "idle"
        # a remote Claude without an inbox listens with wait(), as claude:hook does locally
        assert "wait(" in j["text"]
    finally:
        fc.close()


def test_chain_naming_a_desktop_pid_is_inert(link: FakeLink) -> None:
    async def go() -> None:
        async with FakeAgent(link.desk, "vivado") as local:
            await local.join("#fpga", "vivado")
            lp = part(link, "test")
            rl = link.broker.state.remotes.links[link.name]
            a = rl.attempt
            got: list[dict[str, Any]] = []
            real = rl.send_frame

            def capture(att: Any, frame: dict[str, Any]) -> None:
                if frame.get("t") == "out" and frame.get("c") == 777:
                    got.append(frame["line"])
                real(att, frame)

            # a (forged) chain from the Pi that names the desktop member's own agent process
            chain = [[1000009999, 1.0, "-"], [lp["agent_pid"], lp["agent_start"], "-"]]
            ev = {"harness": "test", "event": "UserPromptSubmit", "t_age": 0.1}

            async def inject() -> None:
                rl.send_frame = capture  # type: ignore[method-assign]
                try:
                    await rl._on_frame(a, {"t": "open", "c": 777})
                    await rl._on_frame(
                        a,
                        {
                            "t": "req",
                            "c": 777,
                            "facts": {"chain": [tuple(x) for x in chain]},
                            "line": {"id": 1, "method": "hook.event", "params": ev},
                        },
                    )
                    await asyncio.sleep(0.2)
                    await rl._on_frame(a, {"t": "close", "c": 777})
                finally:
                    rl.send_frame = real  # type: ignore[method-assign]

            before = part(link, "test")["hooks_seen_at"]
            asyncio.run_coroutine_threadsafe(inject(), link.broker.loop).result(10)
            assert got == [{"id": 1, "result": {"out": None}}]
            assert part(link, "test")["hooks_seen_at"] == before  # the desktop member was never touched

    asyncio.run(go())


# ------------------------------------------------------------------- Cursor
def join_payload(j: dict[str, Any], conv: str = CONV) -> dict[str, Any]:
    out = json.dumps({"content": [{"type": "text", "text": json.dumps(j)}], "isError": False})
    return fixture(
        "cursor",
        "postToolUse_mcp",
        conv=conv,
        tool_name="MCP:join",
        tool_input={"room": "#fpga", "screen_name": "cursor-pi"},
        tool_output=out,
    )


def cursor_bound(link: FakeLink) -> FakeCli:
    cur = FakeCli(None, "cursor", home=link.pi)
    j = cur.tool("join", room="#fpga", screen_name="cursor-pi")
    assert j["ok"], j
    p = part(link, "cursor")
    assert p["bind_state"] == "pending" and p["session_key"].startswith("cursor@fpga-pi:agent:")
    assert cur.hook(join_payload(j)) == ""
    p = part(link, "cursor")
    assert p["bind_state"] == "bound" and p["session_key"] == f"cursor@fpga-pi:{CONV}", dict(p)
    return cur


def test_cursor_join_nonce_bind_over_link(link: FakeLink) -> None:
    cur = cursor_bound(link)
    try:
        m = member(link, "cursor-pi")
        assert m["tier"] == "cursor:stop-park" and m["host"] == "fpga-pi"
        # the same conversation id can't be bound on another host (here: this machine)
        local = FakeCli(link.broker, "cursor")
        try:
            j = local.tool("join", room="#fpga", screen_name="cursor-desk")
            assert j["ok"]
            local.hook(join_payload(j))
            p = q(link, "SELECT * FROM participants WHERE harness='cursor' AND host=''")[0]
            assert p["bind_state"] == "pending"
            assert any("joined from another machine" in t for t in link.notices())
        finally:
            local.close()
    finally:
        cur.close()


def _hook_pids(root: int) -> list[int]:
    out = subprocess.run(["/bin/ps", "-A", "-o", "pid=,ppid=,args="], capture_output=True, text=True).stdout
    procs = [
        (int(a), int(b), c)
        for a, b, c in (
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


def test_cursor_stop_park_over_link_ends_with_its_connection(link: FakeLink) -> None:
    cur = cursor_bound(link)
    try:
        # a park filled by a message becomes the follow-up
        tag = cur.hook_bg(fixture("cursor", "stop", status="completed", loop_count=0), max_wait=PARK_WAIT)
        wait_for(lambda: len(engine(link).sinks.parks()) == 1, what="parked")
        mid = say(link, "please flash it")
        out = json.loads(cur.collect(tag)["stdout"])
        assert ids_in(out["followup_message"]) == [mid]
        cur.hook(fixture("cursor", "postToolUse_shell"))
        assert cur.tool("pass", room="#fpga")["ok"]  # answered: the next stop parks again
        # a park whose hook dies ends with the hook's connection to the satellite
        tag = cur.hook_bg(fixture("cursor", "stop", status="completed", loop_count=1), max_wait=PARK_WAIT)
        wait_for(lambda: len(engine(link).sinks.parks()) == 1, what="parked again")
        pids = wait_for(lambda: _hook_pids(cur.pid), what="the stop hook")
        for pid in pids:
            os.kill(pid, signal.SIGKILL)
        wait_for(lambda: engine(link).sinks.parks() == [], what="the park ended")
        cur.collect(tag, timeout=10)
        # and a park ends with the link too
        tag = cur.hook_bg(fixture("cursor", "stop", status="completed", loop_count=0), max_wait=PARK_WAIT)
        wait_for(lambda: len(engine(link).sinks.parks()) == 1, what="parked a third time")
        pid = link.satellite_pid()
        assert pid
        os.kill(pid, signal.SIGKILL)
        wait_for(lambda: engine(link).sinks.parks() == [], what="the park ended with the link")
        assert cur.collect(tag, timeout=15) == {"rc": 0, "stdout": ""}
    finally:
        cur.close()


# -------------------------------------------------------------------- Devin
def test_devin_wait_loop_over_link(link: FakeLink) -> None:
    dv = FakeCli(None, "devin", home=link.pi)
    try:
        j = dv.tool("join", room="#fpga", screen_name="devin-pi")
        assert j["ok"] and j["tier"] == "devin:wait-loop", j
        dv.hook(fixture("devin", "SessionStart"))
        p = part(link, "devin")
        assert p["status"] == "idle" and p["session_id"] == DEVIN_SID and p["host"] == "fpga-pi"
        dv.hook(fixture("devin", "UserPromptSubmit"))
        assert part(link, "devin")["status"] == "busy"
        dv.hook(
            fixture(
                "devin",
                "PreToolUse_read",
                tool_name=WAIT,
                tool_input={"room": "#fpga"},
                tool_use_id="call_w1",
            )
        )
        tag = dv.tool_bg("wait", room="#fpga", timeout_s=60)
        wait_for(lambda: len(engine(link).sinks.open_sinks()) == 1, what="the wait is open")
        t0 = time.monotonic()
        mid = say(link, "flash top.bit please")
        res = dv.collect(tag)["result"]
        assert res["status"] == "messages" and ids_in(res["text"]) == [mid], res
        lat = time.monotonic() - t0
        bid = res["batch_id"]
        dv.hook(
            fixture(
                "devin",
                "PostToolUse_mcp_wait",
                tool_name=WAIT,
                tool_use_id="call_w1",
                tool_input={"room": "#fpga", "timeout_s": 60},
                tool_response={"success": True, "output": json.dumps(res), "error": None},
            )
        )
        b = wait_for(
            lambda: (x := q(link, "SELECT * FROM batches WHERE id=?", bid)[0])["state"] == "confirmed" and x,
            what="confirmed",
        )
        assert b["turn_start_at"] is not None
        state = q(link, "SELECT state FROM deliveries WHERE message_id=?", mid)[0][0]
        assert state == "in_context"
        print(f"devin wait over the link: post to wait() result {lat * 1000:.0f} ms")
    finally:
        dv.close()
