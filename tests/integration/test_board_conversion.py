"""The browser is an untrusted converter, and membership/rate limits are checked after it replies (§56.9)."""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import InProcBroker, cookie_of, ws_connect
from fakes.fake_agent import FakeAgent
from test_board_tool import broker, page_shape, recv_until  # noqa: F401


@pytest.mark.parametrize("intervene", ["none", "say", "leave"])
async def test_mermaid_conversion_rechecks_membership_and_rate_limit_before_one_atomic_import(
    broker: InProcBroker,  # noqa: F811
    intervene: str,  # noqa: F811
):  # noqa: F811
    """Even a valid browser result cannot win a race with leaving or a post that used the shared limit."""
    web = broker.web_client()
    ws = ws_connect(broker, cookie_of(web))
    ws.send(json.dumps({"t": "hello", "rooms": ["#build"], "after": {}}))
    recv_until(ws, lambda f: f.get("t") == "board")
    try:
        async with FakeAgent(broker.home, "drawer") as writer:
            await writer.join("#build", "drawer")
            pending = asyncio.create_task(
                writer.call(
                    "board",
                    room="#build",
                    action="draw_mermaid",
                    text='flowchart LR\nA-->B\nclick A href "https://example.com"',
                )
            )
            frame = await asyncio.to_thread(recv_until, ws, lambda f: f.get("t") == "board" and "task" in f)
            task = frame["task"]
            assert task["kind"] == "mermaid" and "click" not in task["source"]
            # A wrong result kind and an image element cannot answer a conversion request.
            for result in ("not a picture", [page_shape(type="image")]):
                assert not broker.on_loop(
                    lambda result=result: broker.state.room_board.mermaid_result(
                        broker.state.store.get_room("#build"), task["id"], result
                    )
                )
            if intervene == "say":
                await writer.say("#build", "a post while converting")
            elif intervene == "leave":
                await writer.leave("#build")
            ws.send(
                json.dumps({"t": "board", "room": "#build", "request": task["id"], "result": [page_shape()]})
            )
            result = await pending
            if intervene == "none":
                assert result["changed"] and result["ids"][0] != "s1"
                saved = broker.on_loop(
                    lambda: broker.state.store.board_load(broker.state.store.get_room("#build").id)
                )[1]
                assert len(saved) == 1 and saved[0]["link"] is None and "script" not in saved[0]
            else:
                assert not result.get("changed")
                assert (
                    broker.on_loop(
                        lambda: broker.state.store.board_load(broker.state.store.get_room("#build").id)
                    )[1]
                    == []
                )
            assert broker.on_loop(lambda: len(broker.state.room_board._pending)) == 0
            assert broker.on_loop(lambda: len(broker.state.room_board._converting)) == 0
    finally:
        ws.close()
        web.close()


async def test_mermaid_conversion_error_is_bounded_and_pending_work_is_cleaned_up(broker: InProcBroker):  # noqa: F811
    """The browser may report one known failure, but arbitrary error text never becomes a tool response."""
    web = broker.web_client()
    ws = ws_connect(broker, cookie_of(web))
    ws.send(json.dumps({"t": "hello", "rooms": ["#build"], "after": {}}))
    recv_until(ws, lambda f: f.get("t") == "board")
    try:
        async with FakeAgent(broker.home, "drawer") as writer:
            await writer.join("#build", "drawer")
            invalid = await writer.call("board", room="#build", action="draw_mermaid", text="x" * 8001)
            assert not invalid["ok"] and invalid["code"] == "bad_request"
            pending = asyncio.create_task(
                writer.call("board", room="#build", action="draw_mermaid", text="flowchart LR\nA-->B")
            )
            frame = await asyncio.to_thread(recv_until, ws, lambda f: f.get("t") == "board" and "task" in f)
            ws.send(
                json.dumps(
                    {
                        "t": "board",
                        "room": "#build",
                        "request": frame["task"]["id"],
                        "result": {"error": "cannot_convert"},
                    }
                )
            )
            result = await pending
            assert not result["ok"] and result["code"] == "bad_request"
            assert broker.on_loop(lambda: len(broker.state.room_board._pending)) == 0
    finally:
        ws.close()
        web.close()


def test_a_diagram_that_only_partly_fits_never_writes_a_partial_import(broker: InProcBroker) -> None:  # noqa: F811
    """The normal merge is per-element; a fresh diagram must be all or nothing even at the board cap."""
    from switchboard.broker.service import ServiceError

    def check():
        st = broker.state
        room = st.store.get_room("#build")
        st.room_board.from_page(room, "alice", [page_shape(id=f"s{i}") for i in range(1499)])
        before = st.store.board_load(room.id)
        with pytest.raises(ServiceError, match="does not fit"):
            st.room_board.imported(room, "drawer", 1, [page_shape(id="a"), page_shape(id="b")])
        assert st.store.board_load(room.id) == before

    broker.on_loop(check)
