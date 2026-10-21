"""A verified member sets one status line for its session in every joined room (#292)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent

from switchboard.config import Config


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
    """The service checks the text itself (the MCP tool's schema is the client's): a number or a
    list is refused as ``bad_request``. The code and the message were once passed the wrong way
    round, so the error's code was the sentence."""
    from switchboard.broker.agent_http import HttpConn
    from switchboard.broker.agents import AgentConn
    from switchboard.broker.service import ServiceError

    conn = HttpConn(1, "", AgentConn("test", "", test_session="status-raw"))
    agents = broker.state.agents
    broker.on_loop(agents.join, conn, {"room": "#build", "screen_name": "worker"})
    mid = broker.on_loop(
        lambda: broker.state.store.find_member(broker.state.store.get_room("#build").id, "worker")
    )
    for bad in (7, ["CI green"], None):
        with pytest.raises(ServiceError) as ei:
            broker.on_loop(agents.status, conn, {"cred": f"m:{mid.membership_id}", "text": bad})
        assert ei.value.code == "bad_request" and "status text" in str(ei.value), bad
    assert member(broker, "build")["status_line"] is None
