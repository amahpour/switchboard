"""Broker clients over the Unix socket (DESIGN.md §5.1).

``call_sync``/``Stream`` serve the CLI; ``BrokerConn`` (asyncio) serves the
MCP server: it reconnects with backoff and re-sends ``mcp.hello`` on every
new connection. Before connecting, every client checks that the socket
belongs to the current user.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import socket
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

MAX_LINE = 1 << 20
log = logging.getLogger("switchboard.mcp.client")


class BrokerDown(Exception):
    """No broker is listening on the socket."""


class RpcError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def check_socket_owner(path: str | os.PathLike) -> None:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise BrokerDown(f"no broker socket at {path}") from None
    if not stat.S_ISSOCK(st.st_mode):
        raise BrokerDown(f"{path} is not a socket")
    if st.st_uid != os.getuid():
        raise PermissionError(f"{path} is owned by uid {st.st_uid}; refusing to connect")


def connect(path: str | os.PathLike, timeout: float = 5.0) -> socket.socket:
    check_socket_owner(path)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError) as e:
        s.close()
        raise BrokerDown(str(e)) from None
    except OSError:
        s.close()
        raise
    return s


class Stream:
    """A connection that can send requests and read responses and pushes."""

    def __init__(self, path: str | os.PathLike, timeout: float = 5.0):
        self.sock = connect(path, timeout)
        self.buf = b""
        self._next_id = 1
        self.pending_pushes: list[dict[str, Any]] = []

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "Stream":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def send(self, method: str, params: dict[str, Any] | None = None) -> int:
        rid = self._next_id
        self._next_id += 1
        line = json.dumps({"id": rid, "method": method, "params": params or {}}) + "\n"
        self.sock.sendall(line.encode())
        return rid

    def read_obj(self, timeout: float | None) -> dict[str, Any] | None:
        """Next JSON object, or None on timeout. Raises BrokerDown on EOF."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = None
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
            # The socket's own timeout (poll-based), not select(): select() refuses
            # fds >= FD_SETSIZE (1024), which a process with a high open-file limit
            # (Linux containers default to 1048576) can reach.
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                return None
            finally:
                self.sock.settimeout(None)  # blocking again, as before, for later sends
            if not chunk:
                raise BrokerDown("broker closed the connection")
            self.buf += chunk
            if len(self.buf) > MAX_LINE and b"\n" not in self.buf:
                raise RpcError("bad_request", "response line too long")
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
        rid = self.send(method, params)
        deadline = time.monotonic() + timeout
        while True:
            obj = self.read_obj(max(0.0, deadline - time.monotonic()))
            if obj is None:
                raise TimeoutError(f"{method}: no answer within {timeout} s")
            if "push" in obj:
                self.pending_pushes.append(obj)
                continue
            if obj.get("id") not in (rid, None):
                continue
            if "error" in obj:
                err = obj["error"] or {}
                raise RpcError(str(err.get("code", "internal")), str(err.get("message", "")))
            return obj.get("result") or {}

    def pushes(self, timeout: float | None = None) -> Iterator[dict[str, Any]]:
        """Yield pushes until EOF (or until ``timeout`` passes with nothing new)."""
        while self.pending_pushes:
            yield self.pending_pushes.pop(0)
        while True:
            obj = self.read_obj(timeout)
            if obj is None:
                return
            if "push" in obj:
                yield obj


