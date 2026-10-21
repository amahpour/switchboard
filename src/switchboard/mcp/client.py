"""Broker clients over the Unix socket (DESIGN.md §5.1).

``call_sync``/``Stream`` serve the CLI. Before connecting, every client checks
that the socket belongs to the current user. (The module keeps its name from the
days when the per-session MCP server was a client too: it served agents with an
asyncio ``BrokerConn`` until 0.38.0, #385.)
"""

from __future__ import annotations

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

    def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 10.0
    ) -> dict[str, Any]:
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
