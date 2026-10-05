"""``/close`` and reopen end to end: real MCP agents, the web API and the WebSocket against an
in-process broker (DESIGN.md §28, spec #16 §14 item 14)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import InProcBroker, cookie_of, ws_connect
from fakes.fake_agent import FakeAgent

from switchboard.cli import main
from switchboard.config import Config

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
CLOSED_WAIT = "[switchboard] #build was closed by alice; you are no longer in it."
CLOSED_CALL = "#build was closed by alice; you are no longer in it"


@pytest.fixture
def broker(tmp_home: Path):
    b = InProcBroker(tmp_home, FAST).start()
    web = b.web_client()
    r = web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers())
    assert r.status_code == 200
    b.web = web
    yield b
    web.close()
    b.stop()


async def until(pred: Any, timeout: float = 5.0, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = pred()
        if got:
            return got
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def open_sinks(b: InProcBroker) -> int:
    return b.on_loop(lambda: len(b.state.engine.sinks.open_sinks()))


def command(b: InProcBroker, text: str, slug: str = "build") -> dict[str, Any]:
    r = b.web.post(f"/api/rooms/{slug}/command", json={"text": text}, headers=b.write_headers())
    assert r.status_code == 200, r.text
    return r.json()


def history(b: InProcBroker, slug: str = "build") -> list[dict[str, Any]]:
    r = b.web.get(f"/api/rooms/{slug}/messages")
    assert r.status_code == 200, r.text
    return r.json()["messages"]


def recv_until(ws: Any, pred: Any, timeout: float = 5.0) -> list[dict[str, Any]]:
    """Every frame up to and including the first that matches."""
    got: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        assert left > 0, f"timed out waiting for a frame: {got}"
        f = json.loads(ws.recv(timeout=left))
        got.append(f)
        if pred(f):
            return got


async def test_close_reuse_and_reopen(broker: InProcBroker) -> None:
    b = broker
    ws = ws_connect(b, cookie_of(b.web))
    try:
        ws.send(json.dumps({"t": "hello", "rooms": ["#build"], "after": {}}))
        recv_until(ws, lambda f: f.get("t") == "members")
        async with (
            FakeAgent(b.home, "ka") as alpha,
            FakeAgent(b.home, "kb") as beta,
            FakeAgent(b.home, "kd") as delta,
            FakeAgent(b.home, "kg") as gamma,
        ):
            assert (await alpha.join("#build", "alpha"))["ok"]
            assert (await beta.join("#build", "beta"))["ok"]
            assert (await delta.join("#build", "delta"))["ok"]
            assert (await gamma.join("#build", "gamma"))["ok"]
            assert command(b, "/kick gamma")["ok"]
            t = alpha.wait_task("#build", 30)
            await until(lambda: open_sinks(b) == 1, what="alpha's wait")

            res = await asyncio.to_thread(command, b, "/close")
            assert res == {
                "ok": True,
                "text": "closed #build: 3 agent(s) removed; history kept. The name is free"
                " again; reopen this room from Closed rooms in the web UI",
            }

            # the open wait returns `closed`, with the exact text
            r = await asyncio.wait_for(t, 5)
            assert r["status"] == "closed" and r["text"] == CLOSED_WAIT
            # the next call: the closed error, then the MCP server's own "you are not in"
            r = await beta.say("#build", "anyone?")
            assert r["ok"] is False and r["code"] == "unauthorized" and r["error"] == CLOSED_CALL
            r = await beta.say("#build", "anyone?")
            assert r["ok"] is False and r["code"] == "not_member" and "you are not in #build" in r["error"]
            r = await beta.join("#build", "beta")
            assert r["ok"] is False and r["code"] == "not_found"
            assert r["error"] == "#build was closed by alice: ask your user to reopen it"
            r = await beta.join("#build~closed-1", "beta")
            assert r["ok"] is False and r["code"] == "bad_request"

            # the WebSocket: three leave lines and the notice under #build, then a rooms frame without it
            frames = recv_until(ws, lambda f: f.get("t") == "rooms")
            msgs = [f for f in frames if f.get("t") == "msg"]
            assert all(f["room"] == "#build" for f in msgs)
            assert [(f["msg"]["from"], f["msg"]["kind"], f["msg"]["text"]) for f in msgs[-4:]] == [
                ("alpha", "leave", "left (#build closed)"),
                ("beta", "leave", "left (#build closed)"),
                ("delta", "leave", "left (#build closed)"),
                (
                    "switchboard",
                    "notice",
                    "#build closed by alice (via web): 3 agent(s) removed; the history is kept",
                ),
            ]
            assert frames[-1]["rooms"] == []

            # hidden: not in /api/rooms, listed in /api/closed-rooms; human lookups get the hint
            got = b.web.get("/api/rooms").json()
            assert got == {"rooms": [], "closed": 1}
            [c] = b.web.get("/api/closed-rooms").json()["rooms"]
            assert (c["id"], c["name"], c["display"], c["closed_by"], c["reopenable"]) == (
                1,
                "#build~closed-1",
                "#build",
                "alice",
                True,
            )
            r = b.web.get("/api/rooms/build/messages")
            assert r.status_code == 404 and "it is closed" in r.text

            # the name is free: a new #build, with none of the old lines
            r = b.web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers())
            assert r.status_code == 200 and r.json()["room"]["id"] == 2
            assert [m["text"] for m in history(b)] == ["#build created by alice"]
            # alpha's old credential still names the closed room, never the new one
            r = await alpha.say("#build", "still here?")
            assert r["ok"] is False and r["code"] == "unauthorized" and r["error"] == CLOSED_CALL
            assert [m["text"] for m in history(b)] == ["#build created by alice"]

            # reopen: 409 while the new #build is open
            h = b.write_headers()
            r = b.web.post("/api/closed-rooms/1/reopen", json={}, headers=h)
            assert r.status_code == 409
            assert "#build is taken by an open room: close or delete that room first" in r.text
            assert b.web.get("/api/closed-rooms").json()["rooms"][0]["reopenable"] is False
            assert (await asyncio.to_thread(command, b, "/close"))["ok"]
            r = b.web.post("/api/closed-rooms/1/reopen", json={}, headers=h)
            assert r.status_code == 200, r.text
            room = r.json()["room"]
            assert (room["id"], room["name"]) == (1, "#build")
            texts = [m["text"] for m in history(b)]
            assert texts[0] == "#build created by alice" and "left (#build closed)" in texts
            assert texts[-1] == "#build reopened by alice (via web); agents join() it again"
            assert b.web.get("/api/rooms").json()["closed"] == 1  # the second #build
            r = b.web.post("/api/closed-rooms/1/reopen", json={}, headers=h)
            assert r.status_code == 404 and "no closed room with id 1" in r.text

            # nobody was re-added: alpha joins again, the kicked gamma is still refused,
            # and delta's old credential is now just revoked
            assert (await alpha.join("#build", "alpha"))["ok"]
            r = await gamma.join("#build", "gamma")
            assert r["ok"] is False and r["code"] == "kicked"
            r = await delta.say("#build", "hello?")
            assert r["ok"] is False and r["code"] == "unauthorized" and "was closed" not in r["error"]
    finally:
        ws.close()


def test_close_an_empty_room_and_the_report(broker: InProcBroker) -> None:
    b = broker
    assert command(b, "/close") == {
        "ok": True,
        "text": "closed #build: 0 agent(s) removed; history kept. The name is free again;"
        " reopen this room from Closed rooms in the web UI",
    }
    out = b.home / "r.md"  # a query-only read of the live database (WAL)
    assert main(["report", "--room", "#build", "--home", str(b.home), "--out", str(out)]) == 0
    text = out.read_text()
    assert "# switchboard report: #build (closed)" in text and "#build~closed-1" in text


async def test_agents_cannot_close(broker: InProcBroker) -> None:
    async with FakeAgent(broker.home, "ka") as a:
        await a.join("#build", "alpha")
        assert (await a.say("#build", "/close"))["ok"]
        assert history(broker)[-1]["text"] == "/close"
        assert [r["name"] for r in broker.web.get("/api/rooms").json()["rooms"]] == ["#build"]
        assert (await a.read("#build"))["ok"]


def test_reopen_route_security(broker: InProcBroker) -> None:
    b = broker
    assert command(b, "/close")["ok"]
    h = b.write_headers()
    with httpx.Client(base_url=b.base, timeout=10.0) as anon:
        assert anon.post("/api/closed-rooms/1/reopen", json={}, headers=h).status_code == 401
        assert anon.get("/api/closed-rooms").status_code == 401
    no_x = {k: v for k, v in h.items() if k != "X-Switchboard"}
    assert b.web.post("/api/closed-rooms/1/reopen", json={}, headers=no_x).status_code == 403
    bad_origin = {**h, "Origin": "http://evil.example"}
    assert b.web.post("/api/closed-rooms/1/reopen", json={}, headers=bad_origin).status_code == 403
    # 19 digits pass the pattern, but above 2**63-1 sqlite3 can't bind them (a 500 before)
    for rid in ("0", "01", "-1", "x", "1" * 20, str(2**63), "9" * 19):
        r = b.web.post(f"/api/closed-rooms/{rid}/reopen", json={}, headers=h)
        assert r.status_code == 400 and "bad room id" in r.text, (rid, r.text)
    r = b.web.post(f"/api/closed-rooms/{2**63 - 1}/reopen", json={}, headers=h)
    assert r.status_code == 404 and "no closed room with id" in r.text, r.text
    assert b.web.get("/api/closed-rooms").json()["rooms"][0]["name"] == "#build~closed-1"
