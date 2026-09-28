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
        capture_output=True, text=True, timeout=20,
    ).stdout
    lines = [ln for ln in out.splitlines()[1:] if ln.strip()]
    assert len(lines) == 1, out
    assert f"127.0.0.1:{subproc_broker.port} (LISTEN)" in lines[0]
    udp = subprocess.run(
        ["lsof", "-nP", "-a", "-p", str(subproc_broker.pid), "-iUDP"],
        capture_output=True, text=True, timeout=20,
    ).stdout
    assert udp.strip() == ""


def test_socket_modes(subproc_broker: SubprocBroker) -> None:
    p = subproc_broker.paths
    st = os.stat(p.sock)
    assert stat.S_ISSOCK(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600
    assert st.st_uid == os.getuid()
    for d in (p.home, p.run_dir, p.logs_dir, p.hooks_dir):
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700, d
    for f in (p.db, p.log):
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600, f


def test_in_process_getsockname(broker: InProcBroker) -> None:
    servers = broker.server.servers
    names = [s.getsockname() for srv in servers for s in srv.sockets]
    assert names == [("127.0.0.1", broker.port)]


def test_hook_copy_tamper_is_reported(tmp_home, monkeypatch) -> None:
    import json as _json
    import time as _time

    from conftest import cookie_of, ws_connect
    from switchboard.broker import app as app_mod

    monkeypatch.setattr(app_mod, "MAINTENANCE_S", 0.2)
    b = InProcBroker(tmp_home).start()
    try:
        assert b.call("sys.status")["hooks"].startswith("ok (1 copy)")
        web = b.web_client()
        ws = ws_connect(b, cookie_of(web))
        [copy] = list(b.paths.hooks_dir.glob("switchboard_hook-*.py"))
        copy.chmod(0o644)
        copy.write_text(copy.read_text() + "\n# tampered\n")
        deadline = _time.monotonic() + 5
        while True:
            f = _json.loads(ws.recv(timeout=max(0.1, deadline - _time.monotonic())))
            if f.get("t") == "notice":
                break
        assert "hook copy changed" in f["text"] and f["level"] == "warn"
        assert b.call("sys.status")["hooks"].startswith("MISMATCH")
        ws.close()
    finally:
        b.stop()


@pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof not installed")
def test_remote_adds_no_listener() -> None:
    """A link is the broker's own child on a socketpair (DESIGN.md §27.12 never-do 13): the
    broker still has one TCP listener (127.0.0.1) and one named Unix socket (broker.sock)."""
    from fakes.fake_link import FakeLink

    with FakeLink(trust=True) as link:
        b = link.broker
        sat = link.satellite_pid()
        assert sat
        tcp = subprocess.run(["lsof", "-nP", "-a", "-p", str(b.pid), "-iTCP", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=20).stdout
        rows = [ln for ln in tcp.splitlines()[1:] if ln.strip()]
        assert len(rows) == 1 and f"127.0.0.1:{b.port} (LISTEN)" in rows[0], tcp
        udp = subprocess.run(["lsof", "-nP", "-a", "-p", str(b.pid), "-iUDP"],
                             capture_output=True, text=True, timeout=20).stdout
        assert udp.strip() == ""
        unix = subprocess.run(["lsof", "-nP", "-a", "-p", str(b.pid), "-U"],
                              capture_output=True, text=True, timeout=20).stdout
        sock = os.path.realpath(link.desk_paths.sock)
        # a bound socket's NAME is its path (Linux lsof may add "type=STREAM (LISTEN)")
        named = {w for ln in unix.splitlines()[1:] for w in ln.split()[5:] if w.startswith("/")}
        assert {os.path.realpath(n) for n in named} == {sock}, unix


@pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof not installed")
def test_satellite_has_no_network_socket() -> None:
    """The satellite has no TCP or UDP socket at all (DESIGN.md §27.4.8)."""
    from fakes.fake_link import FakeLink

    with FakeLink(trust=True) as link:
        sat = link.satellite_pid()
        assert sat
        # on Linux the satellite is non-dumpable (§27.4.8): no other process of the user can list
        # its fds, lsof included, and an empty answer would prove nothing. Only trust an empty
        # -i list when lsof can see its Unix sockets (the link and the home's socket).
        # test_satellite_static.py checks the satellite's code for network sockets on every OS.
        unix = subprocess.run(["lsof", "-nP", "-a", "-p", str(sat), "-U"],
                              capture_output=True, text=True, timeout=20).stdout
        if not [ln for ln in unix.splitlines()[1:] if ln.strip()]:
            pytest.skip("lsof can't list the non-dumpable satellite's fds on this OS")
        inet = subprocess.run(["lsof", "-nP", "-a", "-p", str(sat), "-i"],
                              capture_output=True, text=True, timeout=20).stdout
        assert inet.strip() == "", inet
