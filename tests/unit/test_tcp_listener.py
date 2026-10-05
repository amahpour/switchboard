"""The broker's TCP listener (broker/daemon.py ``loopback_listener``): loopback
only, and accepted connections get TCP_NODELAY (Linux support, DESIGN.md §25).

asyncio sets TCP_NODELAY only on sockets whose ``proto`` is IPPROTO_TCP; the
broker's hand-made socket had proto 0, so on Linux Nagle plus the client's
delayed ACK held a response body behind its headers for about 40 ms per request
(`-m perf`: REST to WebSocket p50 44.7 ms in the Linux container, 0.6 ms on macOS).
"""

from __future__ import annotations

import asyncio
import errno
import os
import socket
import sys
from pathlib import Path

import pytest

from switchboard.broker import daemon
from switchboard.broker.daemon import loopback_listener
from switchboard.config import Config
from switchboard.paths import Paths


def test_binds_loopback_only_with_the_tcp_proto() -> None:
    s = loopback_listener(0)
    try:
        host, port = s.getsockname()
        assert host == "127.0.0.1" and port > 0
        assert s.proto == socket.IPPROTO_TCP
    finally:
        s.close()


def test_a_taken_port_raises_and_closes_its_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[socket.socket] = []

    class Spy(socket.socket):
        def __init__(self, *a: object, **k: object) -> None:
            super().__init__(*a, **k)  # type: ignore[arg-type]
            made.append(self)

    first = loopback_listener(0)
    first.listen()
    try:
        monkeypatch.setattr(daemon.socket, "socket", Spy)
        with pytest.raises(OSError):
            loopback_listener(first.getsockname()[1])
        monkeypatch.undo()
        assert len(made) == 1 and made[0].fileno() == -1  # closed, no fd leaked
    finally:
        first.close()


def test_the_broker_listens_through_loopback_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`run_foreground` (the real broker) must take its socket from the helper,
    or it loses TCP_NODELAY again (the in-process test broker and the perf test
    use the helper directly, so they wouldn't notice)."""
    asked: list[int] = []

    def refuse(port: int) -> socket.socket:
        asked.append(port)
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(daemon, "loopback_listener", refuse)
    monkeypatch.setattr(daemon, "setup_logging", lambda *a, **k: None)  # keep pytest's log handlers
    busy = loopback_listener(0)  # a taken port: a broker that bypassed the helper fails fast, not serves
    busy.listen()
    port = busy.getsockname()[1]
    umask = os.umask(0o022)
    try:
        rc = daemon.run_foreground(Paths.from_home(tmp_path / "yk"), Config(), port=port, announce=False)
    finally:
        os.umask(umask)
        busy.close()
    assert rc == 1
    assert asked == [port]
    assert f"can't listen on 127.0.0.1:{port}: Address already in use" in capsys.readouterr().err


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="macOS reports an accepted socket's proto as 0, so asyncio leaves Nagle on there "
    "(as before; no delay measured: REST to WebSocket p50 0.6 ms)",
)
async def test_accepted_connections_get_tcp_nodelay() -> None:
    """The same path uvicorn takes: an asyncio server on the pre-bound socket."""
    s = loopback_listener(0)
    seen: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    class P(asyncio.Protocol):
        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            sock = transport.get_extra_info("socket")
            if not seen.done():
                seen.set_result(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))
            transport.close()

    server = await asyncio.get_running_loop().create_server(P, sock=s)
    w = None
    try:
        _r, w = await asyncio.open_connection("127.0.0.1", s.getsockname()[1])
        assert await asyncio.wait_for(seen, 5) != 0
    finally:
        if w is not None:
            w.close()
        server.close()
        await asyncio.wait_for(server.wait_closed(), 5)
