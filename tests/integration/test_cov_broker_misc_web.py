"""Web routes and the WebSocket on their error paths (DESIGN.md §5.4, §5.5).

The REST checks run on the real app (``broker``/``web`` fixtures). Two WebSocket
teardown paths the real server can't be made to take on demand (a disconnect raised
from ``receive()``, a sender stuck in ``send_text``) drive the endpoint directly with
a stand-in socket.
"""

from __future__ import annotations

import asyncio
import json
import types
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.websockets import WebSocketDisconnect, WebSocketState
from websockets.exceptions import ConnectionClosed

from conftest import InProcBroker, cookie_of, ws_connect
from switchboard.broker import web as webmod
from switchboard.broker.auth import COOKIE_NAME
from switchboard.broker.hub import Hub, Subscriber


def err(r: httpx.Response) -> tuple[int, str, str]:
    body = r.json()
    return r.status_code, body["error"], body["message"]


# ------------------------------------------------------------------- REST
def test_favicon_is_the_32_px_icon(broker: InProcBroker) -> None:
    r = httpx.get(broker.base + "/favicon.ico")  # no session needed
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG\r\n\x1a\n") and r.content[16:24] == (32).to_bytes(4, "big") * 2
    assert r.headers["cache-control"] == "no-cache"
    for path, kind in (("/static/favicon.svg", "image/svg+xml"), ("/static/favicon-32.png", "image/png"),
                       ("/static/apple-touch-icon.png", "image/png")):
        r = httpx.get(broker.base + path)  # the sign-in page links them too: no session needed
        assert r.status_code == 200 and r.headers["content-type"].startswith(kind), path
        assert "script-src 'self'" in r.headers["content-security-policy"], path


def test_request_bodies_are_checked(broker: InProcBroker, web: httpx.Client) -> None:
    h = broker.write_headers()
    big = json.dumps({"name": "#build", "pad": "x" * webmod.MAX_BODY})
    assert err(web.post("/api/rooms", content=big, headers=h)) == (400, "bad_request", "request body too large")
    assert err(web.post("/api/rooms", content=b"", headers=h)) == (400, "bad_request", "name is required")
    assert err(web.post("/api/rooms", content="{", headers=h)) == (400, "bad_request", "invalid JSON")
    for body in ("[]", '["#build"]', '"#build"', "3"):
        assert err(web.post("/api/rooms", content=body, headers=h)) == (400, "bad_request",
                                                                         "body must be a JSON object"), body
    assert err(web.post("/api/rooms", json={"name": 5}, headers=h)) == (400, "bad_request", "name is required")
    # none of that made a room
    assert web.get("/api/rooms").json()["rooms"] == []


def test_say_and_command_need_text(broker: InProcBroker, web: httpx.Client) -> None:
    h = broker.write_headers()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=h).status_code == 200
    for body in ({}, {"text": None}, {"text": 5}, {"text": ["hi"]}):
        assert err(web.post("/api/rooms/build/say", json=body, headers=h)) == (400, "bad_request",
                                                                                "text is required"), body
        assert err(web.post("/api/rooms/build/command", json=body, headers=h)) == (400, "bad_request",
                                                                                    "text is required"), body
    msgs = web.get("/api/rooms/build/messages").json()["messages"]
    assert [m for m in msgs if m["sender_kind"] == "human"] == []  # nothing was posted as the human
    assert web.post("/api/rooms/build/command", json={"text": "/who"}, headers=h).json()["ok"] is True


def test_members_and_messages_of_a_bad_room_or_query(broker: InProcBroker, web: httpx.Client) -> None:
    assert err(web.get("/api/rooms/nope/members")) == (404, "not_found", "no such room: #nope")
    assert web.post("/api/rooms", json={"name": "#build"}, headers=broker.write_headers()).status_code == 200
    assert web.get("/api/rooms/build/members").json()["room"] == "#build"
    for key in ("after", "limit"):
        assert err(web.get(f"/api/rooms/build/messages?{key}=-1")) == (400, "bad_request", f"{key} must be >= 0")
        assert err(web.get(f"/api/rooms/build/messages?{key}=x")) == (400, "bad_request",
                                                                       f"{key} must be an integer")
    assert web.get("/api/rooms/build/messages?after=&limit=0").status_code == 200  # blank and 0: defaults


def test_logout_with_a_bad_body_keeps_the_session(broker: InProcBroker, web: httpx.Client) -> None:
    h = broker.write_headers()
    assert err(web.post("/logout", content="{nope", headers=h)) == (400, "bad_request", "invalid JSON")
    assert err(web.post("/logout", content="[true]", headers=h)) == (400, "bad_request",
                                                                     "body must be a JSON object")
    assert web.get("/api/me").status_code == 200  # still signed in
    assert web.post("/logout", content=b"", headers=h).json() == {"ok": True, "revoked": 1}
    assert web.get("/api/me").status_code == 401