def call_sync(
    path: str | os.PathLike,
    method: str,
    params: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    with Stream(path, timeout=min(timeout, 5.0)) as s:
        return s.call(method, params, timeout)


def ping(path: str | os.PathLike, timeout: float = 2.0) -> dict[str, Any] | None:
    try:
        return call_sync(path, "sys.ping", {}, timeout)
    except (BrokerDown, OSError, TimeoutError, RpcError, ValueError):
        return None


def sock_for(home: str | os.PathLike | None) -> Path:
    from switchboard.paths import Paths

    return Paths.from_home(home).sock


class BrokerConn:
    """One long-lived asyncio connection to the broker, reconnecting with 0.5-10 s backoff.

    ``hello`` params are stored and re-sent first on every (re)connect, so a
    broker restart doesn't strand the MCP server. Calls made while no broker
    answers fail fast with ``BrokerDown``.
    """

    def __init__(self, path: str | os.PathLike, *, backoff: tuple[float, float] = (0.5, 10.0)):
        self.path = str(path)
        self.backoff = backoff
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.hello_params: dict[str, Any] | None = None
        self.hello_result: dict[str, Any] | None = None
        self.hello_error: RpcError | None = None
        self.ready = asyncio.Event()  # connected and hello answered
        self.connected = asyncio.Event()
        self.on_push: Any = None
        # awaited after every successful hello, before calls may proceed
        # (the Claude MCP server attaches its inbox channel here)
        self.after_hello: Any = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def close(self) -> None:
        self._closed = True
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._fail_pending(BrokerDown("closed"))

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _run(self) -> None:
        delay = self.backoff[0]
        while not self._closed:
            try:
                check_socket_owner(self.path)
                reader, writer = await asyncio.open_unix_connection(self.path, limit=MAX_LINE + 1)
            except (OSError, BrokerDown, PermissionError):
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.backoff[1])
                continue
            delay = self.backoff[0]
            self._writer = writer
            self.connected.set()
            reader_task = asyncio.get_running_loop().create_task(self._read(reader))
            try:
                if self.hello_params is not None:
                    await self._send_hello()
                await reader_task
            except asyncio.CancelledError:
                reader_task.cancel()
                raise
            except Exception as e:  # pragma: no cover
                log.debug("broker connection error: %s", type(e).__name__)
            finally:
                self.connected.clear()
                self.ready.clear()
                self._writer = None
                with contextlib.suppress(Exception):
                    writer.close()
                self._fail_pending(BrokerDown("broker connection lost"))

    async def _read(self, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                line = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError, ConnectionError):
                return
            if not line:
                return
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if "push" in obj:
                if self.on_push is not None:
                    with contextlib.suppress(Exception):
                        self.on_push(obj)
                continue
            fut = self._pending.pop(obj.get("id"), None)  # type: ignore[arg-type]
            if fut is None or fut.done():
                continue
            if "error" in obj:
                err = obj.get("error") or {}
                fut.set_exception(RpcError(str(err.get("code", "internal")), str(err.get("message", ""))))
            else:
                fut.set_result(obj.get("result") or {})

    async def _send_hello(self) -> None:
        try:
            self.hello_result = await self._call_now("mcp.hello", self.hello_params or {}, 10.0)
            self.hello_error = None
        except RpcError as e:
            self.hello_error = e
            self.hello_result = None
        except (BrokerDown, TimeoutError, OSError):
            return
        if self.hello_result is not None and self.after_hello is not None:
            try:
                await self.after_hello(self.hello_result)
            except Exception as e:  # never block the connection on it
                log.debug("after_hello failed: %s", type(e).__name__)
        self.ready.set()

    async def hello(self, params: dict[str, Any], timeout: float = 5.0) -> bool:
        """Store the hello and send it now if connected. True once it has been answered."""
        self.hello_params = params
        self.start()
        if self.connected.is_set() and not self.ready.is_set():
            await self._send_hello()
        try:
            await asyncio.wait_for(self.ready.wait(), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return False
        return True

    async def _call_now(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        w = self._writer
        if w is None:
            raise BrokerDown("not connected")
        rid = next(self._ids)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        line = json.dumps({"id": rid, "method": method, "params": params}, separators=(",", ":")) + "\n"
        try:
            w.write(line.encode())
            await w.drain()
        except (ConnectionError, OSError) as e:
            self._pending.pop(rid, None)
            raise BrokerDown(str(e)) from None
        try:
            return await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, TimeoutError):
            self._pending.pop(rid, None)
            raise TimeoutError(f"{method}: no answer within {timeout} s") from None
        except asyncio.CancelledError:
            self._pending.pop(rid, None)
            raise

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 15.0,
                   ready_timeout: float = 3.0) -> dict[str, Any]:
        """A request on the (hello'd) connection. Raises BrokerDown, RpcError or TimeoutError."""
        self.start()
        if not self.ready.is_set():
            try:
                await asyncio.wait_for(self.ready.wait(), ready_timeout)
            except (asyncio.TimeoutError, TimeoutError):
                raise BrokerDown("broker not reachable") from None
        if self.hello_error is not None:
            raise self.hello_error
        return await self._call_now(method, params or {}, timeout)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        """Fire-and-forget request (the answer is ignored)."""
        w = self._writer
        if w is None:
            return
        rid = next(self._ids)
        line = json.dumps({"id": rid, "method": method, "params": params}, separators=(",", ":")) + "\n"
        with contextlib.suppress(Exception):
            w.write(line.encode())
