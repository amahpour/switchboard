"""A CLI say reaches the browser's WebSocket; a web say shows up in `switchboard tail` (DESIGN.md §12.2)."""

from __future__ import annotations

import json
import time

import httpx
from conftest import SubprocBroker

from switchboard.mcp.client import call_sync


def test_cli_to_web_and_web_to_cli(subproc_broker: SubprocBroker) -> None:
    from websockets.sync.client import connect

    b = subproc_broker
    assert b.cli("create", "#build").returncode == 0
    tok = b.paths.test_login_token.read_text().strip()
    web = httpx.Client(base_url=b.base, timeout=10)
    assert web.get(f"/login?t={tok}").status_code == 303
    cookie = web.cookies.get("switchboard_session")
    assert cookie
    ws = connect(
        f"ws://switchboard.localhost:{b.port}/ws",
        origin=b.base,
        additional_headers={"Cookie": f"switchboard_session={cookie}"},
        open_timeout=5,
        legacy=True,
    )
    try:
        ws.send(json.dumps({"t": "hello", "rooms": ["#build"]}))
        while json.loads(ws.recv(timeout=5))["t"] != "members":
            pass
        assert b.cli("say", "#build", "from the terminal").returncode == 0
        deadline = time.monotonic() + 5
        while True:
            f = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
            if f["t"] == "msg" and f["msg"]["kind"] == "chat":
                break
        assert f["msg"]["text"] == "from the terminal" and f["msg"]["via"] == "cli"
        h = {"Origin": b.base, "X-Switchboard": "1"}
        assert (
            web.post("/api/rooms/build/say", json={"text": "from the browser"}, headers=h).status_code == 200
        )
        r = b.cli("tail", "#build", "--no-follow")
        assert r.stdout.splitlines()[-1].endswith("<alice> from the browser")
        hist = call_sync(b.paths.sock, "room.history", {"room": "#build"})["messages"]
        assert [m["via"] for m in hist if m["kind"] == "chat"] == ["cli", "web"]
    finally:
        ws.close()
        web.close()
