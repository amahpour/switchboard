"""A page with more rooms than one hello may name says hello in batches (DESIGN.md §5.5).

The broker refuses a hello that names more than ``WS_MAX_ROOMS`` rooms and closes the socket,
so a page with 65 rooms stayed disconnected ("reconnecting") for as long as it had them. The
page now sends batches (tests/unit/test_web_app_behavior.py), which only works if a later hello
adds to what the socket follows. These tests pin that, and that the cap on one hello stays.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest
from conftest import InProcBroker, cookie_of, ws_connect
from websockets.exceptions import ConnectionClosed

from switchboard.broker.web import WS_MAX_ROOMS


def make_rooms(broker: InProcBroker, web: httpx.Client, n: int) -> list[str]:
    names = [f"#room-{i:02d}" for i in range(n)]
    for name in names:
        r = web.post("/api/rooms", json={"name": name}, headers=broker.write_headers())
        assert r.status_code == 200, r.text
    return names


def frames_until(ws: Any, done: Any, seconds: float = 10.0) -> list[dict[str, Any]]:
    """The frames the socket sends, up to and including the first one ``done`` accepts."""
    out: list[dict[str, Any]] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        frame = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
        out.append(frame)
        if done(frame):
            return out
    raise AssertionError(f"no frame was accepted by {done}; saw {[f.get('t') for f in out]}")


def test_a_second_hello_adds_its_rooms_to_what_the_socket_follows(
    broker: InProcBroker, web: httpx.Client
) -> None:
    """Two hellos (64 rooms, then 6) make the socket follow all 70: a message posted in the last
    room reaches it. Fails if a later hello replaced the rooms of the first, or closed the socket."""
    names = make_rooms(broker, web, WS_MAX_ROOMS + 6)
    last = names[-1]
    ws = ws_connect(broker, cookie_of(web))
    ws.send(json.dumps({"t": "hello", "rooms": names[:WS_MAX_ROOMS], "after": {}}))
    ws.send(json.dumps({"t": "hello", "rooms": names[WS_MAX_ROOMS:], "after": {}}))
    # the second hello's room frame (its settings) says it was taken; the socket is still open
    frames_until(ws, lambda f: f.get("t") == "room" and f.get("room") == last)
    posted = web.post(
        f"/api/rooms/{last[1:]}/say", json={"text": "hello last room"}, headers=broker.write_headers()
    )
    assert posted.status_code == 200, posted.text
    frames = frames_until(ws, lambda f: f.get("t") == "msg" and f["msg"]["text"] == "hello last room")
    assert frames[-1]["room"] == last
    # and the first batch is still followed
    web.post(
        f"/api/rooms/{names[0][1:]}/say", json={"text": "hello first room"}, headers=broker.write_headers()
    )
    frames = frames_until(ws, lambda f: f.get("t") == "msg" and f["msg"]["text"] == "hello first room")
    assert frames[-1]["room"] == names[0]
    ws.close()


def test_one_hello_over_the_cap_still_closes_the_socket(broker: InProcBroker, web: httpx.Client) -> None:
    """The cap on one hello stays: the page's batches work within it, they don't lift it. Fails if
    the broker started taking a hello of more than WS_MAX_ROOMS rooms."""
    names = make_rooms(broker, web, WS_MAX_ROOMS + 1)
    ws = ws_connect(broker, cookie_of(web))
    ws.send(json.dumps({"t": "hello", "rooms": names, "after": {}}))
    with pytest.raises(ConnectionClosed) as e:
        while True:
            ws.recv(timeout=5)
    assert e.value.rcvd is not None and e.value.rcvd.code == 1008
