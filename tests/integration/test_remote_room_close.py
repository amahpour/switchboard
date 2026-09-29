"""``/close`` with a remote (Pi) member over a real link (DESIGN.md §28.3, §28.8; spec #16 §14
item 15). The satellite and the Pi's MCP server are the real thing; the desktop broker runs
in-process."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fakes.fake_agent import FakeAgent
from fakes.fake_link import FakeLink, wait_for

CLOSED_WAIT = "[switchboard] #fpga was closed by alice; you are no longer in it."
CLOSED_CALL = "#fpga was closed by alice; you are no longer in it"


async def until(pred: Any, timeout: float = 10.0, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = await asyncio.to_thread(pred)
        if got:
            return got
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def close(link: FakeLink) -> dict[str, Any]:
    web = link.broker.web_client()
    try:
        r = web.post("/api/rooms/fpga/command", json={"text": "/close"}, headers=link.broker.write_headers())
        assert r.status_code == 200, r.text
        return r.json()
    finally:
        web.close()


def create(link: FakeLink, name: str) -> None:
    web = link.broker.web_client()
    try:
        r = web.post("/api/rooms", json={"name": name}, headers=link.broker.write_headers())
        assert r.status_code == 200, r.text
    finally:
        web.close()


def room_lines(link: FakeLink, room_id: int) -> list[Any]:
    b = link.broker
    return b.on_loop(lambda: b.state.store.history(room_id, None, 100))


def open_sinks(link: FakeLink) -> int:
    b = link.broker
    return b.on_loop(lambda: len(b.state.engine.sinks.open_sinks()))


async def test_close_with_a_remote_member_in_wait() -> None:
    with FakeLink(rooms=("*",), desk_rooms=("#fpga",), kind="inproc") as link:
        async with FakeAgent(link.pi, "bench") as a:
            assert (await a.join("#fpga", "bench"))["ok"]
            t = a.wait_task("#fpga", 30)
            await until(lambda: open_sinks(link) == 1, what="the remote wait")
            res = await asyncio.to_thread(close, link)
            assert res["text"].startswith("closed #fpga: 1 agent(s) removed (1 on fpga-pi); history kept.")
            r = await asyncio.wait_for(t, 10)
            assert r["status"] == "closed" and r["text"] == CLOSED_WAIT
            r = await a.say("#fpga", "hello?")
            assert r["ok"] is False and r["code"] == "unauthorized" and r["error"] == CLOSED_CALL
            [leave] = [m for m in room_lines(link, 1) if m.kind == "leave"]
            assert (leave.sender_name, leave.text, leave.sender_host) == ("bench", "left (#fpga closed)", "fpga-pi")
            await until(lambda: not link.status()["members"], what="no remote members")
            assert link.status()["state"] == "up"
        # a restart with the closed room in the database: the welcome leaves its name out
        await asyncio.to_thread(link.restart_broker)
        assert link.status()["state"] == "up"


async def test_member_offline_at_close_gets_the_error_after_relink() -> None:
    with FakeLink(rooms=("*",), desk_rooms=("#fpga",), kind="inproc") as link:
        async with FakeAgent(link.pi, "bench") as a:
            assert (await a.join("#fpga", "bench"))["ok"]
            await asyncio.to_thread(link.call, "remote.disable", {"name": link.name})
            await asyncio.to_thread(link.wait_state, "disabled", 20.0, "disabled")
            assert (await asyncio.to_thread(close, link))["ok"]
            await asyncio.to_thread(link.call, "remote.enable", {"name": link.name}, 30.0)
            await asyncio.to_thread(link.wait_state, "up")
            got: dict[str, Any] = {}

            async def closed_error() -> bool:
                got.update(await a.read("#fpga"))
                return got.get("code") == "unauthorized"

            deadline = time.monotonic() + 15
            while not await closed_error():  # the Pi's MCP server reconnects to the satellite
                assert time.monotonic() < deadline, got
                await asyncio.sleep(0.05)
            assert got["error"] == CLOSED_CALL


async def test_allowlisted_name_matches_a_recreated_room() -> None:
    with FakeLink(rooms=("#fpga",), kind="inproc") as link:
        async with FakeAgent(link.pi, "bench") as a:
            assert (await a.join("#fpga", "bench"))["ok"]
            assert (await asyncio.to_thread(close, link))["ok"]
            await asyncio.to_thread(create, link, "#fpga")
            r = await a.join("#fpga", "bench")
            assert r["ok"] and r["room"] == "#fpga"
            assert (await a.say("#fpga", "back"))["ok"]
            wait_for(lambda: [m["name"] for m in link.members()] == ["bench"], what="bench in the new #fpga")
            assert [m["name"] for m in link.members()] == ["bench"]
