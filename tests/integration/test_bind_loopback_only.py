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
