"""UDS JSON-lines RPC: framing, errors, roles (DESIGN.md §5.1, §5.2)."""

from __future__ import annotations

import json
import os
import socket
import time
from itertools import takewhile
from pathlib import Path

import pytest
from conftest import InProcBroker, has_controlling_tty, human_cli_denial_word

from switchboard.broker import proc
from switchboard.broker.peer import ProcessPeerPolicy
from switchboard.mcp.client import RpcError, Stream, call_sync


def raw_lines(sock_path: Path, payload: bytes, n: int, timeout: float = 5.0) -> list[dict]:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(sock_path))
    s.sendall(payload)
    buf = b""
    out: list[dict] = []
    deadline = time.monotonic() + timeout
    while len(out) < n and time.monotonic() < deadline:
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            out.append(json.loads(line))
    s.close()
    return out


def test_ping_and_status(broker: InProcBroker) -> None:
    r = broker.call("sys.ping")
    assert r["pid"] == os.getpid() and r["port"] == broker.port and r["test_mode"] is True
    st = broker.call("sys.status")
    assert st["rooms"] == [] and st["members"] == 0 and st["test_mode"] is True
    assert st["url"] == f"http://switchboard.localhost:{broker.port}/"


def test_framing_errors(broker: InProcBroker) -> None:
    out = raw_lines(
        broker.paths.sock,
        b'not json\n{"id": 1}\n{"id": "x", "method": "sys.ping"}\n'
        b'{"id": 2, "method": "nope.nope"}\n{"id": 3, "method": "sys.ping", "params": []}\n'
        b'{"id": 4, "method": "sys.ping"}\n',
        6,
    )
    assert out[0]["error"]["code"] == "bad_request" and out[0]["id"] is None
    assert out[1]["error"]["code"] == "bad_request"
    assert out[2]["error"]["code"] == "bad_request"
    assert out[3] == {"id": 2, "error": {"code": "not_found", "message": "unknown method nope.nope"}}
    assert out[4]["error"]["code"] == "bad_request"
    assert out[5]["id"] == 4 and "result" in out[5]


def test_pipelined_requests_answer_in_order(broker: InProcBroker) -> None:
    payload = b"".join(json.dumps({"id": i, "method": "sys.ping"}).encode() + b"\n" for i in range(1, 21))
    out = raw_lines(broker.paths.sock, payload, 20)
    assert [o["id"] for o in out] == list(range(1, 21))


def test_oversized_line_is_refused(broker: InProcBroker) -> None:
    big = b'{"id": 1, "method": "sys.ping", "params": {"x": "' + b"a" * (1 << 20) + b'"}}\n'
    out = raw_lines(broker.paths.sock, big, 1)
    assert out and out[0]["error"]["code"] == "bad_request"


def test_service_errors_map_to_codes(broker: InProcBroker) -> None:
    with pytest.raises(RpcError) as e:
        broker.call("room.who", {"room": "#nope"})
    assert e.value.code == "not_found"
    with pytest.raises(RpcError) as e:
        broker.call("room.who", {})
    assert e.value.code == "bad_request"
    with pytest.raises(RpcError) as e:
        broker.call("room.create", {"name": "No Spaces"})
    assert e.value.code == "bad_request"
    broker.call("room.create", {"name": "#build"})
    with pytest.raises(RpcError) as e:
        broker.call("room.create", {"name": "#build"})
    assert e.value.code == "conflict"
    with pytest.raises(RpcError) as e:
        broker.call("room.history", {"room": "#build", "after": "x"})
    assert e.value.code == "bad_request"


def test_tail_pushes_and_unsubscribes_on_close(broker: InProcBroker) -> None:
    broker.call("room.create", {"name": "#build"})
    s = Stream(broker.paths.sock)
    res = s.call("room.tail", {"room": "#build", "limit": 0})
    assert res["messages"] == []
    broker.call("human.say", {"room": "#build", "text": "one"})
    push = next(s.pushes(timeout=5))
    assert push["push"] == "message" and push["data"]["msg"]["text"] == "one"
    s.close()
    time.sleep(0.2)
    subs = broker.on_loop(lambda: len(broker.state.hub.subs))
    assert subs == 0


def test_socket_is_owner_checked_by_clients(tmp_path: Path) -> None:
    from switchboard.mcp.client import BrokerDown, check_socket_owner

    with pytest.raises(BrokerDown):
        check_socket_owner(tmp_path / "missing.sock")
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises(BrokerDown):
        check_socket_owner(f)


