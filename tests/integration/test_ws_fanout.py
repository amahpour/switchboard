"""Fan-out: WebSocket clients and UDS tails all get every message (DESIGN.md §5.5, §12.2)."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest

from conftest import InProcBroker, cookie_of, ws_connect
from switchboard.mcp.client import Stream


def recv_until(ws: Any, pred, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        assert left > 0, "timed out waiting for a frame"
        f = json.loads(ws.recv(timeout=left))
        if pred(f):
            return f


def is_msg(text: str):
    return lambda f: f.get("t") == "msg" and f["msg"]["text"] == text


def hello(ws: Any, rooms: list[str], after: dict[str, int] | None = None) -> None:
    ws.send(json.dumps({"t": "hello", "rooms": rooms, "after": after or {}}))


@pytest.fixture
def room(broker: InProcBroker, web: httpx.Client) -> str:
    r = web.post("/api/rooms", json={"name": "#build"}, headers=broker.write_headers())
    assert r.status_code == 200, r.text
    return "#build"


def test_two_websockets_and_a_uds_tail_get_the_message(broker: InProcBroker, web: httpx.Client, room: str) -> None:
    ck = cookie_of(web)
    ws1, ws2 = ws_connect(broker, ck), ws_connect(broker, cookie_of(broker.web_client()))
    tail = Stream(broker.paths.sock)
    try:
        for ws in (ws1, ws2):
            hello(ws, [room])
            recv_until(ws, lambda f: f.get("t") == "members")  # hello processed
        res = tail.call("room.tail", {"room": room})
        assert res["following"] is True
        t0 = time.monotonic()
        r = web.post("/api/rooms/build/say", json={"text": "fan out!"}, headers=broker.write_headers())
        assert r.status_code == 200
        mid = r.json()["id"]
        f1 = recv_until(ws1, is_msg("fan out!"))
        f2 = recv_until(ws2, is_msg("fan out!"))
        push = next(p for p in tail.pushes(timeout=5) if p["push"] == "message")
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0  # loose target; the perf target (50 ms) is marked separately
        for f in (f1, f2):
            m = f["msg"]
            assert f["room"] == room and m["id"] == mid
            assert (m["from"], m["sender_kind"], m["via"], m["kind"]) == ("alice", "human", "web", "chat")
            assert set(m) == {"id", "ts", "from", "harness", "sender_kind", "via", "kind", "text",
                              "reply_to", "mentions", "host"}  # host: M8c, None on this machine
            assert m["host"] is None
        assert push["data"]["room"] == room and push["data"]["msg"]["id"] == mid
    finally:
        ws1.close()
        ws2.close()
        tail.close()


@pytest.mark.perf
def test_fanout_latency_perf(broker: InProcBroker, web: httpx.Client, room: str) -> None:
    ws = ws_connect(broker, cookie_of(web))
    hello(ws, [room])
    recv_until(ws, lambda f: f.get("t") == "members")
    samples = []
    for i in range(20):
        t0 = time.monotonic()
        web.post("/api/rooms/build/say", json={"text": f"p{i}"}, headers=broker.write_headers())
        recv_until(ws, is_msg(f"p{i}"))
        samples.append(time.monotonic() - t0)
    ws.close()
    samples.sort()
    p50, p95 = samples[len(samples) // 2], samples[int(len(samples) * 0.95) - 1]
    print(f"REST say -> WebSocket: p50 {p50 * 1000:.1f} ms, p95 {p95 * 1000:.1f} ms (n={len(samples)})")
    assert p50 < 0.05


def test_hello_backlog_settings_members_and_after(broker: InProcBroker, web: httpx.Client, room: str) -> None:
    h = broker.write_headers()
    ids = [web.post("/api/rooms/build/say", json={"text": f"m{i}"}, headers=h).json()["id"] for i in range(3)]
    ws = ws_connect(broker, cookie_of(web))
    try:
        hello(ws, [room, "#nope", "bad name!"])
        frames = [json.loads(ws.recv(timeout=5)) for _ in range(6)]  # notice + 3 msgs + room + members
        kinds = [f["t"] for f in frames]
        assert kinds == ["msg", "msg", "msg", "msg", "room", "members"]
        assert [f["msg"]["text"] for f in frames[:4]] == ["#build created by alice", "m0", "m1", "m2"]
        assert frames[4]["settings"]["budget_remaining"] == 60 and frames[4]["settings"]["test_mode"] is True
        assert frames[5]["members"] == []
    finally:
        ws.close()
    ws = ws_connect(broker, cookie_of(web))
    try:
        hello(ws, [room], {room: ids[1]})
        f = json.loads(ws.recv(timeout=5))
        assert f["t"] == "msg" and f["msg"]["id"] == ids[2]
        assert json.loads(ws.recv(timeout=5))["t"] == "room"
    finally:
        ws.close()


def test_room_settings_and_notices_are_pushed(broker: InProcBroker, web: httpx.Client, room: str) -> None:
    ws = ws_connect(broker, cookie_of(web))
    try:
        hello(ws, [room])
        recv_until(ws, lambda f: f.get("t") == "members")
        r = web.post("/api/rooms/build/command", json={"text": "/pause"}, headers=broker.write_headers())
        assert r.json()["ok"] is True
        n = recv_until(ws, lambda f: f.get("t") == "msg" and f["msg"]["kind"] == "notice")
        assert "paused the room" in n["msg"]["text"]
        f = recv_until(ws, lambda f: f.get("t") == "room")
        assert f["settings"]["paused"] is True and f["settings"]["paused_reason"] == "paused by alice"
        # a new web login is announced to every connected tab
        broker.web_client()
        n = recv_until(ws, lambda f: f.get("t") == "notice")
        assert n["text"].startswith("a login link was issued via cli") and n["level"] == "warn"
        n = recv_until(ws, lambda f: f.get("t") == "notice")
        assert n["text"] == "new web login" and n["level"] == "warn" and n["room"] is None
        # creating a room tells every tab
        web.post("/api/rooms", json={"name": "random"}, headers=broker.write_headers())
        f = recv_until(ws, lambda f: f.get("t") == "rooms")
        assert f["rooms"] == ["#build", "#random"]
    finally:
        ws.close()


def test_join_and_leave_lines_reach_the_ui(broker: InProcBroker, web: httpx.Client, room: str) -> None:
    """M2 posts join/leave lines through the same path; the UI renders kind=join/leave."""
    ws = ws_connect(broker, cookie_of(web))
    try:
        hello(ws, [room])
        recv_until(ws, lambda f: f.get("t") == "members")
        st = broker.state

        def post_join_leave() -> None:
            r = st.store.get_room(room)
            st.service._post(r, sender_name="claude-1", sender_kind="agent", sender_harness="claude",
                             via="system", kind="join", text="joined")
            st.service._post(r, sender_name="claude-1", sender_kind="agent", sender_harness="claude",
                             via="system", kind="leave", text="left")

        broker.on_loop(post_join_leave)
        j = recv_until(ws, lambda f: f.get("t") == "msg" and f["msg"]["kind"] == "join")
        lv = recv_until(ws, lambda f: f.get("t") == "msg" and f["msg"]["kind"] == "leave")
        assert j["msg"]["from"] == lv["msg"]["from"] == "claude-1"
    finally:
        ws.close()


def test_display_text_is_cleaned(broker: InProcBroker, web: httpx.Client, room: str) -> None:
    ws = ws_connect(broker, cookie_of(web))
    try:
        hello(ws, [room])
        recv_until(ws, lambda f: f.get("t") == "members")
        web.post("/api/rooms/build/say", json={"text": "hi\x1b[2J‮evil <b>x</b>"}, headers=broker.write_headers())
        f = recv_until(ws, lambda f: f.get("t") == "msg" and f["msg"]["kind"] == "chat")
        assert f["msg"]["text"] == "hi[2Jevil <b>x</b>"
    finally:
        ws.close()


def test_rest_history_and_members(broker: InProcBroker, web: httpx.Client, room: str) -> None:
    h = broker.write_headers()
    for i in range(5):
        web.post("/api/rooms/build/say", json={"text": f"h{i}"}, headers=h)
    msgs = web.get("/api/rooms/build/messages").json()["messages"]
    assert [m["text"] for m in msgs][-5:] == [f"h{i}" for i in range(5)]
    last2 = web.get("/api/rooms/build/messages?limit=2").json()["messages"]
    assert [m["text"] for m in last2] == ["h3", "h4"]
    after = web.get(f"/api/rooms/build/messages?after={msgs[-2]['id']}").json()["messages"]
    assert [m["text"] for m in after] == ["h4"]
    assert web.get("/api/rooms/build/messages?after=x").status_code == 400
    assert web.get("/api/rooms/nope/messages").status_code == 404
    mem = web.get("/api/rooms/build/members").json()
    assert mem["room"] == room and mem["human"] == "alice" and mem["members"] == []
    rooms = web.get("/api/rooms").json()["rooms"]
    assert rooms[0]["name"] == room and rooms[0]["last_id"] == msgs[-1]["id"]
    # validation
    assert web.post("/api/rooms", json={"name": "Bad Name"}, headers=h).status_code == 400
    assert web.post("/api/rooms", json={"name": "#build"}, headers=h).status_code == 409
    assert web.post("/api/rooms/build/say", json={"text": ""}, headers=h).status_code == 400
    assert web.post("/api/rooms/build/say", json={"text": "x" * 4001}, headers=h).status_code == 400
    assert web.post("/api/rooms/build/say", content="nope", headers=h).status_code == 400
    assert web.post("/api/rooms/build/command", json={"text": "/nope"}, headers=h).status_code == 400
    assert web.post("/api/rooms/build/command", json={"text": "/kick x"}, headers=h).status_code == 404


def test_long_absence_replays_the_newest_lines_without_a_hole(
    broker: InProcBroker, web: httpx.Client, room: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from switchboard.broker import web as web_mod

    monkeypatch.setattr(web_mod, "WS_BACKLOG", 3)
    h = broker.write_headers()
    ids = [web.post("/api/rooms/build/say", json={"text": f"n{i}"}, headers=h).json()["id"] for i in range(8)]
    ws = ws_connect(broker, cookie_of(web))
    try:
        hello(ws, [room], {room: ids[0]})
        first = json.loads(ws.recv(timeout=5))
        assert first["t"] == "notice" and first["text"].startswith("4 earlier message(s) not shown")
        got = [json.loads(ws.recv(timeout=5)) for _ in range(3)]
        assert [f["msg"]["text"] for f in got] == ["n5", "n6", "n7"]
        web.post("/api/rooms/build/say", json={"text": "live"}, headers=h)
        f = recv_until(ws, lambda f: f.get("t") == "msg")
        assert f["msg"]["text"] == "live"
    finally:
        ws.close()


def test_replay_skip_count_is_per_room(
    broker: InProcBroker, web: httpx.Client, room: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Message ids are global, so the 'not shown' count must count this room's rows."""
    from switchboard.broker import web as web_mod

    monkeypatch.setattr(web_mod, "WS_BACKLOG", 3)
    h = broker.write_headers()
    assert web.post("/api/rooms", json={"name": "#b"}, headers=h).status_code == 200
    a_ids = []
    for i in range(6):  # #build and #b alternate
        a_ids.append(web.post("/api/rooms/build/say", json={"text": f"a{i}"}, headers=h).json()["id"])
        web.post("/api/rooms/b/say", json={"text": f"b{i}"}, headers=h)
    ws = ws_connect(broker, cookie_of(web))
    try:
        hello(ws, [room], {room: a_ids[0]})  # a1..a5 are newer: 2 skipped, 3 replayed
        first = json.loads(ws.recv(timeout=5))
        assert first["t"] == "notice" and first["text"].startswith("2 earlier message(s) not shown")
        got = [json.loads(ws.recv(timeout=5))["msg"]["text"] for _ in range(3)]
        assert got == ["a3", "a4", "a5"]
    finally:
        ws.close()
    ws = ws_connect(broker, cookie_of(web))
    try:
        hello(ws, [room])  # no 'after': #build has its created notice + a0..a5 = 7; 4 skipped
        first = json.loads(ws.recv(timeout=5))
        assert first["t"] == "notice" and first["text"].startswith("4 earlier message(s) not shown")
    finally:
        ws.close()


def test_tail_after_pages_through_everything(broker: InProcBroker, room: str) -> None:
    for i in range(30):
        broker.call("human.say", {"room": room, "text": f"t{i}"})
    s = Stream(broker.paths.sock)
    try:
        res = s.call("room.tail", {"room": room, "after": 0, "limit": 10})
        assert res["more"] is True and len(res["messages"]) == 10
        res2 = s.call("room.tail", {"room": room, "limit": 10})
        assert res2["more"] is False and res2["messages"][-1]["text"] == "t29"
    finally:
        s.close()
