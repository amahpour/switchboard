"""mcp/client.py's Stream waits without select(), so an fd above FD_SETSIZE works
(Linux support, DESIGN.md §25: containers default to a 1,048,576 open-file limit)."""

from __future__ import annotations

import fcntl
import resource
import socket
from collections.abc import Iterator

import pytest

from switchboard.mcp.client import BrokerDown, Stream

HIGH_FD = 1100  # above FD_SETSIZE (1024), where select.select() raises ValueError


@pytest.fixture
def high_fd_pair() -> Iterator[tuple[socket.socket, Stream]]:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = HIGH_FD + 100
    if soft != resource.RLIM_INFINITY and soft < want:
        if hard != resource.RLIM_INFINITY and hard < want:
            pytest.skip(f"open-file hard limit {hard} is below {want}")
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    a, b = socket.socketpair()
    hi = fcntl.fcntl(b.fileno(), fcntl.F_DUPFD, HIGH_FD)
    b.close()
    st = Stream.__new__(Stream)
    st.sock, st.buf, st._next_id, st.pending_pushes = socket.socket(fileno=hi), b"", 1, []
    try:
        yield a, st
    finally:
        st.close()
        a.close()
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_read_obj_on_an_fd_above_fd_setsize(high_fd_pair: tuple[socket.socket, Stream]) -> None:
    peer, st = high_fd_pair
    assert st.sock.fileno() >= HIGH_FD
    assert st.read_obj(0.05) is None  # a timeout, not "filedescriptor out of range in select()"
    peer.sendall(b'{"id": 1, "result": {"ok": true}}\n{"push": "x"}\n')
    assert st.read_obj(2.0) == {"id": 1, "result": {"ok": True}}
    assert st.read_obj(None) == {"push": "x"}  # no deadline: blocks until a line
    assert st.sock.gettimeout() is None  # left blocking for later sends, as before
    peer.sendall(b'{"id": 1, "result": {"pong": true}}\n')  # the answer to the call below, queued
    assert st.call("sys.ping", {}, timeout=2.0) == {"pong": True}
    assert b'"method": "sys.ping"' in peer.recv(4096)
    peer.close()
    with pytest.raises(BrokerDown):
        st.read_obj(2.0)
