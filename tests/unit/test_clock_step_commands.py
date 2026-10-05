"""A Linux clock step must not hide a live dialer or broker (issue #60, DESIGN.md §31.7)."""

from __future__ import annotations

import io
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from switchboard.broker import daemon, proc
from switchboard.paths import Paths
from switchboard.remote import join, satellite
from switchboard.remote.config import write_satellite_conf

LINUX = sys.platform.startswith("linux")


def stepped_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Paths, int, float]:
    paths = Paths.from_home(tmp_path)
    paths.ensure()
    write_satellite_conf(paths, "machine", broker_url="https://sb.example.com", broker_key="A" * 43)
    (paths.home / "link").mkdir(exist_ok=True)
    (paths.home / "link" / "id_ed25519").write_text("test key")
    now = [proc.read_linux_btime()]
    bid = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    satellite.boot_time(paths, bid, now[0])
    monkeypatch.setattr(proc, "_BTIME_PIN", now[0])
    me = proc.info(os.getpid())
    assert me is not None
    pid, start = me.pid, me.start
    (paths.run_dir / "dialer.pid").write_text(f"{pid} {start!r}\n")
    (paths.run_dir / "dialer.state").write_text(json.dumps({"state": "up", "pid": pid}))
    monkeypatch.setattr(proc, "_BTIME_PIN", None)
    now[0] += 4.0  # WSL2 adjusted btime after the pidfile was written
    monkeypatch.setattr(proc, "read_linux_btime", lambda: now[0])
    monkeypatch.setattr(proc, "_linux_btime", lambda: now[0])
    monkeypatch.setattr(proc, "argv", lambda p, s: "switchboard start --foreground")
    return paths, pid, start


@pytest.mark.skipif(not LINUX, reason="/proc btime: Linux")
@pytest.mark.parametrize("command", ("status", "start", "stop", "remote remove", "remote join"))
def test_dialer_commands_keep_the_same_process_after_clock_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """Each command must see the dialer recorded before btime moved, without starting another."""
    paths, pid, _ = stepped_home(tmp_path, monkeypatch)
    out = io.StringIO()
    if command == "status":
        assert f"dialer for machine: up, pid {pid}" in join.status_lines(paths)[0]
    elif command == "start":
        monkeypatch.setattr(
            daemon.subprocess, "Popen", lambda *a, **k: pytest.fail("started a second dialer")
        )
        assert daemon.start_dialer(paths, out=out) == 0
        assert "already runs" in out.getvalue()
    elif command == "remote join":
        assert f"dialer runs (pid {pid})" in (join.home_problem(paths) or "")
    else:
        kills: list[tuple[int, int]] = []

        def stop(p: int, sig: int) -> None:
            kills.append((p, sig))
            (paths.run_dir / "dialer.pid").unlink()

        monkeypatch.setattr(daemon.os, "kill", stop)
        if command == "stop":
            assert daemon.stop_dialer(paths, out=out) == 0
        else:
            assert join.leave(paths, "machine", yes=True, out=out) == 0
        assert kills == [(pid, signal.SIGTERM)]


@pytest.mark.skipif(not LINUX, reason="/proc btime: Linux")
def test_broker_sigterm_fallback_still_matches_its_pidfile_after_clock_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a dead socket, stop may signal only the broker recorded before btime moved."""
    paths, pid, start = stepped_home(tmp_path, monkeypatch)
    gone = [False]
    real_info = proc.info
    monkeypatch.setattr(proc, "info", lambda p: None if gone[0] else real_info(p))
    killed: list[tuple[int, int]] = []

    def stop(p: int, sig: int) -> None:
        killed.append((p, sig))
        gone[0] = True

    monkeypatch.setattr(daemon.os, "kill", stop)
    out = io.StringIO()
    assert daemon._sigterm_fallback(paths, (pid, start), out) == 0
    assert killed == [(pid, signal.SIGTERM)]
    assert "stopped (SIGTERM)" in out.getvalue()
