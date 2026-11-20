"""A secret on the whiteboard reaches no file of the broker's home (DESIGN.md §11 item 10, §56, #893).

An in-process broker, a scripted agent that is a real MCP client of ``/mcp`` and a signed-in page's socket.
What these catch: a secret drawn by an agent's ``board`` tool, a Mermaid diagram or a page that is stored
(the database and its write-ahead log are searched byte by byte), shown to the room (a read, a WebSocket
frame) or echoed in the answer. Every secret is built from pieces, so no scanner reads this file as a leak."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock, InProcBroker, cookie_of, ws_connect
from fakes.fake_agent import FakeAgent
from test_board_tool import page_shape, recv_until
from test_mcp_agent import FAST

TOKEN = "ghp" + "_" + "aB3" * 12
PASSWORD_VALUE = "hunter2" * 2
GH = "[redacted: github token]"


@pytest.fixture
def broker(tmp_home: Path, clock: FakeClock) -> Iterator[InProcBroker]:
    b = InProcBroker(tmp_home, FAST, clock=clock).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    yield b
    web.close()
    b.stop()


def no_secret_on_disk(b: InProcBroker) -> None:
    """Neither the secret's bytes nor its password are in any file under the home: the database, its
    write-ahead log and anything else."""
    found = [
        str(f)
        for f in b.home.rglob("*")
        if f.is_file() and any(s.encode() in f.read_bytes() for s in (TOKEN, PASSWORD_VALUE))
    ]
    assert found == []


async def test_an_agents_drawing_never_holds_a_secret_in_the_database_or_a_read(
    broker: InProcBroker, clock: FakeClock
) -> None:
    """Every action an agent draws with, and a Mermaid diagram, with a secret in each text it takes. Fails if
    a text is stored as written: the row, a reader's ``read`` and the answer of the call would hold it."""
    async with FakeAgent(broker.home, "drawer") as drawer, FakeAgent(broker.home, "reader") as reader:
        await drawer.join("#build", "claude-1")
        await reader.join("#build", "codex-1")
        words = f"key {TOKEN} password={PASSWORD_VALUE}"
        replies = []
        for action, extra in (
            ("note", {}),
            ("box", {"x": 400}),
            ("text", {"y": 300}),
            ("frame", {"y": 600}),
        ):
            clock.advance(10)
            replies.append(await drawer.call("board", room="#build", action=action, text=words, **extra))
        shapes = [r["ids"][0] for r in replies[:2]]
        clock.advance(10)
        replies.append(
            await drawer.call(
                "board", room="#build", action="connect", id=shapes[0], to=shapes[1], text=words
            )
        )
        clock.advance(10)
        replies.append(
            await drawer.call("board", room="#build", action="edit", id=shapes[0], text=f"again {words}")
        )
        clock.advance(10)
        source = f'flowchart LR\nA["node {TOKEN}"] -->|"edge password={PASSWORD_VALUE}"| B[plain]'
        replies.append(await drawer.call("board", room="#build", action="draw_mermaid", text=source))
        for r in replies:
            assert r["ok"] and r["changed"] is True, (replies.index(r), r)
        seen = await reader.call("board", room="#build", action="read")
    assert TOKEN not in json.dumps(replies) + seen["text"] and PASSWORD_VALUE not in seen["text"]
    assert GH in seen["text"]
    no_secret_on_disk(broker)
    room_board = broker.on_loop(
        lambda: broker.state.store.board_load(broker.state.store.get_room("#build").id)
    )
    assert GH in json.dumps(room_board[1])


async def test_a_value_its_marker_would_make_too_long_is_refused_and_nothing_is_stored(
    broker: InProcBroker,
) -> None:
    """The refusal names the kind and never the value, and the board stays empty."""
    from switchboard import board

    value = "x" * (board.MAX_TEXT - len("password=" + PASSWORD_VALUE) - 1) + " password=" + PASSWORD_VALUE
    async with FakeAgent(broker.home, "drawer") as drawer:
        await drawer.join("#build", "claude-1")
        refused = await drawer.call("board", room="#build", action="note", text=value)
    assert refused["ok"] is False and "password or token" in refused["error"]
    assert PASSWORD_VALUE not in json.dumps(refused)
    no_secret_on_disk(broker)


async def test_a_page_that_sends_a_secret_stores_and_broadcasts_a_marker(broker: InProcBroker) -> None:
    """The signed-in page's ``board`` frame goes through the same reader: the text, the original text and a
    name are cut, a second page hears the marker, and a ``customData`` field is not stored."""
    web = broker.web_client()
    left, right = ws_connect(broker, cookie_of(web)), ws_connect(broker, cookie_of(web))
    try:
        for ws in (left, right):
            ws.send(json.dumps({"t": "hello", "rooms": ["#build"], "after": {}}))
            recv_until(ws, lambda f: f.get("t") == "board" and f.get("seq") == 0)
        shape = page_shape(
            text=TOKEN,
            originalText=TOKEN,
            name=f"password={PASSWORD_VALUE}",
            customData={"note": TOKEN},
        )
        label = page_shape(id="s1t", type="text", text=TOKEN, originalText=TOKEN, containerId="s1")
        left.send(json.dumps({"t": "board", "room": "#build", "elements": [shape, label]}))
        frame: dict[str, Any] = recv_until(right, lambda f: f.get("t") == "board" and f.get("seq") == 1)
        assert TOKEN not in json.dumps(frame) and PASSWORD_VALUE not in json.dumps(frame)
        assert [e["text"] for e in frame["elements"]] == [GH, GH]
        assert all("customData" not in e for e in frame["elements"])
    finally:
        left.close()
        right.close()
        web.close()
    no_secret_on_disk(broker)


def test_a_page_whose_element_was_refused_is_sent_the_committed_board(broker: InProcBroker) -> None:
    """The page keeps its own copy of what it drew until the broker answers. An element the broker refuses
    (here: text its marker would make too long) is never echoed, so without an answer the tab would keep the
    refused copy for good (#893). The sender is sent the whole board, and its own copy then gives way (no
    id the page supplied is sent back). Fails if no ``full`` frame comes back."""
    from switchboard import board

    web = broker.web_client()
    ws = ws_connect(broker, cookie_of(web))
    try:
        ws.send(json.dumps({"t": "hello", "rooms": ["#build"], "after": {}}))
        recv_until(ws, lambda f: f.get("t") == "board" and f.get("seq") == 0)
        value = "x" * (board.MAX_TEXT - len("password=" + PASSWORD_VALUE) - 1) + " password=" + PASSWORD_VALUE
        ws.send(json.dumps({"t": "board", "room": "#build", "elements": [page_shape(text=value)]}))
        full = recv_until(ws, lambda f: f.get("t") == "board" and f.get("full") is True and f.get("seq") == 0)
        assert full["elements"] == []
        assert "refused" not in full and "s1" not in json.dumps(full)  # no page-supplied id is sent back
    finally:
        ws.close()
        web.close()
    no_secret_on_disk(broker)
