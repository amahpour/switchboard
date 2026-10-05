"""/hops against a real broker: the web session, the UDS roles, the CLI, and a loop-guard
pause that shows once on the WebSocket and in ``tail`` (DESIGN.md §10, §22)."""

from __future__ import annotations

import json
import os
import time
from itertools import takewhile
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import InProcBroker, SubprocBroker, cookie_of, human_cli_denial_word, ws_connect

from switchboard.broker import proc
from switchboard.broker.peer import ProcessPeerPolicy
from switchboard.mcp.client import RpcError, Stream, call_sync


def recv_until(ws: Any, pred, timeout: float = 5.0) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The first frame matching ``pred``, plus every frame read on the way (it included)."""
    seen: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        assert left > 0, f"timed out waiting for a frame; saw {seen!r}"
        f = json.loads(ws.recv(timeout=left))
        seen.append(f)
        if pred(f):
            return f, seen


def is_msg(text: str):
    return lambda f: f.get("t") == "msg" and f["msg"]["text"] == text


def web_cmd(broker: InProcBroker, web: httpx.Client, text: str) -> dict[str, Any]:
    r = web.post("/api/rooms/build/command", json={"text": text}, headers=broker.write_headers())
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def room(broker: InProcBroker, web: httpx.Client) -> str:
    r = web.post("/api/rooms", json={"name": "#build"}, headers=broker.write_headers())
    assert r.status_code == 200, r.text
    return "#build"


def agent_post(broker: InProcBroker, text: str) -> None:
    """An agent chat message through the broker's own persist-then-publish path (on its loop)."""
    svc = broker.state.service

    def post() -> None:
        svc._post(
            svc.room("#build"),
            sender_name="alpha",
            sender_kind="agent",
            sender_harness="test",
            via="mcp",
            text=text,
        )

    broker.on_loop(post)


def test_hops_from_the_web_updates_settings_and_posts_a_notice(
    broker: InProcBroker, web: httpx.Client, room: str
) -> None:
    ws = ws_connect(broker, cookie_of(web))
    try:
        ws.send(json.dumps({"t": "hello", "rooms": [room], "after": {}}))
        recv_until(ws, lambda f: f.get("t") == "members")
        res = web_cmd(broker, web, "/hops 30")
        assert res == {"ok": True, "text": "#build: hop limit set to 30 (was 6); hops 0/30"}
        n, _ = recv_until(ws, lambda f: f.get("t") == "msg" and f["msg"]["kind"] == "notice")
        assert n["msg"]["text"] == "alice set the hop limit to 30 (was 6) (via web)"
        f, _ = recv_until(ws, lambda f: f.get("t") == "room")
        assert f["settings"]["hop_limit"] == 30 and f["settings"]["hop_count"] == 0
        web_cmd(broker, web, "/hops 0")
        f, _ = recv_until(ws, lambda f: f.get("t") == "room" and f["settings"]["hop_limit"] == 0)
        rooms = web.get("/api/rooms").json()["rooms"]
        assert rooms[0]["settings"]["hop_limit"] == 0
        assert "hops 0, loop guard off" in web_cmd(broker, web, "/status")["text"]
        assert broker.call("sys.status")["rooms"][0]["hop_limit"] == 0
    finally:
        ws.close()


def test_a_loop_guard_pause_shows_once_on_the_websocket_and_in_tail(
    broker: InProcBroker, web: httpx.Client, room: str
) -> None:
    assert web_cmd(broker, web, "/hops 2")["ok"]  # lower it (the web may do anything)
    ws = ws_connect(broker, cookie_of(web))
    tail = Stream(broker.paths.sock)
    try:
        ws.send(json.dumps({"t": "hello", "rooms": [room], "after": {}}))
        recv_until(ws, lambda f: f.get("t") == "members")
        assert tail.call("room.tail", {"room": room})["following"] is True
        agent_post(broker, "hop one")
        agent_post(broker, "hop two")  # the 2nd in a row trips the guard
        st = broker.state.store.get_room("#build")
        assert st.paused and st.paused_reason == "loop guard" and st.hop_count == 2
        # a later message: every frame about the pause is in before it (one ordered queue each)
        r = web.post("/api/rooms/build/say", json={"text": "after the pause"}, headers=broker.write_headers())
        assert r.status_code == 200
        _, frames = recv_until(ws, is_msg("after the pause"))
        guard = [
            f
            for f in frames
            if (f.get("t") == "msg" and "loop guard:" in f["msg"]["text"])
            or (f.get("t") == "notice" and "loop guard:" in f["text"])
        ]
        assert len(guard) == 1, guard
        assert (
            guard[0]["t"] == "msg"
            and guard[0]["msg"]["kind"] == "notice"
            and guard[0]["msg"]["level"] == "warn"
        )
        assert "2 agent messages in a row" in guard[0]["msg"]["text"] and "(now 2)" in guard[0]["msg"]["text"]
        pushes = []
        for p in tail.pushes(timeout=5):
            pushes.append(p)
            if p["push"] == "message" and p["data"]["msg"]["text"] == "after the pause":
                break
        texts = [
            p["data"]["msg"]["text"] if p["push"] == "message" else p["data"].get("text", "") for p in pushes
        ]
        assert sum("loop guard:" in t for t in texts) == 1, texts
        # history has it once too
        msgs = web.get("/api/rooms/build/messages").json()["messages"]
        [hist] = [m for m in msgs if "loop guard:" in m["text"]]
        assert hist["level"] == "warn"  # a reload still styles it as a warning (not a column: derived)
        # raising it while paused keeps the pause and says how to continue
        res = web_cmd(broker, web, "/hops 30")
        assert res["text"] == "#build: hop limit set to 30 (was 2); now 0/30; /resume to continue"
        assert broker.state.store.get_room("#build").paused
    finally:
        ws.close()
        tail.close()


