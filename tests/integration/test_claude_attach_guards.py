"""The broker's own checks on ``mcp.attach`` and ``mcp.posted`` (DESIGN.md §6.4,
§9.2), independent of the MCP server's local guard: only a verified Claude
connection with the session token and a passing guard gets the inbox tier, and
``mcp.posted`` fields that reach the log and the batches table are bounded."""

from __future__ import annotations

import asyncio
import math
from typing import Any

import pytest
from conftest import InProcBroker

from switchboard.broker.agents import McpConn
from switchboard.broker.peer import McpIdentity
from switchboard.broker.service import ServiceError
from switchboard.mcp.client import RpcError, Stream


class StubConn:
    """Stands in for an rpc Conn on the broker's side (attach keeps a reference)."""

    closed = False

    def __init__(self, mc: McpConn | None) -> None:
        self.mcp = mc
        self.id = 99001
        self.pushed: list[tuple[str, dict[str, Any]]] = []

    def push(self, kind: str, data: dict[str, Any]) -> None:
        self.pushed.append((kind, data))


def claude_conn(
    *, token: bool = True, socket: str | None = "/tmp/yk-guard-test.sock", harness: str = "claude"
) -> StubConn:
    ident = McpIdentity(
        harness=harness,
        mcp_pid=987654,
        mcp_start=1.0,
        agent_pid=987650,
        agent_start=0.5,
        evidence="stub",
        claude_socket=socket,
    )
    return StubConn(McpConn(ident=ident, has_messaging_token=token))


def attach(b: InProcBroker, conn: StubConn, params: dict[str, Any]) -> dict[str, Any]:
    return b.on_loop(b.state.agents.attach, conn, params)


def test_an_unverified_connection_claiming_claude_is_refused(broker: InProcBroker) -> None:
    with Stream(broker.paths.sock, timeout=5) as s:
        s.call(
            "mcp.hello", {"harness": "claude", "claude_socket": "/tmp/x.sock", "has_messaging_token": True}, 5
        )
        r = s.call("mcp.attach", {"guard_ok": True}, 5)
        assert r.get("attached") is False
        # and mcp.posted from a connection that isn't attached changes nothing
        assert s.call("mcp.posted", {"batch_id": 1, "ok": True, "t_post": 1e300}, 5) == {}


def test_attach_before_hello_is_unauthorized(broker: InProcBroker) -> None:
    with Stream(broker.paths.sock, timeout=5) as s, pytest.raises(RpcError) as ei:
        s.call("mcp.attach", {"guard_ok": True}, 5)
    assert ei.value.code == "unauthorized"


@pytest.mark.parametrize(
    "case", ["no_token", "guard_missing", "guard_false", "guard_truthy_string", "no_socket", "not_claude"]
)
def test_the_broker_refuses_attach_on_its_own(broker: InProcBroker, case: str) -> None:
    conn = claude_conn(
        token=case != "no_token",
        socket=None if case == "no_socket" else "/tmp/yk-g.sock",
        harness="codex" if case == "not_claude" else "claude",
    )
    params: dict[str, Any] = {"guard_ok": True}
    if case == "guard_missing":
        params = {}
    elif case == "guard_false":
        params = {"guard_ok": False}
    elif case == "guard_truthy_string":
        params = {"guard_ok": "true"}
    r = attach(broker, conn, params)
    assert r["attached"] is False and r["reason"]
    assert conn.mcp is not None and conn.mcp.inbox_attached is False
    assert ("", 987654) not in broker.state.engine.adapters["claude"].conns  # keyed (host, mcp pid)


def test_attach_without_hello_state_raises(broker: InProcBroker) -> None:
    with pytest.raises(ServiceError):
        attach(broker, StubConn(None), {"guard_ok": True})


def test_posted_fields_are_bounded(broker: InProcBroker) -> None:
    conn = claude_conn()
    assert attach(broker, conn, {"guard_ok": True})["attached"] is True
    adapter = broker.state.engine.adapters["claude"]
    try:

        async def post(params: dict[str, Any]) -> dict[str, Any]:
            fut = asyncio.get_running_loop().create_future()
            adapter.pending_posts[77] = (fut, conn)
            broker.state.agents.posted(conn, {"batch_id": 77, **params})
            adapter.pending_posts.pop(77, None)
            return fut.result()

        def run(params: dict[str, Any]) -> dict[str, Any]:
            return asyncio.run_coroutine_threadsafe(post(params), broker.loop).result(5)

        r = run({"ok": False, "err": "OSError\nFAKE log line", "t_post": math.inf})
        assert r == {"ok": False, "err": "post_failed", "t_post": None}
        r = run({"ok": False, "err": "guard", "t_post": math.nan})
        assert r == {"ok": False, "err": "guard", "t_post": None}
        r = run({"ok": True, "t_post": 1790000000.25})
        assert r == {"ok": True, "err": None, "t_post": 1790000000.25}
        # posted for a batch this connection wasn't handed changes nothing
        other = claude_conn()
        other.mcp.inbox_attached = True

        async def foreign() -> bool:
            fut = asyncio.get_running_loop().create_future()
            adapter.pending_posts[78] = (fut, conn)
            broker.state.agents.posted(other, {"batch_id": 78, "ok": True})
            adapter.pending_posts.pop(78, None)
            return fut.done()

        assert asyncio.run_coroutine_threadsafe(foreign(), broker.loop).result(5) is False
    finally:
        broker.on_loop(adapter.detach, conn)


class RemoteStubConn(StubConn):
    """A connection carried by a link, with the facts its satellite attached to the request."""

    remote = True

    def __init__(self, mc: McpConn | None, facts: dict[str, Any]) -> None:
        super().__init__(mc)
        self.facts = facts


@pytest.mark.parametrize(
    ("facts", "marked"), [({"lastmile": True}, True), ({}, False), ({"lastmile": 1}, False)]
)
def test_only_the_satellites_own_report_is_marked(
    broker: InProcBroker, facts: dict[str, Any], marked: bool
) -> None:
    """``mcp.posted`` over a link is the satellite's own last-mile report only when the request
    carries ``facts.lastmile`` (which a client can't set); the adapter re-routes only that one
    uncounted (DESIGN.md §27.5.6, M8d review). A local connection is never marked."""
    base = claude_conn()
    conn = RemoteStubConn(base.mcp, facts)
    adapter = broker.state.engine.adapters["claude"]

    async def post(c: StubConn) -> dict[str, Any]:
        fut = asyncio.get_running_loop().create_future()
        adapter.pending_posts[79] = (fut, c)
        broker.state.agents.posted(c, {"batch_id": 79, "ok": False, "err": "stale_status"})
        adapter.pending_posts.pop(79, None)
        return fut.result()

    assert conn.mcp is not None
    conn.mcp.inbox_attached = True
    r = asyncio.run_coroutine_threadsafe(post(conn), broker.loop).result(5)
    assert r.get("lastmile", False) is marked and r["err"] == "stale_status"
    local = claude_conn()
    assert local.mcp is not None
    local.mcp.inbox_attached = True
    local.facts = {"lastmile": True}  # type: ignore[attr-defined]  # a local conn has no satellite facts
    r = asyncio.run_coroutine_threadsafe(post(local), broker.loop).result(5)
    assert "lastmile" not in r
