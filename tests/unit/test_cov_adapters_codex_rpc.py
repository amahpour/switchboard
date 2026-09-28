"""The Codex app-server client's edges (DESIGN.md §9.3, §11 items 2 and 5): the
allowlists' remaining refusals, the socket ownership check, and the wire client
against a scripted server that fails ``initialize``, sends junk, drops the
connection mid-request or has a notification handler that raises. Every server
here is in-process on a short Unix socket path; nothing reaches a real daemon."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from switchboard.adapters import codex_rpc as rpc
from switchboard.adapters.codex_rpc import CodexRpc, ForbiddenRpc, check_request

Handler = Callable[[Any], Awaitable[None]]


# ------------------------------------------------------------ allowlists
def test_params_must_be_an_object() -> None:
    for bad in ("threadId", ["threadId"], 5):
        with pytest.raises(ForbiddenRpc, match="params must be an object"):
            check_request("thread/read", bad)
    check_request("initialized", None)  # no params at all is an empty object


def test_thread_read_params_are_type_checked() -> None:
    with pytest.raises(ForbiddenRpc, match="thread/read: bad params"):
        check_request("thread/read", {"threadId": 5})
    with pytest.raises(ForbiddenRpc, match="thread/read: bad params"):
        check_request("thread/read", {"threadId": "t", "includeTurns": "yes"})
    with pytest.raises(ForbiddenRpc, match="thread/read: bad params"):
        check_request("thread/read", {})


def test_the_override_denylist_holds_even_if_an_allowlist_were_widened(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defence in depth: the per-method key tables are the first gate, the
    guardrails' override-field list the second. Widen a table by mistake and
    the override key is still refused before anything is sent."""
    monkeypatch.setitem(rpc.PARAM_KEYS, "thread/loaded/list", frozenset({"cursor", "model"}))
    with pytest.raises(ForbiddenRpc, match="override field"):
        check_request("thread/loaded/list", {"model": "gpt-x"})
    check_request("thread/loaded/list", {"cursor": "c1"})


def test_contains_is_false_for_a_thread_that_does_not_serialize() -> None:
    assert rpc.contains({"items": [{"text": "yk:b1.0"}]}, "yk:b1.0")
    assert rpc.contains({"odd": {1, 2}}, "1") is False  # a set is not JSON: no match, no crash


def test_join_proof_skips_malformed_turns() -> None:
    needle = "yk:j0123456789abcdef"
    good = {"type": "mcpToolCall", "server": "switchboard", "tool": "join", "status": "completed",
            "result": {"content": [{"type": "text", "text": needle}]}}
    assert not rpc.join_proven({"turns": ["x", {"items": "x"}, {"id": "t"}]}, needle)
    assert rpc.join_proven({"turns": ["x", {"items": "x"}, {"items": ["junk", good]}]}, needle)


def test_server_version_needs_a_version_in_the_user_agent() -> None:
    assert rpc.server_version({"userAgent": "codex_cli_rs (unknown build)"}) is None
    assert rpc.server_version({"userAgent": 156}) is None