def test_hops_roles_over_the_uds(tmp_home: Path) -> None:
    """A caller that passes only the human_cli check may lower the limit, never raise it or turn it off."""

    class CliOnly(ProcessPeerPolicy):
        def __init__(self) -> None:
            super().__init__(
                chain_fn=lambda pid: (
                    list(takewhile(lambda p: p.pid != os.getppid(), proc.ancestry(pid, 12))),
                    True,
                )
            )

        def human_cli_allowed(self, peer) -> bool:  # type: ignore[override]
            return peer.uid == os.getuid()

        def login_allowed(self, peer) -> bool:  # type: ignore[override]
            return peer.uid == os.getuid()

    b = InProcBroker(tmp_home, policy=CliOnly()).start()
    try:
        web = b.web_client()
        web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers())

        def uds(text: str) -> dict[str, Any]:
            return call_sync(b.paths.sock, "human.command", {"room": "#build", "text": text})

        assert uds("/hops")["text"].startswith("#build: hops 0/6")
        for text, why in (("/hops 7", "raising the hop limit"), ("/hops 0", "turning the loop guard off")):
            with pytest.raises(RpcError) as e:
                uds(text)
            assert e.value.code == "forbidden" and why in e.value.message and "web session" in e.value.message
        assert b.state.store.get_room("#build").hop_limit == 6
        assert uds("/hops 3")["text"] == "#build: hop limit set to 3 (was 6); hops 0/3"
        r = web.post("/api/rooms/build/command", json={"text": "/hops 30"}, headers=b.write_headers())
        assert r.status_code == 200 and r.json()["ok"]
        assert uds("/hops 12")["ok"]  # 30 -> 12 is a lowering
        assert b.state.store.get_room("#build").hop_limit == 12
        with pytest.raises(RpcError) as e:
            uds("/hops 1001")
        assert e.value.code == "bad_request"
        notices = [
            m["text"]
            for m in web.get("/api/rooms/build/messages").json()["messages"]
            if m["kind"] == "notice"
        ]
        assert any(n.startswith("alice set the hop limit to 3 (was 6) (via cli:") for n in notices)
        assert "alice set the hop limit to 30 (was 3) (via web)" in notices
        kinds = [(e.kind, e.data) for e in b.state.store.recent_events(kinds=["hop_limit_set"])]
        assert [d["new"] for _, d in kinds] == [12, 30, 3]
    finally:
        b.stop()


def test_hops_from_the_cli(subproc_broker: SubprocBroker) -> None:
    """``switchboard cmd '#room' /hops …`` with test trust (the 'human' role): show, set, off, bounds."""
    b = subproc_broker
    assert b.cli("create", "#build").returncode == 0
    r = b.cli("cmd", "#build", "/hops")
    assert r.returncode == 0 and "#build: hops 0/6" in r.stdout, r.stderr
    r = b.cli("cmd", "#build", "/hops", "12")
    assert r.returncode == 0 and "hop limit set to 12 (was 6); hops 0/12" in r.stdout, r.stderr
    r = b.cli("status")
    assert r.returncode == 0 and "hops 0/12" in r.stdout, r.stdout
    r = b.cli("cmd", "#build", "hops", "0")  # leading slash optional
    assert r.returncode == 0 and "loop guard off" in r.stdout, r.stderr
    assert "hops 0, loop guard off" in b.cli("status").stdout
    r = b.cli("cmd", "#build", "/hops", "1001")
    assert r.returncode == 1 and "between 0 and 1000" in r.stderr
    r = b.cli("tail", "#build", "--no-follow")
    assert "-!- alice turned the loop guard off (hop limit was 12) (via cli:" in r.stdout


def test_hops_from_the_cli_without_test_trust(tmp_home: Path) -> None:
    """The real peer check: raising refused either way; lowering allowed from a real terminal."""
    b = SubprocBroker(tmp_home, trust=False).start()
    try:
        tok = b.paths.test_login_token.read_text().strip()
        with httpx.Client(base_url=b.base, timeout=10) as web:
            assert web.get(f"/login?t={tok}").status_code == 303
            h = {"Origin": b.base, "X-Switchboard": "1"}
            assert web.post("/api/rooms", json={"name": "#build"}, headers=h).status_code == 200
        word = human_cli_denial_word()
        denied = word is not None
        why = word or "web session"
        for args in (("cmd", "#build", "/hops", "30"), ("cmd", "#build", "/hops", "0")):
            r = b.cli(*args)
            assert r.returncode == 1 and "forbidden" in r.stderr and why in r.stderr, r.stderr
        r = b.cli("cmd", "#build", "/hops", "3")
        if denied:  # this pytest runs under an agent harness (or an ssh login): human_cli is refused too
            assert r.returncode == 1 and "forbidden" in r.stderr and why in r.stderr
        else:
            assert r.returncode == 0 and "hop limit set to 3 (was 6)" in r.stdout, r.stderr
    finally:
        b.kill()
