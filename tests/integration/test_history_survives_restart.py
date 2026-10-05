"""History survives a restart; a reconnecting tab resumes after its last id (DESIGN.md §12.2)."""

from __future__ import annotations

import json

from conftest import InProcBroker, cookie_of, ws_connect


def test_history_survives_restart(broker: InProcBroker) -> None:
    web = broker.web_client()
    h = broker.write_headers()
    web.post("/api/rooms", json={"name": "#build"}, headers=h)
    web.post("/api/rooms", json={"name": "#ops"}, headers=h)
    sent = [
        web.post("/api/rooms/build/say", json={"text": f"msg {i}"}, headers=h).json()["id"] for i in range(10)
    ]
    broker.call("human.say", {"room": "#ops", "text": "ops line"})
    broker.call("human.command", {"room": "#build", "text": "/pause"})
    before = web.get("/api/rooms/build/messages").json()["messages"]

    broker.restart()
    web2 = broker.web_client()
    after = web2.get("/api/rooms/build/messages").json()["messages"]
    assert after == before
    assert [r["name"] for r in web2.get("/api/rooms").json()["rooms"]] == ["#build", "#ops"]
    assert web2.get("/api/rooms/ops/messages").json()["messages"][-1]["text"] == "ops line"
    room = next(r for r in web2.get("/api/rooms").json()["rooms"] if r["name"] == "#build")
    assert room["settings"]["paused"] is True  # room state persisted too

    ws = ws_connect(broker, cookie_of(web2))
    try:
        ws.send(json.dumps({"t": "hello", "rooms": ["#build"], "after": {"#build": sent[-1]}}))
        f = json.loads(ws.recv(timeout=5))
        assert f["t"] == "msg" and f["msg"]["kind"] == "notice"  # only what came after msg 9
        assert json.loads(ws.recv(timeout=5))["t"] == "room"
    finally:
        ws.close()
    new_id = web2.post("/api/rooms/build/say", json={"text": "later"}, headers=h).json()["id"]
    assert new_id > before[-1]["id"]