def test_ws_hello_with_a_room_that_is_not_text_closes(broker: InProcBroker, web: httpx.Client) -> None:
    ws = ws_connect(broker, cookie_of(web))
    ws.send('{"t":"ping"}')
    assert ws.recv(timeout=5) == '{"t": "pong"}'
    ws.send(json.dumps({"t": "hello", "rooms": ["#build", 7]}))
    with pytest.raises(ConnectionClosed) as e:
        ws.recv(timeout=5)
    assert e.value.rcvd is not None and e.value.rcvd.code == 1008


# ------------------------------------------------- WebSocket teardown paths
ORIGIN = "http://switchboard.localhost:1"


class RecHub(Hub):
    def __init__(self) -> None:
        super().__init__()
        self.added: list[Subscriber] = []

    def add(self, sub: Subscriber) -> None:
        self.added.append(sub)
        super().add(sub)


class FakeWS:
    """Just what ``ws_endpoint`` uses of a starlette WebSocket."""

    def __init__(self, incoming: list[Any], *, block_sends: bool = False) -> None:
        self.headers = {"origin": ORIGIN}
        self.cookies = {COOKIE_NAME: "good"}
        self.client_state = WebSocketState.CONNECTING
        self.incoming = incoming
        self.block_sends = block_sends
        self.sent: list[str] = []
        self.send_cancelled = False
        self.closes: list[int] = []

    async def accept(self) -> None:
        self.client_state = WebSocketState.CONNECTED

    async def receive(self) -> dict[str, Any]:
        item = self.incoming.pop(0)
        if isinstance(item, BaseException):
            self.client_state = WebSocketState.DISCONNECTED
            raise item
        return item

    async def send_text(self, text: str) -> None:
        if self.block_sends:
            try:
                await asyncio.Event().wait()  # a client that never reads
            except asyncio.CancelledError:
                self.send_cancelled = True
                raise
        self.sent.append(text)

    async def close(self, code: int = 1000) -> None:
        self.closes.append(code)
        self.client_state = WebSocketState.DISCONNECTED


def ws_endpoint(hub: Hub) -> Any:
    state = types.SimpleNamespace(
        origin=ORIGIN,
        sessions=types.SimpleNamespace(check=lambda sid: "sid-hash" if sid == "good" else None),
        hub=hub,
        store=types.SimpleNamespace(web_session_person=lambda h: None),  # the owner's session (§32)
        cfg=types.SimpleNamespace(human_name="alice"),
    )
    app = FastAPI()
    webmod.install(app, state)  # type: ignore[arg-type]
    [route] = [r for r in app.routes if getattr(r, "path", None) == "/ws"]
    return route.endpoint


def text_frame(obj: dict[str, Any]) -> dict[str, Any]:
    return {"type": "websocket.receive", "text": json.dumps(obj)}


def test_a_disconnect_raised_by_receive_ends_the_socket_cleanly() -> None:
    hub = RecHub()
    endpoint = ws_endpoint(hub)
    ws = FakeWS([text_frame({"t": "ping"}), WebSocketDisconnect(1001)])
    asyncio.run(asyncio.wait_for(endpoint(ws), 5))  # returns: the disconnect does not escape
    [sub] = hub.added
    assert sub.closed and sub.sid_hash == "sid-hash" and hub.subs == set()
    assert ws.closes == []  # the client is gone: nothing to close
    assert ws.sent == ['{"t": "pong"}']  # what was queued before the disconnect still drains


class _ShortGrace:
    """``web``'s asyncio, with the 1 s grace for a stuck sender cut to 20 ms."""

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    def wait_for(self, aw: Any, timeout: float) -> Any:
        assert timeout == 1.0  # the product's grace
        return asyncio.wait_for(aw, 0.02)


def test_a_stuck_sender_is_cancelled_and_the_socket_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webmod, "asyncio", _ShortGrace())
    hub = RecHub()
    endpoint = ws_endpoint(hub)
    ws = FakeWS([text_frame({"t": "ping"}), text_frame({"t": "say", "text": "x"})], block_sends=True)

    async def run() -> None:
        await asyncio.wait_for(endpoint(ws), 5)
        await asyncio.sleep(0)  # let the cancellation reach the sender

    asyncio.run(run())
    [sub] = hub.added
    assert sub.closed and hub.subs == set()
    assert ws.send_cancelled and ws.sent == []  # the pong never went out
    assert ws.closes == [1008]  # closed by the endpoint, not by the stuck sender
