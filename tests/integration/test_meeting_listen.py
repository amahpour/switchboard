"""The real command and agent HTTPS path keep listening under a person's control (§52.1)."""

from __future__ import annotations

from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from fakes.fake_joinly import FakeJoinly
from test_meetings import ZOOM, command, notices, wait_until
from test_meetings import broker as broker
from test_meetings import joinly as joinly


async def test_web_listen_notice_literal_agent_command_and_call_end(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """An agent cannot switch itself on; the web can, and ending the real bridge clears it."""
    async with FakeAgent(broker.home, "k1") as agent:
        assert (await agent.join("#build", "alpha"))["ok"]
        command(broker, f"/meeting {ZOOM}")
        await wait_until(lambda: len(joinly.joins) == 1)
        assert (await agent.say("#build", "/meeting listen alpha"))["ok"]
        members = broker.web.get("/api/rooms/build/members").json()["members"]
        assert members[0]["listen"] is False
        command(broker, "/meeting listen alpha")
        members = broker.web.get("/api/rooms/build/members").json()["members"]
        assert members[0]["listen"] is True
        assert "started alpha listening" in notices(broker)
        assert "approval mode is unknown" in notices(broker)
        command(broker, "/meeting leave")
        await wait_until(lambda: bool(joinly.leaves))
        await wait_until(
            lambda: not broker.web.get("/api/rooms/build/members").json()["members"][0]["listen"]
        )
