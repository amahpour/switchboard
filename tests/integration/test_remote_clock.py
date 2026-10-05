"""A remote host's clock never reaches delivery logic (DESIGN.md §27.4.6): only ages cross
the link. The satellite runs an hour ahead or behind (``SWITCHBOARD_TEST_CLOCK_SKEW``,
applied to its own clock and to the timestamps of its host's clients, as on a remote
with a wrong clock); everything works as without skew, and one notice says so."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from typing import Any

import pytest
from fakes.fake_agent import ids_in
from fakes.fake_cli import FakeCli, fixture
from fakes.fake_link import PID_SHIFT, FakeLink, wait_for

from switchboard.config import Config

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
WAIT = "mcp__switchboard__wait"


def q(link: FakeLink, sql: str, *args: Any) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{link.desk_paths.db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def say(link: FakeLink, text: str) -> int:
    return link.call("human.say", {"room": "#fpga", "text": text})["id"]


def inject_hook(link: FakeLink, dv: FakeCli, params: dict[str, Any]) -> dict[str, Any]:
    """A hook.event frame for the Devin session, straight into the live link's handler."""
    rl = link.broker.state.remotes.links[link.name]
    a = rl.attempt
    got: list[dict[str, Any]] = []
    real = rl.send_frame
    p = q(link, "SELECT agent_pid, agent_start FROM participants WHERE harness='devin'")[0]
    chain = [(1000009999, 1.0, "-"), (p["agent_pid"], p["agent_start"], "devin")]

    def capture(att: Any, frame: dict[str, Any]) -> None:
        if frame.get("t") == "out" and frame.get("c") == 555:
            got.append(frame["line"])
        real(att, frame)

    async def go() -> None:
        rl.send_frame = capture  # type: ignore[method-assign]
        try:
            await rl._on_frame(a, {"t": "open", "c": 555})
            await rl._on_frame(
                a,
                {
                    "t": "req",
                    "c": 555,
                    "facts": {"chain": chain},
                    "line": {"id": 1, "method": "hook.event", "params": {"harness": "devin", **params}},
                },
            )
            await asyncio.sleep(0.1)
            await rl._on_frame(a, {"t": "close", "c": 555})
        finally:
            rl.send_frame = real  # type: ignore[method-assign]

    asyncio.run_coroutine_threadsafe(go(), link.broker.loop).result(10)
    return got[0]


@pytest.mark.parametrize("skew", ["+3600", "-3600"])
def test_pi_clock_skew(skew: str) -> None:
    link = FakeLink(kind="inproc", broker_cfg=FAST, env={"SWITCHBOARD_TEST_CLOCK_SKEW": skew})
    dv = None
    try:
        link.start()
        st = link.status()
        assert abs(st["skew_s"] - float(skew)) < 5
        dv = FakeCli(None, "devin", home=link.pi)
        assert dv.tool("join", room="#fpga", screen_name="devin-pi")["ok"]
        dv.hook(fixture("devin", "SessionStart"))
        dv.hook(fixture("devin", "UserPromptSubmit"))
        part = lambda: q(link, "SELECT * FROM participants WHERE harness='devin'")[0]  # noqa: E731
        assert part()["status"] == "busy" and part()["agent_pid"] > PID_SHIFT

        # 1. an orphaned wait(): the next hook of the session (started after the wait opened) ends it
        first = dv.tool_bg("wait", room="#fpga", timeout_s=60)
        wait_for(lambda: len(link.broker.state.engine.sinks.open_sinks()) == 1, what="wait open")
        dv.hook(fixture("devin", "PreToolUse_read"))  # the agent moved on (an interrupt sends no cancel)
        res = dv.collect(first, timeout=10)["result"]
        assert res["status"] == "superseded", res

        # 2. a wait() answer, confirmed by that call's own PostToolUse: its turn start is broker time
        dv.hook(
            fixture(
                "devin",
                "PreToolUse_read",
                tool_name=WAIT,
                tool_input={"room": "#fpga"},
                tool_use_id="call_w2",
            )
        )
        tag = dv.tool_bg("wait", room="#fpga", timeout_s=60)
        wait_for(lambda: len(link.broker.state.engine.sinks.open_sinks()) == 1, what="wait open again")
        mid = say(link, "flash it")
        res = dv.collect(tag)["result"]
        assert res["status"] == "messages" and ids_in(res["text"]) == [mid]
        bid = res["batch_id"]
        dv.hook(
            fixture(
                "devin",
                "PostToolUse_mcp_wait",
                tool_name=WAIT,
                tool_use_id="call_w2",
                tool_input={"room": "#fpga", "timeout_s": 60},
                tool_response={"success": True, "output": json.dumps(res), "error": None},
            )
        )
        now = time.time()
        b = wait_for(
            lambda: (x := q(link, "SELECT * FROM batches WHERE id=?", bid)[0])["state"] == "confirmed" and x,
            what="confirmed",
        )
        assert abs(b["turn_start_at"] - now) < 0.5, (b["turn_start_at"], now)

        # 3. no early pull-batch expiry: a Stop that started before a read() answer was made
        # leaves it offered; the Pi's wall clock (any "t" it sends) is never looked at
        dv.hook(fixture("devin", "UserPromptSubmit", prompt_id="p-2"))
        say(link, "one more")
        r = dv.tool("read", room="#fpga")
        rb = r["batch_id"]
        time.sleep(0.3)
        out = inject_hook(link, dv, {"event": "Stop", "t_age": 5.0, "t": time.time() + 3600})
        assert out == {"id": 1, "result": {"out": None}} or out.get("result") is not None
        assert q(link, "SELECT state FROM batches WHERE id=?", rb)[0][0] == "offered"
        inject_hook(link, dv, {"event": "Stop", "t_age": 0.0, "t": time.time() - 3600})
        assert (
            q(link, "SELECT state FROM batches WHERE id=?", rb)[0][0] == "expired"
        )  # control: ended after it

        # 4. exactly one notice about the clock, and the link stays up
        notes = [t for t in link.notices() if "clock is" in t]
        want = "ahead" if skew.startswith("+") else "behind"
        assert len(notes) == 1 and f"3600 s {want}" in notes[0] and "delivery is unaffected" in notes[0]
        assert link.status()["state"] == "up"
    finally:
        if dv is not None:
            dv.close()
        link.close()