# ------------------------------------------------------------ socket
def test_a_socket_owned_by_someone_else_is_refused(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = socket.socket(socket.AF_UNIX)
    try:
        s.bind(str(tmp_home / "cx.sock"))
        assert rpc.check_socket(tmp_home / "cx.sock") == os.path.realpath(tmp_home / "cx.sock")
        me = os.getuid()
        monkeypatch.setattr(rpc.os, "getuid", lambda: me + 1)
        with pytest.raises(rpc.SocketRefused, match="not owned by this user"):
            rpc.check_socket(tmp_home / "cx.sock")
    finally:
        s.close()


# ------------------------------------------------------------ the wire
async def serve(path: Path, handler: Handler) -> Any:
    from websockets.asyncio.server import unix_serve

    server = await unix_serve(handler, str(path), ping_interval=None, compression=None)
    return server


async def stop(server: Any) -> None:
    server.close()
    await server.wait_closed()


def init_ok(m: dict[str, Any]) -> str:
    return json.dumps({"id": m["id"], "result": {"userAgent": "fake/0.156.1"}})


async def test_a_failed_initialize_closes_the_connection_and_raises(tmp_home: Path) -> None:
    async def handler(ws: Any) -> None:
        async for frame in ws:
            m = json.loads(frame)
            if m.get("method") == "initialize":
                await ws.send(json.dumps({"id": m["id"], "error": {"code": -32000, "message": "nope"}}))

    server = await serve(tmp_home / "cx.sock", handler)
    try:
        c = CodexRpc(str(tmp_home / "cx.sock"))
        with pytest.raises(rpc.RpcError) as ei:
            await c.connect(timeout=2.0)
        assert ei.value.code == -32000 and ei.value.message == "nope"
        assert c.closed and c._ws is None and c.init_result is None
    finally:
        await stop(server)


async def test_junk_frames_are_skipped_and_a_raising_handler_is_contained(
        tmp_home: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Frames that aren't a JSON object, a message with neither id nor a string
    method, and a notification handler that raises all leave the link up."""
    async def handler(ws: Any) -> None:
        async for frame in ws:
            m = json.loads(frame)
            if m.get("method") == "initialize":
                await ws.send("not json {")
                await ws.send(json.dumps(["a", "list"]))
                await ws.send(json.dumps({"method": 7, "params": {}}))
                await ws.send(json.dumps({"params": {"threadId": "t"}}))
                await ws.send(json.dumps({"method": "thread/closed", "params": "not an object"}))
                await ws.send(init_ok(m))
            elif m.get("method") == "thread/loaded/list" and "id" in m:
                await ws.send(json.dumps({"id": m["id"], "result": {"data": ["t1", 5], "nextCursor": ""}}))

    seen: list[tuple[str, dict[str, Any]]] = []

    def on_note(method: str, params: dict[str, Any], t: float) -> None:
        seen.append((method, params))
        raise RuntimeError("handler bug")

    server = await serve(tmp_home / "cx.sock", handler)
    try:
        c = CodexRpc(str(tmp_home / "cx.sock"), on_notification=on_note, clock=lambda: 42.0)
        with caplog.at_level(logging.ERROR, logger="switchboard.codex.rpc"):
            assert await c.connect(timeout=2.0) == {"userAgent": "fake/0.156.1"}
            assert await c.loaded_threads(timeout=2.0) == {"t1"}  # a non-string id is dropped
        assert seen == [("thread/closed", {})]  # params that aren't an object reach it as {}
        assert c.notes == {"thread/closed": 1} and not c.server_requests
        assert not c.closed
        assert "codex notification handler failed (thread/closed)" in caplog.text
        await c.close()
    finally:
        await stop(server)


async def test_a_dropped_connection_fails_the_request_in_flight(tmp_home: Path) -> None:
    """The server vanishes (no close frame) while a request waits: the request
    fails with ConnectionError at once, and the client says it is closed."""
    async def handler(ws: Any) -> None:
        async for frame in ws:
            m = json.loads(frame)
            if m.get("method") == "initialize":
                await ws.send(init_ok(m))
            elif m.get("method") == "thread/read":
                ws.transport.abort()  # no answer, no close handshake
                return

    server = await serve(tmp_home / "cx.sock", handler)
    try:
        c = CodexRpc(str(tmp_home / "cx.sock"))
        await c.connect(timeout=2.0)
        with pytest.raises(ConnectionError, match="connection closed"):
            await c.read_thread("t1", timeout=5.0)
        await asyncio.wait_for(c.closed_event.wait(), 2.0)
        assert c.closed and c._pending == {}
        # nothing more can be sent on it
        with pytest.raises(ConnectionError, match="connection closed"):
            await c.request("thread/loaded/list", {}, timeout=1.0)
        with pytest.raises(ConnectionError, match="connection closed"):
            await c.notify("initialized")
        await c.close()
    finally:
        await stop(server)


async def test_notify_with_params_sends_them_and_no_id(tmp_home: Path) -> None:
    got: list[dict[str, Any]] = []
    done = asyncio.Event()

    async def handler(ws: Any) -> None:
        async for frame in ws:
            m = json.loads(frame)
            got.append(m)
            if m.get("method") == "initialize":
                await ws.send(init_ok(m))
            elif m.get("method") == "thread/loaded/list":
                done.set()

    server = await serve(tmp_home / "cx.sock", handler)
    try:
        c = CodexRpc(str(tmp_home / "cx.sock"))
        await c.connect(timeout=2.0)
        await c.notify("thread/loaded/list", {"cursor": "c9"})
        await asyncio.wait_for(done.wait(), 2.0)
        assert got[-1] == {"method": "thread/loaded/list", "params": {"cursor": "c9"}}
        with pytest.raises(ForbiddenRpc):
            await c.notify("turn/interrupt", {"threadId": "t"})
        await c.close()
    finally:
        await stop(server)


async def test_close_never_raises_even_when_the_socket_close_fails() -> None:
    class BrokenWs:
        closed = 0

        async def close(self) -> None:
            BrokenWs.closed += 1
            raise OSError("already gone")

    c = CodexRpc("/nonexistent/cx.sock")
    c._ws = BrokenWs()
    c._reader = asyncio.get_running_loop().create_task(asyncio.sleep(60))  # a reader still running
    await c.close()
    assert BrokenWs.closed == 1 and c._ws is None and c.closed
    assert c._reader.cancelled()
    await c.close()  # twice is fine


async def test_one_shot_reads_on_a_fresh_connection_and_closes_it(tmp_home: Path) -> None:
    conns: list[int] = []
    open_now: set[int] = set()

    async def handler(ws: Any) -> None:
        me = len(conns) + 1
        conns.append(me)
        open_now.add(me)
        try:
            async for frame in ws:
                m = json.loads(frame)
                if m.get("method") == "initialize":
                    await ws.send(init_ok(m))
                elif m.get("method") == "thread/read":
                    tid = m["params"]["threadId"]
                    res: Any = {"thread": {"id": tid, "status": {"type": "idle"}}} if tid == "t1" else {"thread": 5}
                    await ws.send(json.dumps({"id": m["id"], "result": res}))
        finally:
            open_now.discard(me)

    server = await serve(tmp_home / "cx.sock", handler)
    try:
        path = str(tmp_home / "cx.sock")
        th = await rpc.one_shot(path, lambda r: r.read_thread("t1"), timeout=2.0)
        assert th == {"id": "t1", "status": {"type": "idle"}}
        assert await rpc.one_shot(path, lambda r: r.read_thread("t2"), timeout=2.0) == {}  # not a thread object
        assert conns == [1, 2]
        for _ in range(100):  # the server notices each close a moment later
            if not open_now:
                break
            await asyncio.sleep(0.01)
        assert open_now == set()
    finally:
        await stop(server)
