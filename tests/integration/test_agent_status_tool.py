"""A verified member sets one status line for its session in every joined room (#292)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent

from switchboard.config import Config
from switchboard.mcp.client import RpcError, Stream


@pytest.fixture
def broker(tmp_home: Path):
    b = InProcBroker(tmp_home, Config(human_name="alice")).start()
    web = b.web_client()
    for room in ("#build", "#docs"):
        assert web.post("/api/rooms", json={"name": room}, headers=b.write_headers()).status_code == 200
    b.web = web
    yield b
    web.close()
    b.stop()


def member(b: InProcBroker, room: str) -> dict[str, Any]:
    return b.web.get(f"/api/rooms/{room}/members").json()["members"][0]  # type: ignore[attr-defined]


async def test_status_is_one_bounded_line_across_a_sessions_rooms(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "status-session") as agent:
        await agent.join("#build", "worker")
        await agent.join("#docs", "writer")
        before = broker.web.get("/api/rooms/build/messages").json()["messages"]  # type: ignore[attr-defined]
        result = await agent.call("status", text="  CI 31/39\n green  ")
        assert result["status_line"] == "CI 31/39 green"
        for room in ("build", "docs"):
            row = member(broker, room)
            assert row["status_line"] == "CI 31/39 green"
            assert row["status_line_stale"] is False
            assert 0 < row["status_line_remaining_s"] <= 1800
        assert broker.web.get("/api/rooms/build/messages").json()["messages"] == before  # type: ignore[attr-defined]
        long = await agent.call("status", text="x" * 500)
        assert long["status_line"] == "x" * 160
        assert member(broker, "docs")["status_line"] == "x" * 160
        cleared = await agent.call("status", text="")
        assert cleared["status_line"] is None
        assert member(broker, "build")["status_line"] is None
        assert member(broker, "docs")["status_line"] is None


def test_status_text_that_isnt_a_string_is_a_bad_request(broker: InProcBroker) -> None:
    """The broker checks the text itself (the MCP tool's schema is the client's): a number or a
    list is refused as ``bad_request``. The code and the message were once passed the wrong way
    round, so the error's code was the sentence."""
    with Stream(broker.paths.sock, timeout=5) as s:
        s.call("mcp.hello", {"harness": "test", "test_session": "status-raw"}, 5)
        cred = s.call("agent.join", {"room": "#build", "screen_name": "worker"}, 5)["cred"]
        for bad in (7, ["CI green"], None):
            with pytest.raises(RpcError) as ei:
                s.call("agent.status", {"cred": cred, "text": bad}, 5)
            assert ei.value.code == "bad_request" and "status text" in ei.value.message, bad
        assert member(broker, "build")["status_line"] is None
