"""BrokerConn reconnect backoff (DESIGN.md §27.14, built in M8a).

Behind a socket that accepts and closes at once (a tunnel whose far end is
down), the old client reset its backoff on every connect and reconnected
without sleeping: 20,009 connects in 3 s were measured. Now the backoff resets
only once a hello was answered on the connection (or, with no hello to send,
once the connection lived 2 s), every ended connection is followed by a sleep,
and EOF fails pending calls at once instead of leaving a hello waiting 10 s.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from switchboard.mcp import client as client_mod
from switchboard.mcp import server as srv
from switchboard.mcp.client import BrokerConn, BrokerDown
from switchboard.paths import Paths

HELLO = {"name": "t", "harness": "test"}
Script = Callable[[int, asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


@pytest.fixture
def sock_dir() -> Iterator[Path]:
    # short: a Unix socket path must fit in about 100 bytes
    d = Path(tempfile.mkdtemp(prefix="yk-bo-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


class FakeBroker:
    """A Unix socket server that runs ``script(n, reader, writer)`` for its n-th connection
    (1-based) and records when each connection was accepted and closed."""

    def __init__(self, path: Path, script: Script):
        self.path = path
        self.script = script
        self.opened: list[float] = []
        self.closed: list[float] = []
        self.server: asyncio.AbstractServer | None = None
        self.tasks: set[asyncio.Task[Any]] = set()

    async def __aenter__(self) -> "FakeBroker":
        self.server = await asyncio.start_unix_server(self._handle, path=str(self.path))
        return self

    async def __aexit__(self, *exc: Any) -> None:
        assert self.server is not None
        self.server.close()
        for t in list(self.tasks):
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.wait_for(self.server.wait_closed(), 5.0)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        self.opened.append(time.monotonic())
        n = len(self.opened)
        try:
            await self.script(n, reader, writer)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            self.closed.append(time.monotonic())


async def close_at_once(n: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    return None


async def answer_hello(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    req = json.loads(await reader.readline())
    assert req["method"] == "mcp.hello"
    writer.write(json.dumps({"id": req["id"], "result": {"ok": True}}).encode() + b"\n")
    await writer.drain()


async def until(cond: Callable[[], bool], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.005)


@pytest.mark.parametrize("hello", [False, True], ids=["no_hello", "hello"])
async def test_accept_then_close_is_bounded(sock_dir: Path, hello: bool) -> None:
    """The regression for the 20,009-connect storm: at most 8 connects in 3 s."""
    path = sock_dir / "b.sock"
    async with FakeBroker(path, close_at_once) as fb:
        conn = BrokerConn(path)  # the production default, (0.5, 10.0)
        if hello:
            conn.hello_params = dict(HELLO)
        conn.start()
        await asyncio.sleep(3.0)
        await conn.close()
    assert 2 <= len(fb.opened) <= 8, len(fb.opened)


async def test_eof_fails_pending_hello_promptly(sock_dir: Path) -> None:
    """The broker end closes after reading the hello: the hello fails at once
    (it used to wait out its 10 s timeout with the connection marked up)."""
    path = sock_dir / "b.sock"

    async def read_then_close(n: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        if n == 1:
            await asyncio.sleep(0.05)  # the hello is in flight
            return
        await asyncio.sleep(30)

    async with FakeBroker(path, read_then_close) as fb:
        conn = BrokerConn(path, backoff=(5.0, 10.0))  # no reconnect within the test
        conn.hello_params = dict(HELLO)
        conn.start()
        await until(lambda: len(fb.closed) == 1, 5.0)
        await until(lambda: not conn.connected.is_set(), 5.0)
        assert time.monotonic() - fb.closed[0] < 0.2
        assert conn.hello_result is None and conn.hello_error is None
        assert not conn.ready.is_set()
        await conn.close()


async def test_eof_fails_a_call_made_while_the_hello_is_in_flight(sock_dir: Path) -> None:
    """A call made while the hello is still unanswered fails at once when the broker
    end closes (the old client left it, and the hello, waiting out their 10 s)."""
    path = sock_dir / "b.sock"

    async def read_two_then_close(n: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()  # the hello, never answered
        if n == 1:
            await reader.readline()  # the call
            await asyncio.sleep(0.05)
            return
        await asyncio.sleep(30)

    async with FakeBroker(path, read_two_then_close):
        conn = BrokerConn(path, backoff=(5.0, 10.0))  # no reconnect within the test
        conn.hello_params = dict(HELLO)
        conn.start()
        await until(conn.connected.is_set, 5.0)
        t0 = time.monotonic()
        with pytest.raises(BrokerDown):
            await conn._call_now("sys.ping", {}, 10.0)
        assert time.monotonic() - t0 < 0.3  # 0.05 s in the fake broker, then at once
        assert not conn.ready.is_set()
        await conn.close()


# The two tests below check the delays the client asks asyncio.sleep for (SleepRecorder),
# not the gaps a clock measures between connections: on a loaded CI runner a 0.1 s gap
# once measured 0.55 s. The fake broker's own holds (0.3 s, 0.2 s, 2.1 s) stay real, since
# the client decides from them whether a connection was healthy.
async def test_backoff_resets_only_after_answered_hello(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = sock_dir / "b.sock"
    rec = SleepRecorder()
    monkeypatch.setattr(client_mod, "asyncio", rec)

    async def script(n: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if n in (1, 2, 5, 6):
            return  # accept, then close: no answer
        if n == 3:  # reads the hello, never answers, holds the connection a while
            await reader.readline()
            await asyncio.sleep(0.3)
            return
        if n == 4:  # answers the hello, then the "broker restarts"
            await answer_hello(reader, writer)
            await asyncio.sleep(0.2)
            return
        await asyncio.sleep(30)

    async with FakeBroker(path, script) as fb:
        conn = BrokerConn(path, backoff=(0.1, 3.2))
        conn.hello_params = dict(HELLO)
        conn.start()
        await until(lambda: len(fb.opened) >= 7, 10.0)
        await conn.close()
    # 0.1, 0.2: doubling while nothing answers; 0.4 after the long connection whose hello was
    # never answered (doubling, not reset); 0.1 after the answered hello (0.8 without the
    # reset); then 0.1, 0.2: doubling again from the first step
    assert rec.delays[:6] == [0.1, 0.2, 0.4, 0.1, 0.1, 0.2], rec.delays


async def test_backoff_resets_after_a_2s_connection_without_hello(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = sock_dir / "b.sock"
    rec = SleepRecorder()
    monkeypatch.setattr(client_mod, "asyncio", rec)

    async def script(n: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if n in (1, 2, 3, 5):
            return
        if n == 4:
            await asyncio.sleep(2.1)
            return
        await asyncio.sleep(30)

    async with FakeBroker(path, script) as fb:
        conn = BrokerConn(
            path, backoff=(0.1, 3.2)
        )  # no hello params (an MCP server before its first tool call)
        conn.start()
        await until(lambda: len(fb.opened) >= 6, 10.0)
        await conn.close()
    # 0.1, 0.2, 0.4: short connections double; a 2 s connection resets (0.8 without the
    # reset); then 0.1 again after the next short one
    assert rec.delays[:5] == [0.1, 0.2, 0.4, 0.1, 0.1], rec.delays


class SleepRecorder:
    """Stands in for the ``asyncio`` module inside mcp.client: records every
    backoff sleep and returns almost at once."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0.001)


