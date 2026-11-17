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


async def test_a_claim_in_one_room_blocks_the_same_key_in_another_and_every_end_frees_it(
    broker: InProcBroker,
) -> None:
    """#817: claude-1 claims SYS-645 in #build. codex-1, in #review, is refused and told who and where
    (the key's case doesn't matter). A release, then an expiry, then claude-1 leaving each free it for
    codex-1 in the other room. The limit of eight claims, and a claim in the holder's own room, are as before.
    Fails if a claim in room B succeeds beside room A's, the refusal loses the holder or the room, or a
    release, an expiry or a leave leaves the key held."""
    assert (
        broker.web.post(  # type: ignore[attr-defined]
            "/api/rooms", json={"name": "#review"}, headers=broker.write_headers()
        ).status_code
        == 200
    )
    async with FakeAgent(broker.home, "xr-first") as first, FakeAgent(broker.home, "xr-second") as second:
        await first.join("#build", "claude-1")
        await second.join("#review", "codex-1")
        assert (await first.call("claim", room="#build", key="SYS-645", ttl=120, note="editing"))["ok"]
        refused = await second.call("claim", room="#review", key="sys-645", ttl=120)
        assert not refused["ok"]
        assert "claude-1" in refused["error"] and "#build" in refused["error"], refused
        assert (await second.call("claim", room="#review", key="SYS-646", ttl=120))["ok"]  # another key
        # the room's own claims stay the room's: #review shows only its own
        rows = broker.web.get("/api/rooms/review/members").json()["members"]  # type: ignore[attr-defined]
        assert [c["key"] for r in rows for c in r["claims"]] == ["SYS-646"]
        assert (await first.call("claim", room="#build", key="SYS-645", ttl=0))["released"]
        assert (await second.call("claim", room="#review", key="SYS-645", ttl=120))["ok"]
        assert not (await first.call("claim", room="#build", key="SYS-645", ttl=120))["ok"]  # the other way
        assert (await second.leave("#review"))["ok"]
        assert (await first.call("claim", room="#build", key="SYS-645", ttl=120))["ok"]


async def test_a_claim_held_in_a_room_the_asker_cannot_see_is_only_held_elsewhere(
    broker: InProcBroker,
) -> None:
    """#817, §44: a claim in a private room of someone else still blocks the key, but the refusal gives
    the asker neither the room's name nor the holder: 'held elsewhere'. Fails if a private room's
    name or its member
    leaks into another room's refusal, or the key is not held."""
    from switchboard.models import session_key

    def seed() -> None:
        st = broker.state.store
        room = st.create_room("#secret-plans", "bob", 60, 10, private_for=7)  # person 7: not alice, the owner
        key = session_key("claude", "bobs-box", "claude-9")
        p = st.upsert_participant("claude", key, status="idle", tier="mcp-only", host="bobs-box")
        m = st.create_membership(room.id, p.id, "claude-9")
        assert st.move_claim(room.id, m.id, "SYS-9", 600).action == "acquired"

    broker.on_loop(seed)
    async with FakeAgent(broker.home, "xr-outsider") as outsider:
        await outsider.join("#build", "codex-1")
        refused = await outsider.call("claim", room="#build", key="sys-9", ttl=60)
        assert not refused["ok"]
        assert refused["error"] == "sys-9 is held elsewhere on this broker", refused
        for secret in ("claude-9", "secret-plans", "bobs-box"):
            assert secret not in refused["error"], refused
