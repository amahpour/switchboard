"""The advisory claim tool through real MCP clients and a broker (#293)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent

from switchboard.config import Config


@pytest.fixture
def broker(tmp_home: Path):
    b = InProcBroker(tmp_home, Config(human_name="alice")).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web
    yield b
    web.close()
    b.stop()


async def test_duplicate_claim_names_holder_and_leave_frees_the_key(broker: InProcBroker) -> None:
    """A competing agent is refused until release or the first membership ends."""
    async with (
        FakeAgent(broker.home, "claim-first") as first,
        FakeAgent(broker.home, "claim-second") as second,
    ):
        await first.join("#build", "claude-1")
        await second.join("#build", "codex-1")
        acquired = await first.call("claim", room="#build", key="build", ttl=120)
        assert acquired["ok"] and acquired["key"] == "build" and acquired["holder"] == "claude-1"
        refused = await second.call("claim", room="#build", key="build", ttl=120)
        assert not refused["ok"] and "claude-1" in refused["error"]
        rows = broker.web.get("/api/rooms/build/members").json()["members"]  # type: ignore[attr-defined]
        assert [r["claims"] for r in rows if r["name"] == "claude-1"][0][0]["key"] == "build"
        released = await first.call("claim", room="#build", key="build", ttl=0)
        assert released["ok"] and released["released"]
        assert (await second.call("claim", room="#build", key="build", ttl=120))["ok"]
        assert (await second.leave("#build"))["ok"]
        assert (await first.call("claim", room="#build", key="build", ttl=120))["ok"]
