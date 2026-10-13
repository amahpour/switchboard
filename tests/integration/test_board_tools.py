"""Which members can use the review board, from the tools each MCP server lists in ``mcp.hello``
(#249, DESIGN.md §37.5): today's server lists ``review``; one from before the list says nothing
and counts as unable, and the member row says why while that process is connected. Through a
remote link too, since the satellite passes the hello on as it came."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from fakes.fake_link import FakeLink, wait_for

from switchboard import __version__
from switchboard.config import Config
from switchboard.mcp.client import Stream

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
OLD = (
    "codex-1 can't use the board: its switchboard is older than this one and doesn't list its"
    " tools. Upgrade it, then restart its session."
)


@pytest.fixture
def broker(tmp_home: Path):
    b = InProcBroker(tmp_home, FAST).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web
    yield b
    web.close()
    b.stop()


def members(b: InProcBroker) -> dict[str, dict[str, Any]]:
    rows = b.web.get("/api/rooms/build/members").json()["members"]  # type: ignore[attr-defined]
    return {m["name"]: m for m in rows}


async def test_a_member_from_before_the_tool_list_is_named(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        with Stream(broker.paths.sock, timeout=5) as old:
            # an MCP server from before #249: no version, no tools
            old.call("mcp.hello", {"harness": "test", "test_session": "old-1"}, 5)
            old.call("agent.join", {"room": "#build", "screen_name": "codex-1"}, 5)
            got = members(broker)
            assert got["claude-1"]["board_why"] is None  # today's server lists review
            assert got["codex-1"]["board_why"] == OLD
        # its connection went, and its claims with it: offline says the rest
        wait_for(lambda: members(broker)["codex-1"]["status"] == "offline", what="codex-1 offline")
        assert members(broker)["codex-1"]["board_why"] is None


async def test_a_list_without_review_names_what_is_missing(broker: InProcBroker) -> None:
    with Stream(broker.paths.sock, timeout=5) as s:
        hello = {"harness": "test", "test_session": "old-2", "version": "0.16.2", "tools": ["join", "say"]}
        s.call("mcp.hello", hello, 5)
        s.call("agent.join", {"room": "#build", "screen_name": "codex-1"}, 5)
        assert members(broker)["codex-1"]["board_why"] == (
            "codex-1 can't use the board: its switchboard (0.16.2) has no review tool. Upgrade it,"
            " then restart its session."
        )
        # a reconnect of the same process with today's list: the newer claim is the one that counts
        with Stream(broker.paths.sock, timeout=5) as again:
            again.call("mcp.hello", {**hello, "tools": ["join", "review", "say"]}, 5)
            assert members(broker)["codex-1"]["board_why"] is None


async def test_a_remote_members_list_arrives_through_the_link() -> None:
    with FakeLink(kind="inproc") as link:
        async with FakeAgent(link.pi, "bench") as a:
            await a.join("#fpga", "bench")
            assert next(m for m in link.members() if m["name"] == "bench")["board_why"] is None
            with Stream(link.pi_paths.sock) as old:
                old.call("mcp.hello", {"harness": "test", "test_session": "old-pi"})
                old.call("agent.join", {"room": "#fpga", "screen_name": "codex-1"})
                assert next(m for m in link.members() if m["name"] == "codex-1")["board_why"] == OLD


async def test_who_and_members_show_each_mcp_servers_bounded_claims(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "current") as current:
        await current.join("#build", "claude-1")
        with Stream(broker.paths.sock, timeout=5) as old:
            old.call(
                "mcp.hello",
                {
                    "harness": "test",
                    "test_session": "old-claims",
                    "version": "0.23.0",
                    "tools": ["join", "say", "say", "bad tool", "x" * 33],
                },
                5,
            )
            old.call("agent.join", {"room": "#build", "screen_name": "codex-1"}, 5)
            got = members(broker)
            assert got["claude-1"]["version_claim"] == __version__
            assert "review" in got["claude-1"]["tools_claim"]
            assert got["codex-1"]["version_claim"] == "0.23.0"
            assert got["codex-1"]["tools_claim"] == ["join", "say"]
            who = (await current.who("#build"))["text"]
            assert f"version_claim={__version__}" in who
            assert "version_claim=0.23.0 tools_claim=join,say" in who
        with Stream(broker.paths.sock, timeout=5) as unknown:
            unknown.call("mcp.hello", {"harness": "test", "test_session": "unknown-claims"}, 5)
            unknown.call("agent.join", {"room": "#build", "screen_name": "devin-1"}, 5)
            assert members(broker)["devin-1"]["version_claim"] == "unknown"
            assert members(broker)["devin-1"]["tools_claim"] is None
            assert "version_claim=unknown tools_claim=unknown" in (await current.who("#build"))["text"]


async def test_join_upgrade_note_is_once_for_behind_and_absent_for_current(broker: InProcBroker) -> None:
    with Stream(broker.paths.sock, timeout=5) as old:
        old.call(
            "mcp.hello",
            {
                "harness": "test",
                "test_session": "behind-note",
                "version": "0.23.9",
                "tools": [],
            },
            5,
        )
        first = old.call("agent.join", {"room": "#build", "screen_name": "codex-1"}, 5)
        assert first["room"] == "#build"
        old.call("agent.join", {"room": "#build", "screen_name": "codex-1"}, 5)
        history = broker.web.get("/api/rooms/build/messages").json()["messages"]  # type: ignore[attr-defined]
        notes = [m["text"] for m in history if "switchboard update" in m["text"]]
        assert notes == [
            "codex-1 reports switchboard 0.23.9; run switchboard update where it runs,"
            " then restart its session."
        ]
    async with FakeAgent(broker.home, "current-note") as current:
        await current.join("#build", "claude-1")
        history = broker.web.get("/api/rooms/build/messages").json()["messages"]  # type: ignore[attr-defined]
        assert len([m for m in history if "switchboard update" in m["text"]]) == 1
