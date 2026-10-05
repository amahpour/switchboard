"""What a remote connection may call (DESIGN.md §27.5.2): the exact allowlist, no human,
room, sys or remote method, and the refusal before any role check under every policy."""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any

import pytest

from switchboard.broker import rpc
from switchboard.broker.peer import AllowAllHumans, Peer
from switchboard.broker.remote import RemotePeer
from switchboard.remote import proto

EXACT = {
    "mcp.hello",
    "mcp.attach",
    "mcp.posted",
    "mcp.bye",
    "agent.join",
    "agent.leave",
    "agent.who",
    "agent.say",
    "agent.read",
    "agent.wait",
    "agent.unwait",
    "agent.pass",
    "agent.away",
    "agent.review",
    "hook.event",
    "hook.ack",
}


def methods() -> dict[str, rpc.MethodSpec]:
    return rpc.build_methods(SimpleNamespace(peer_policy=AllowAllHumans()))  # type: ignore[arg-type]


def test_remote_methods_exact_set() -> None:
    assert set(rpc.REMOTE_METHODS) == EXACT
    assert rpc.REMOTE_METHODS is proto.REMOTE_METHODS  # the satellite's own refusal uses the same set
    assert EXACT <= set(methods())


def test_remote_methods_have_no_human_or_room_role() -> None:
    table = methods()
    for name in rpc.REMOTE_METHODS:
        assert table[name].role in rpc.REMOTE_ROLES, name
        assert not name.startswith(("sys.", "room.", "human.", "remote.")), name
    # and every method outside the allowlist that a human role guards stays outside it
    for name, spec in table.items():
        if spec.role in ("human", "human_cli", "login"):
            assert name not in rpc.REMOTE_METHODS
    for name in table:
        if name.startswith(("sys.", "room.", "human.", "remote.")):
            assert name not in rpc.REMOTE_METHODS


class _Conn(rpc.Conn):
    """A connection that records what it is sent."""

    def __init__(self, peer: Peer, remote: bool):
        self.id = next(rpc.Conn._ids)
        self.peer = peer
        self.closed = False
        self.tails = []
        self.mcp = None
        self.sent: list[dict[str, Any]] = []
        self._remote = remote

    @property
    def remote(self) -> bool:  # type: ignore[override]
        return self._remote

    def send(self, obj: dict[str, Any]) -> None:
        self.sent.append(obj)


def _server(called: list[str]) -> rpc.RpcServer:
    srv = rpc.RpcServer.__new__(rpc.RpcServer)
    srv.policy = AllowAllHumans()
    srv._tasks = set()

    def spec(role: str, name: str) -> rpc.MethodSpec:
        async def h(conn: Any, p: dict[str, Any]) -> dict[str, Any]:
            called.append(name)
            return {"ok": True}

        return rpc.MethodSpec(role, h)

    srv.methods = {name: spec(s.role, name) for name, s in methods().items()}
    return srv


FORBIDDEN_HERE = [
    "sys.stop",
    "sys.status",
    "sys.ping",
    "human.say",
    "human.command",
    "human.login_link",
    "human.logout_all",
    "room.create",
    "room.list",
    "room.who",
    "room.history",
    "room.tail",
    "remote.enable",
    "remote.disable",
    "remote.remove",
    "remote.status",
    "room.delete",
    "no.such.method",
]


@pytest.mark.parametrize("method", FORBIDDEN_HERE)
def test_forbidden_before_authorize_under_allow_all_humans(method: str) -> None:
    called: list[str] = []
    srv = _server(called)
    authorized: list[str] = []
    real = srv._authorize
    srv._authorize = lambda conn, role, m: (authorized.append(m), real(conn, role, m))[1]  # type: ignore[method-assign]
    # a remote peer with this process's pid and uid still gets nothing: the allowlist comes first
    conn = _Conn(RemotePeer(pid=os.getpid(), uid=os.getuid(), start=None, host="fpga-pi"), remote=True)
    asyncio.run(srv._dispatch(conn, json.dumps({"id": 1, "method": method, "params": {}}).encode()))
    assert called == [] and authorized == []
    [reply] = conn.sent
    assert reply["error"]["code"] == "forbidden" and rpc.REMOTE_FORBIDDEN in reply["error"]["message"]
    # control: the same request from a local peer under AllowAllHumans gets through
    if method != "no.such.method":
        local = _Conn(Peer(pid=os.getpid(), uid=os.getuid(), start=None), remote=False)
        asyncio.run(srv._dispatch(local, json.dumps({"id": 1, "method": method, "params": {}}).encode()))
        assert called == [method]


def test_remote_connection_never_gets_a_human_role() -> None:
    srv = _server([])
    conn = _Conn(RemotePeer(pid=None, uid=None, start=None, host="fpga-pi"), remote=True)
    for role in ("human", "human_cli", "login", "something"):
        with pytest.raises(rpc.RpcError) as ei:
            srv._authorize(conn, role, "x.y")
        assert ei.value.code == "forbidden"
    srv._authorize(conn, "anon", "mcp.hello")
    srv._authorize(conn, "hook", "hook.event")
    # the policies refuse a remote peer on their own too (belt and braces)
    peer = RemotePeer(pid=None, uid=None, start=None, host="fpga-pi")
    pol = AllowAllHumans()
    assert not pol.human_allowed(peer) and not pol.human_cli_allowed(peer) and not pol.login_allowed(peer)