def test_real_peer_policy_roles(tmp_home: Path) -> None:
    """ProcessPeerPolicy on the live UDS: anon works; 'human' never over the UDS;
    human_cli and login depend on who runs pytest (agent harness or not, TTY or not)."""
    b = InProcBroker(tmp_home, policy=ProcessPeerPolicy()).start()
    try:
        assert b.call("sys.ping")["pid"] == os.getpid()
        with pytest.raises(RpcError) as e:
            b.call("room.create", {"name": "#build"})
        assert e.value.code == "forbidden" and "web session" in e.value.message
        word = human_cli_denial_word()
        denied = word is not None
        if denied or not has_controlling_tty():
            with pytest.raises(RpcError) as e:
                b.call("human.login_link")
            assert e.value.code == "forbidden"
        else:  # a real terminal, no agent above it
            assert "/login?t=" in b.call("human.login_link")["url"]
        if denied:
            with pytest.raises(RpcError) as e:
                b.call("human.say", {"room": "#build", "text": "x"})
            assert e.value.code == "forbidden" and word in e.value.message
            with pytest.raises(RpcError):
                b.call("sys.stop")
        else:
            with pytest.raises(RpcError) as e:
                b.call("human.say", {"room": "#build", "text": "x"})
            assert e.value.code == "not_found"
    finally:
        b.stop()


def test_command_roles_over_uds_without_trust(tmp_home: Path) -> None:
    """With a policy that grants only human_cli: reducing commands work, raising ones are forbidden."""

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
        assert (
            "paused" in call_sync(b.paths.sock, "human.command", {"room": "#build", "text": "/pause"})["text"]
        )
        for text in ["/resume", "/budget 61", "/release x"]:
            with pytest.raises(RpcError) as e:
                call_sync(b.paths.sock, "human.command", {"room": "#build", "text": text})
            assert e.value.code == "forbidden", text
        assert call_sync(b.paths.sock, "human.command", {"room": "#build", "text": "/budget 3"})["ok"]
        # the same raising command works from the web session
        r = web.post("/api/rooms/build/command", json={"text": "/resume"}, headers=b.write_headers())
        assert r.status_code == 200 and r.json()["ok"]
        notices = [
            m["text"]
            for m in web.get("/api/rooms/build/messages").json()["messages"]
            if m["kind"] == "notice"
        ]
        assert any("(via cli:" in n for n in notices) and any("(via web)" in n for n in notices)
    finally:
        b.stop()


def test_login_refusal_texts(tmp_home: Path) -> None:
    """``login`` without the terminal: room.delete names itself; human.login_link keeps its
    own wording (§28.6). The refusal comes before the handler, so nothing is looked up."""
    from switchboard.broker.peer import PeerPolicy

    class CliNoTty(PeerPolicy):
        def human_cli_allowed(self, peer) -> bool:  # type: ignore[override]
            return peer.uid == os.getuid()

    b = InProcBroker(tmp_home, policy=CliNoTty()).start()
    try:
        with pytest.raises(RpcError) as e:
            b.call("room.delete", {"room": "#build", "dry_run": True})
        assert e.value.code == "forbidden"
        assert e.value.message == (
            "room.delete must come from a terminal you typed in"
            " (not an agent's shell, nor a script without a terminal)"
        )
        with pytest.raises(RpcError) as e:
            b.call("human.login_link")
        assert e.value.code == "forbidden"
        assert e.value.message == (
            "login links are only issued to a terminal you typed in: run `switchboard login` there"
        )
    finally:
        b.stop()


def test_room_delete_params_and_room_list_closed(broker: InProcBroker) -> None:
    """room.delete without the plan's pin is refused; room.list reports closed rooms."""
    with pytest.raises(RpcError) as e:
        broker.call("room.delete", {"room": "#build"})
    assert e.value.code == "bad_request" and e.value.message == (
        "room_id, name and created_at are required: run the plan first"
    )
    with pytest.raises(RpcError) as e:
        broker.call("room.delete", {"dry_run": True})
    assert e.value.code == "bad_request" and e.value.message == "room is required"
    with pytest.raises(RpcError) as e:
        broker.call("room.delete", {"room": "#nope", "dry_run": True})
    assert e.value.code == "not_found" and e.value.message == "no such room: #nope"
    web = broker.web_client()
    web.post("/api/rooms", json={"name": "#build"}, headers=broker.write_headers())
    lst = broker.call("room.list")
    assert lst["closed"] == 0 and [(r["name"], type(r["id"])) for r in lst["rooms"]] == [("#build", int)]
    assert broker.call("human.command", {"room": "#build", "text": "/close"})["ok"]
    assert broker.call("room.list") == {"rooms": [], "closed": 1}
    got = broker.call("room.list", {"closed": True})
    assert got["closed"] == 1 and [r["display"] for r in got["rooms"]] == ["#build"]
    assert broker.call("sys.status")["closed_rooms"] == 1
    plan = broker.call("room.delete", {"room": "#build", "dry_run": True})
    assert plan["state"] == "closed" and plan["closed_by"] == "alice"
    res = broker.call(
        "room.delete",
        {
            "room": "#build",
            "room_id": plan["room_id"],
            "name": plan["name"],
            "created_at": plan["created_at"],
        },
    )
    assert res["removed"]["rooms"] == 1 and Path(res["backup"]).exists()
    assert broker.call("room.list", {"closed": True}) == {"rooms": [], "closed": 0}


def test_second_broker_on_the_same_home_refuses_to_start(broker: InProcBroker) -> None:
    other = InProcBroker(broker.home)
    with pytest.raises(RuntimeError, match="failed to start"):
        other.start()
    other.stop()
    assert broker.call("sys.ping")["port"] == broker.port  # the first one is untouched