def test_satellite_home_caps_backoff_at_2s(tmp_path: Path) -> None:
    desk, pi = Paths.from_home(tmp_path / "desk"), Paths.from_home(tmp_path / "pi")
    for p in (desk, pi):
        p.home.mkdir(mode=0o700)
    pi.satellite_conf.write_text('name = "fpga-pi"\n')
    assert pi.satellite_conf == pi.home / "satellite.toml"
    assert srv.broker_backoff(desk) == (0.5, 10.0)
    assert srv.broker_backoff(pi) == (0.5, 2.0)

    # main() builds its BrokerConn with it
    seen: list[tuple[float, float]] = []

    async def fake_serve(server: Any, st: Any) -> None:
        seen.append(st.conn.backoff)

    mp = pytest.MonkeyPatch()
    old_umask = os.umask(0o022)
    os.umask(old_umask)
    try:
        mp.setattr(srv, "serve", fake_serve)
        for p in (desk, pi):
            assert srv.main(["--home", str(p.home)]) == 0
    finally:
        mp.undo()
        os.umask(old_umask)  # main() sets 077
    assert seen == [(0.5, 10.0), (0.5, 2.0)]

    # and the reconnect loop never sleeps longer than the cap
    async def run(home: Paths) -> list[float]:
        rec = SleepRecorder()
        mp = pytest.MonkeyPatch()
        mp.setattr(client_mod, "asyncio", rec)
        try:
            conn = BrokerConn(home.home / "run" / "none.sock", backoff=srv.broker_backoff(home))
            conn.start()
            while len(rec.delays) < 10:
                await asyncio.sleep(0.005)
            await conn.close()
        finally:
            mp.undo()
        return rec.delays[:10]

    assert asyncio.run(run(pi)) == [0.5, 1.0] + [2.0] * 8
    assert asyncio.run(run(desk)) == [0.5, 1.0, 2.0, 4.0, 8.0] + [10.0] * 5


@pytest.mark.parametrize("hello", [False, True], ids=["no_hello", "hello"])
async def test_satellite_backoff_levels_off_at_2s_behind_accept_then_close(
    sock_dir: Path, hello: bool
) -> None:
    """The post-connection sleeps (not only the connect-failure ones) stop at the 2 s cap
    on a satellite home, and at 10 s by default, behind a socket that accepts and closes."""
    rec = SleepRecorder()
    mp = pytest.MonkeyPatch()
    mp.setattr(client_mod, "asyncio", rec)
    try:
        for i, (backoff, want) in enumerate(
            (((0.5, 2.0), [0.5, 1.0] + [2.0] * 6), ((0.5, 10.0), [0.5, 1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0]))
        ):
            rec.delays.clear()
            path = sock_dir / f"b{i}.sock"
            async with FakeBroker(path, close_at_once) as fb:
                conn = BrokerConn(path, backoff=backoff)
                if hello:
                    conn.hello_params = dict(HELLO)
                conn.start()
                await until(lambda: len(rec.delays) >= 8, 10.0)
                await conn.close()
            assert rec.delays[:8] == want, (backoff, rec.delays)
            assert len(fb.opened) >= 8  # every sleep followed a real connection
    finally:
        mp.undo()
