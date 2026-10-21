"""The broker listens on 127.0.0.1 and a 0600 Unix socket, nothing else (DESIGN.md §11 #9)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess

import pytest
from conftest import InProcBroker, SubprocBroker


@pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof not installed")
def test_lsof_shows_exactly_one_loopback_listener(subproc_broker: SubprocBroker) -> None:
    out = subprocess.run(
        ["lsof", "-nP", "-a", "-p", str(subproc_broker.pid), "-iTCP", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout
    lines = [ln for ln in out.splitlines()[1:] if ln.strip()]
    assert len(lines) == 1, out
    assert f"127.0.0.1:{subproc_broker.port} (LISTEN)" in lines[0]
    udp = subprocess.run(
        ["lsof", "-nP", "-a", "-p", str(subproc_broker.pid), "-iUDP"],
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout
    assert udp.strip() == ""


def test_socket_modes(subproc_broker: SubprocBroker) -> None:
    p = subproc_broker.paths
    st = os.stat(p.sock)
    assert stat.S_ISSOCK(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600
    assert st.st_uid == os.getuid()
    for d in (p.home, p.run_dir, p.logs_dir):
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700, d
    for f in (p.db, p.log):
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600, f


def test_in_process_getsockname(broker: InProcBroker) -> None:
    servers = broker.server.servers
    names = [s.getsockname() for srv in servers for s in srv.sockets]
    assert names == [("127.0.0.1", broker.port)]
