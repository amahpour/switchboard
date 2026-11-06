"""Both call sources use one checked posting path (DESIGN.md §52, §54, #623).

The existing server-bot integration tests stay unchanged. These add a path assertion and check the
stored provenance, which is deliberately invisible for the server's own lines.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from fakes.fake_joinly import FakeJoinly
from test_meeting_hosted import Host, Relay, host
from test_meetings import ZOOM, broker, command, joinly, meeting_lines, seg, wait_until  # noqa: F401

from switchboard.meeting import ingest


@pytest.mark.parametrize("source", ["server", "hosted"])
async def test_both_sources_check_and_store_the_line_through_the_same_ingest(
    broker: InProcBroker,  # noqa: F811
    joinly: FakeJoinly,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    """A name still wakes its agent; both sources reach ingest.receive and keep their call id.
    Fails when the bridge bypasses the shared entry point or drops the server call's provenance.
    """
    received: list[Any] = []
    receive = ingest.receive

    def record(*args: Any, **kw: Any) -> Any:
        received.append(args[1])
        return receive(*args, **kw)

    monkeypatch.setattr(ingest, "receive", record)
    agent = await FakeAgent(broker.home, "k1").start()
    owner = Host(broker)
    relay = Relay(broker)
    try:
        assert (await agent.join("#build", "codex-1"))["ok"]
        if source == "server":
            command(broker, f"/meeting {ZOOM}")
            await wait_until(lambda: bool(joinly.joins))
        else:
            assert (await owner.join("#build", "claude-1"))["ok"]
            assert (await host(owner))["ok"]
        waiting = agent.wait_task("#build", timeout_s=30)
        segments = [seg("Codex, check the upload limit?", 0, 1, "bob"), seg("thanks", 10, 11, "bob")]
        if source == "server":
            joinly.push(segments)
        else:
            response = relay.push(segments)
            assert response.status_code == 200
        await wait_until(lambda: bool(meeting_lines(broker)))
        got = await waiting
        assert got["ok"] and "to_you=yes" in got["text"] and "upload limit" in got["text"]
        assert len(received) >= 1 and received[0].source == source

        def stored() -> tuple[Any, Any]:
            room = broker.state.store.get_room("#build")
            assert room is not None
            row = broker.state.store.open_meeting(room.id)
            lines = [m for m in broker.state.store.history(room.id) if m.sender_kind == "meeting"]
            meeting_id = broker.state.store.con.execute(
                "SELECT meeting_id FROM messages WHERE id=?", (lines[0].id,)
            ).fetchone()[0]
            return (row, meeting_id), lines[0]

        (row, meeting_id), line = broker.on_loop(stored)
        assert meeting_id == row.id
        assert line.sender_host == ("alice-laptop" if source == "hosted" else None)
        assert line.text == "Codex, check the upload limit?" and line.sender_name == "bob"
        command(broker, "/meeting leave")
    finally:
        relay.close()
        await owner.close()
        await agent.close()
