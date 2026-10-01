"""A /dossier protocol follows ordinary human delivery through a real broker (DESIGN.md §33)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import InProcBroker
from fakes.fake_agent import FakeAgent, ids_in
from switchboard.config import Config

PR = "https://github.com/example/shop/pull/80"


@pytest.fixture
def broker(tmp_home: Path):
    cfg = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
    broker = InProcBroker(tmp_home, cfg).start()
    broker.web = broker.web_client()
    assert broker.web.post("/api/rooms", json={"name": "#review"},
                           headers=broker.write_headers()).status_code == 200
    yield broker
    broker.web.close()
    broker.stop()


def command(broker: InProcBroker, text: str) -> Any:
    return broker.web.post("/api/rooms/review/command", json={"text": text}, headers=broker.write_headers())


def messages(broker: InProcBroker) -> list[dict[str, Any]]:
    return [m for m in broker.web.get("/api/rooms/review/messages").json()["messages"] if m["kind"] == "chat"]


async def until_waiting(broker: InProcBroker, count: int) -> None:
    """Wait for registered sinks, not an assumed delay before a post."""
    deadline = time.monotonic() + 10
    while broker.on_loop(lambda: len(broker.state.engine.sinks.open_sinks())) != count:
        assert time.monotonic() < deadline, "the agents did not open their wait() calls"
        await asyncio.sleep(0.02)


async def test_protocol_reaches_all_reviewers_whole_through_wait(broker: InProcBroker) -> None:
    """A human broadcast reaches every member in full through wait(), including its final posting rules."""
    async with FakeAgent(broker.home, "author") as author, FakeAgent(broker.home, "reviewer") as reviewer:
        await author.join("#review", "author")
        await reviewer.join("#review", "reviewer")
        async with asyncio.TaskGroup() as group:
            first = group.create_task(author.wait("#review", 20))
            second = group.create_task(reviewer.wait("#review", 20))
            await until_waiting(broker, 2)
            response = command(broker, "/dossier " + PR)
            assert response.status_code == 200, response.text
            [message] = messages(broker)
            assert message["sender_kind"] == "human" and message["from"] == "alice"
            assert message["via"] == "web" and message["mentions"] == []
            for task, agent in ((first, author), (second, reviewer)):
                delivered = await asyncio.wait_for(task, 5)
                assert ids_in(delivered["text"]) == [message["id"]]
                assert "prio=human" in delivered["text"] and "Nothing posts twice" in delivered["text"]
                assert "## Left out" in delivered["text"] and PR in delivered["text"]
                assert "read() shows full" not in delivered["text"]
                assert (await agent.pass_("#review"))["ok"]


@pytest.mark.parametrize("gate,release", [("/hold reviewer", "/release reviewer"), ("/pause", "/resume")])
async def test_protocol_waits_for_hold_or_pause_without_releasing_it(
    broker: InProcBroker, gate: str, release: str,
) -> None:
    """Protocol delivery must respect the same holds and pauses as any human request."""
    async with FakeAgent(broker.home, "reviewer") as reviewer:
        await reviewer.join("#review", "reviewer")
        assert command(broker, gate).status_code == 200
        async with asyncio.TaskGroup() as group:
            waiting = group.create_task(reviewer.wait("#review", 20))
            await until_waiting(broker, 1)
            assert command(broker, "/dossier " + PR).status_code == 200
            [message] = messages(broker)
            assert not waiting.done()
            assert broker.on_loop(lambda: len(broker.state.engine.sinks.open_sinks())) == 1
            assert command(broker, release).status_code == 200
            delivered = await asyncio.wait_for(waiting, 5)
            assert ids_in(delivered["text"]) == [message["id"]]


async def test_agent_slash_text_is_literal_and_does_not_start_a_dossier(broker: InProcBroker) -> None:
    """An agent can discuss /dossier, but cannot run a human command or manufacture its human sender."""
    async with FakeAgent(broker.home, "reviewer") as reviewer:
        await reviewer.join("#review", "reviewer")
        assert (await reviewer.say("#review", "/dossier " + PR))["ok"]
        [message] = messages(broker)
        assert message["sender_kind"] == "agent" and message["text"] == "/dossier " + PR
        assert broker.on_loop(lambda: broker.state.store.recent_events(kinds=["dossier"])) == []
        tools = await reviewer.list_tools()
        assert "dossier" not in {tool.name for tool in tools}
        # The human command still works afterwards; the agent's message was not a protocol.
        assert command(broker, "/dossier " + PR).status_code == 200
        assert len(messages(broker)) == 2 and messages(broker)[1]["sender_kind"] == "human"
