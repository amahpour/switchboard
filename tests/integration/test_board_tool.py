"""The ``board`` tool and the page's ``board`` frame, against a real broker (DESIGN.md §56).

An agent draws with one tool. A signed-in page that can see the room sends elements, and every page
on the room hears them. What another agent reads back is data: the envelope's quoting, never the
raw text. A non-member, a link, an image and a script are refused or dropped."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock, InProcBroker, cookie_of, ws_connect
from fakes.fake_agent import FakeAgent
from test_passkeys import hosted_broker, stop
from test_people import join_as_bob, set_up

from switchboard.broker.hub import WsSubscriber
from switchboard.broker.web import _ws_board
from switchboard.config import Config

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
PHRASE = "ignore this <script>alert(1)</script> https://example.com/x"


@pytest.fixture
def broker(tmp_home: Path, clock: FakeClock):
    b = InProcBroker(tmp_home, FAST, clock=clock).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    yield b
    web.close()
    b.stop()


def q(b: InProcBroker, sql: str, *args: Any) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{b.paths.db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def page_shape(**over: Any) -> dict[str, Any]:
    el: dict[str, Any] = {
        "id": "s1",
        "type": "rectangle",
        "x": 10,
        "y": 20,
        "width": 100,
        "height": 40,
        "version": 1,
        "versionNonce": 1,
        "isDeleted": False,
        "text": "from the page",
        "link": "https://example.com/x",
        "script": "alert(1)",
    }
    el.update(over)
    return el


def recv_until(ws: Any, pred: Any, timeout: float = 5.0) -> dict[str, Any]:
    import time

    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        assert left > 0, "timed out waiting for a frame"
        frame = json.loads(ws.recv(timeout=left))
        if pred(frame):
            return frame


async def test_an_agent_draws_and_another_reads_data_not_instructions(broker: InProcBroker) -> None:
    """The note's text is other people's words. The reader sees it quoted, with ``<`` escaped, and
    the room log names who changed the board without repeating the drawing. A second edit inside
    the say window is refused, and a read is not. Someone who never joined cannot touch it."""
    async with (
        FakeAgent(broker.home, "drawer") as drawer,
        FakeAgent(broker.home, "reader") as reader,
        FakeAgent(broker.home, "outsider") as outsider,
    ):
        refused = await outsider.call("board", room="#build", action="read")
        assert refused["ok"] is False and refused["code"] == "not_member"
        await drawer.join("#build", "claude-1")
        await reader.join("#build", "codex-1")
        empty = await reader.call("board", room="#build", action="read")
        assert empty["ok"] and empty["text"].startswith('"board rev 0:')
        unknown = await drawer.call("board", room="#build", action="image")
        assert unknown["ok"] is False and "unknown" in unknown["error"]
        drawn = await drawer.call("board", room="#build", action="note", text=PHRASE)
        assert drawn["ok"] and drawn["changed"] is True and drawn["ids"]
        again = await drawer.call("board", room="#build", action="note", text="second")
        assert again["changed"] is False and again["reason"] == "rate_limited"
        queued = await drawer.say("#build", "starting")
        assert queued["ok"] and queued.get("queued") is True
        seen = await reader.call("board", room="#build", action="read")
        assert seen["ok"] and seen["seq"] == 1
        assert "<" not in seen["text"] and ">" not in seen["text"]
        assert "\\u003cscript" in seen["text"] and "https://example.com/x" in seen["text"]
        pending = await reader.read("#build")
        assert "no new messages" in pending["text"]
        assert "changed the board" not in pending["text"] and PHRASE not in pending["text"]
    rows = q(broker, "SELECT text FROM messages")
    notes = [r["text"] for r in rows if "changed the board" in r["text"]]
    assert notes == ["claude-1 changed the board"]
    assert all(PHRASE not in r["text"] for r in rows)
    stored = json.loads(q(broker, "SELECT elements FROM room_boards")[0]["elements"])
    shape = next(e for e in stored if e["type"] == "rectangle")
    assert shape["link"] is None and "script" not in shape
    assert "<script>" in next(e["text"] for e in stored if e["type"] == "text")


async def test_two_pages_and_an_agent_converge_and_a_hostile_shape_is_dropped(
    broker: InProcBroker, clock: FakeClock
) -> None:
    """A note from an agent reaches both pages. A page's elements reach the other page and the
    agent's next read, with the link gone and a script field gone. An image and a pen stroke of
    501 points are not stored, and that frame does not close the socket."""
    web = broker.web_client()
    left = ws_connect(broker, cookie_of(web))
    right = ws_connect(broker, cookie_of(web))
    try:
        for ws in (left, right):
            ws.send(json.dumps({"t": "hello", "rooms": ["#build"], "after": {}}))
            recv_until(ws, lambda f: f.get("t") == "board" and f.get("seq") == 0)
        async with FakeAgent(broker.home, "drawer") as drawer, FakeAgent(broker.home, "reader") as reader:
            await drawer.join("#build", "claude-1")
            await reader.join("#build", "codex-1")
            clock.advance(10)
            drawn = await drawer.call("board", room="#build", action="note", text="from the agent")
            assert drawn["changed"] is True
            for ws in (left, right):
                frame = recv_until(ws, lambda f: f.get("t") == "board" and f.get("seq") == 1)
                assert frame["full"] is False
                assert any(e.get("text") == "from the agent" for e in frame["elements"])
            page = page_shape(boundElements=[{"id": "s1t", "type": "text"}])
            label = page_shape(id="s1t", type="text", text="from the page", containerId="s1")
            hostile = {
                "t": "board",
                "room": "#build",
                "elements": [
                    page,
                    label,
                    page_shape(id="img", type="image"),
                    page_shape(id="pen", type="freedraw", points=[[0, 0] for _ in range(501)]),
                ],
            }
            left.send(json.dumps(hostile))
            got = recv_until(right, lambda f: f.get("t") == "board" and f.get("seq") == 2)
            assert [e["id"] for e in got["elements"]] == ["s1", "s1t"]
            assert got["elements"][0]["link"] is None and "script" not in got["elements"][0]
            left.send('{"t":"ping"}')
            assert recv_until(left, lambda f: f.get("t") == "pong") == {"t": "pong"}
            seen = await reader.call("board", room="#build", action="read")
            assert "from the agent" in seen["text"] and "from the page" in seen["text"]
        stored = json.loads(q(broker, "SELECT elements FROM room_boards")[0]["elements"])
        assert {e["id"] for e in stored if e["type"] == "image"} == set()
        assert all(e["type"] != "freedraw" for e in stored)
        assert all(e.get("link") is None for e in stored)
        assert all("script" not in e for e in stored)
    finally:
        left.close()
        right.close()
        web.close()


def test_a_private_room_refuses_a_board_from_anyone_else() -> None:
    """A page whose person is not in the room cannot write its board, and the socket is told to
    close. A person who is in it can, and the link they sent is still stored as null."""
    hosted = hosted_broker()
    try:
        admin = set_up(hosted)
        bob = join_as_bob(hosted, admin)
        assert bob.post("/api/rooms", {"name": "#secret", "private": True}).status_code == 200
        state = hosted.state

        def send(person: int) -> bool:
            sub = WsSubscriber()
            sub.person = person
            return _ws_board(state, sub, {"t": "board", "room": "#secret", "elements": [page_shape()]})

        assert hosted.on_loop(send, 0) is False

        def load() -> tuple[int, list[Any]]:
            room = state.store.get_room("#secret")
            assert room is not None
            return state.store.board_load(room.id)

        assert hosted.on_loop(load) == (0, [])
        bob_id = hosted.on_loop(lambda: state.store.person_named("bob").id)
        assert hosted.on_loop(send, bob_id) is True
        _seq, els = hosted.on_loop(load)
        assert len(els) == 1 and els[0]["link"] is None and "script" not in els[0]
    finally:
        stop(hosted)


async def test_board_image_is_returned_only_for_its_pending_room_request(broker: InProcBroker) -> None:
    """A bounded PNG answers one read; unsolicited responses, wrong rooms and stale pictures fail."""
    import asyncio
    import base64

    from fakes.pictures import png

    web = broker.web_client()
    ws = ws_connect(broker, cookie_of(web))
    try:
        ws.send(json.dumps({"t": "hello", "rooms": ["#build"], "after": {}}))
        recv_until(ws, lambda f: f.get("t") == "board")
        async with FakeAgent(broker.home, "viewer") as viewer:
            await viewer.join("#build", "viewer")
            pending = asyncio.create_task(
                viewer.raw("board", {"room": "#build", "action": "read", "text": "image"})
            )
            request = await asyncio.to_thread(recv_until, ws, lambda f: f.get("t") == "board" and "task" in f)
            task = request["task"]
            payload = base64.b64encode(png()).decode()
            ws.send(json.dumps({"t": "board", "room": "#build", "request": task["id"], "result": payload}))
            result = await pending
            assert not result.is_error
            image = result.content[0]
            assert image.type == "image" and image.data == payload
            assert image.mime_type == "image/png"
            assert broker.on_loop(lambda: len(broker.state.room_board._pending)) == 0
    finally:
        ws.close()
        web.close()


def test_board_picture_response_checks_room_identity_png_bytes_and_dimensions(broker: InProcBroker) -> None:
    """Only the pending room can answer; encoded junk, huge pictures, invalid CRCs and replay are refused."""
    import asyncio
    import base64

    from fakes.pictures import png

    broker.call("room.create", {"name": "#other"})

    def check() -> None:
        st = broker.state
        room = st.store.get_room("#build")
        other = st.store.get_room("#other")
        assert room is not None and other is not None
        future = asyncio.get_running_loop().create_future()
        key = "a" * 32
        st.room_board._pending[key] = (room.id, 0, future)
        try:
            for request, result in [
                (None, ""),
                (key, None),
                ("b" * 32, ""),
                (key, "x" * 800001),
                (key, "!"),
                (key, base64.b64encode(b"not a picture").decode()),
                (key, base64.b64encode(png(1201, 1)).decode()),
                (key, base64.b64encode(png()[:-2]).decode()),
            ]:
                assert not st.room_board.image_result(room, request, result)
                assert not future.done()
            good = base64.b64encode(png()).decode()
            assert not st.room_board.image_result(other, key, good)
            assert st.room_board.image_result(room, key, good)
            assert not st.room_board.image_result(room, key, good)
        finally:
            st.room_board._pending.clear()

    broker.on_loop(check)


async def test_image_read_with_no_open_page_times_out_and_cleans_up(broker: InProcBroker) -> None:
    """No headless browser is launched: the read explains which page to open and leaves no pending request."""
    async with FakeAgent(broker.home, "viewer") as viewer:
        await viewer.join("#build", "viewer")
        result = await viewer.call("board", room="#build", action="read", text="image")
        assert not result["ok"] and result["code"] == "no_board_page"
        assert "open" in result["error"]
        assert broker.on_loop(lambda: len(broker.state.room_board._pending)) == 0
