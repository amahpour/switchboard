"""A Linux clock step must not hide a live broker (issue #60)."""

from __future__ import annotations

import io
import os
import signal
import sys
from pathlib import Path

import pytest

from switchboard.broker import daemon, proc
from switchboard.paths import Paths

LINUX = sys.platform.startswith("linux")


def stepped_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Paths, int, float]:
    paths = Paths.from_home(tmp_path)
    paths.ensure()
    now = [proc.read_linux_btime()]
    bid = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    proc.boot_time(paths, bid, now[0])
    monkeypatch.setattr(proc, "_BTIME_PIN", now[0])
    me = proc.info(os.getpid())
    assert me is not None
    pid, start = me.pid, me.start
    monkeypatch.setattr(proc, "_BTIME_PIN", None)
    now[0] += 4.0  # WSL2 adjusted btime after the pidfile was written
    monkeypatch.setattr(proc, "read_linux_btime", lambda: now[0])
    monkeypatch.setattr(proc, "_linux_btime", lambda: now[0])
    return paths, pid, start


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
