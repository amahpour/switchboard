"""A ``switchboard start`` that gives up leaves nothing it launched running (#929, DESIGN.md §2).

``daemon.start()`` launches the broker in a session of its own and waits for its socket. Before
#929, a start that waited out ``START_WAIT_S`` printed "the broker did not start" and returned 1
with the child still running: no one had its pid, and a test's ``tmp_home`` teardown then removed
the home under it. These tests run ``daemon.start()`` in-process with ``subprocess.Popen`` wrapped:
the wrapper swaps in a stand-in broker (or launches the real one), waits for it to say it is ready
(the event, never a sleep), and only then hands it back, so the start's short deadline always
passes with the child alive. Each stand-in holds the write end of a pipe the test reads, and so
does every process it starts: EOF on the read end is the event that every one of them has exited.
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import tmp_homes
from conftest import child_env

from switchboard.broker import daemon
from switchboard.mcp.client import ping
from switchboard.paths import Paths

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the broker runs on POSIX only")

# A bound for each event, generous enough to hold under `-n auto`; the tests never wait it out.
EVENT_S = 30.0

# The stand-in broker: never opens the socket. argv: the pipe's write fd, "ignore-term" or "",
# the pid file to write ("" for none). It starts a grandchild in its own process group (the
# broker's), which inherits the pipe, then says "ready <grandchild pid>" and waits for a signal.
STAND_IN = r"""
import os, signal, subprocess, sys, time
fd = int(sys.argv[1])
if sys.argv[2] == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if sys.argv[3]:
    with open(sys.argv[3], "w") as f:
        f.write(f"{os.getpid()} {time.time()!r}\n")
g = subprocess.Popen([sys.executable, "-c", "import signal\nwhile True: signal.pause()"], pass_fds=(fd,))
os.write(fd, f"ready {g.pid}\n".encode())
while True:
    signal.pause()
"""


def _read_line(fd: int, what: str) -> str:
    """One line from ``fd``, by its deadline (select and os.read, never a buffered read)."""
    buf = b""
    deadline = time.monotonic() + EVENT_S
    while b"\n" not in buf:
        left = deadline - time.monotonic()
        assert left > 0, f"no {what} within {EVENT_S}s"
        r, _, _ = select.select([fd], [], [], left)
        if r:
            chunk = os.read(fd, 4096)
            assert chunk, f"the pipe closed before {what}"
            buf += chunk
    return buf.decode().split("\n", 1)[0]


def _eof(fd: int) -> bool:
    """True once every holder of the pipe's write end has exited, within the bound."""
    deadline = time.monotonic() + EVENT_S
    while (left := deadline - time.monotonic()) > 0:
        r, _, _ = select.select([fd], [], [], left)
        if r and not os.read(fd, 4096):
            return True
    return False


def _reaped(pid: int) -> bool:
    """True if ``pid`` is no child of this process any more: exited and reaped, not a zombie."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return False


class Launches:
    """What the wrapped Popen launched, and its cleanup: on main a child outlives the start."""

    def __init__(self) -> None:
        self.children: list[subprocess.Popen[bytes]] = []
        self.pipes: list[int] = []

    def pipe(self) -> tuple[int, int]:
        r, w = os.pipe()
        self.pipes += [r, w]
        return r, w

    def close_write(self, w: int) -> None:
        os.close(w)
        self.pipes.remove(w)

    def cleanup(self) -> None:
        for c in self.children:
            if c.poll() is None:
                try:
                    os.killpg(c.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                c.wait(timeout=EVENT_S)
        for fd in self.pipes:
            os.close(fd)


@pytest.fixture
def launches() -> Iterator[Launches]:
    lx = Launches()
    try:
        yield lx
    finally:
        lx.cleanup()


def _stand_in_start(
    monkeypatch: pytest.MonkeyPatch, home: Path, lx: Launches, *, ignore_term: bool, pidfile: str
) -> tuple[int, subprocess.Popen[bytes], int, int]:
    """Run ``daemon.start()`` on a stand-in broker that never opens its socket: (its return code,
    the child, the grandchild's pid, the pipe's read end)."""
    real_popen = subprocess.Popen
    r, w = lx.pipe()
    seen: dict[str, Any] = {}

    def popen(cmd: list[str], **kw: Any) -> subprocess.Popen[bytes]:
        argv = [sys.executable, "-I", "-c", STAND_IN, str(w), "ignore-term" if ignore_term else "", pidfile]
        kw["env"] = child_env()
        kw["pass_fds"] = (w, *tmp_homes.lock_fds_for(home))
        child = real_popen(argv, **kw)
        lx.children.append(child)
        lx.close_write(w)  # only the stand-in and its grandchild hold it now
        seen["grandchild"] = int(_read_line(r, "ready line").split()[1])
        return child

    monkeypatch.setattr(daemon.subprocess, "Popen", popen)
    monkeypatch.setattr(daemon, "START_WAIT_S", 0.2)
    rc = daemon.start(Paths.from_home(home), port=0, test_mode=True)
    return rc, lx.children[0], seen["grandchild"], r


def test_a_timed_out_start_stops_and_reaps_the_broker_it_launched(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, launches: Launches, capsys: pytest.CaptureFixture[str]
) -> None:
    """A start whose broker never answers stops it, and every process in its group, and reaps it.

    Fails before #929: start returned 1 with the stand-in and its grandchild still running (the
    child had no return code and was no zombie either, just alive)."""
    rc, child, _grandchild, r = _stand_in_start(
        monkeypatch, tmp_home, launches, ignore_term=False, pidfile=""
    )
    assert rc == 1
    assert "the broker did not start" in capsys.readouterr().err
    assert child.returncode == -signal.SIGTERM, "start did not stop and reap the broker it launched"
    assert _reaped(child.pid)
    assert _eof(r), "a process the broker started is still running"


def test_a_broker_that_ignores_sigterm_is_killed_and_its_pid_file_removed(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, launches: Launches
) -> None:
    """A child that doesn't exit on SIGTERM gets SIGKILL after a bounded wait, and the pid file
    it wrote naming itself (which SIGKILL gives it no chance to remove) is removed for it, so the
    home is as a fresh start expects.

    Fails before #929: the stand-in kept running and its pid file stayed."""
    paths = Paths.from_home(tmp_home)
    monkeypatch.setattr(daemon, "REAP_TERM_WAIT_S", 0.5, raising=False)  # absent before #929
    rc, child, _grandchild, r = _stand_in_start(
        monkeypatch, tmp_home, launches, ignore_term=True, pidfile=str(paths.pidfile)
    )
    assert rc == 1
    assert child.returncode == -signal.SIGKILL, "start did not kill the broker that ignored SIGTERM"
    assert _reaped(child.pid)
    assert _eof(r), "a process the broker started is still running"
    assert not paths.pidfile.exists()


def test_a_broker_up_just_after_the_deadline_is_stopped_and_a_fresh_start_works(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, launches: Launches, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real broker, fully up by the time start gives up (start's own ping is made to miss it),
    is stopped by the SIGTERM as `switchboard stop` would: it exits 0, its socket and pid file go,
    its lock is free, and a fresh start then runs a broker in the same home.

    Fails before #929: the first broker kept running, answering on the socket, so the fresh start
    found it "already running" instead of starting its own."""
    paths = Paths.from_home(tmp_home)
    real_popen = subprocess.Popen

    def popen(cmd: list[str], **kw: Any) -> subprocess.Popen[bytes]:
        kw["env"] = child_env()
        kw["pass_fds"] = tmp_homes.lock_fds_for(tmp_home)
        child = real_popen(cmd, **kw)
        launches.children.append(child)
        return child

    def broker_up() -> None:
        deadline = time.monotonic() + EVENT_S
        while ping(paths.sock, timeout=1.0) is None:
            assert launches.children[-1].poll() is None, "the broker exited before it was up"
            assert time.monotonic() < deadline, f"the broker was not up within {EVENT_S}s"
            time.sleep(0.05)  # between pings of the socket, the event waited for

    def popen_then_wait(cmd: list[str], **kw: Any) -> subprocess.Popen[bytes]:
        child = popen(cmd, **kw)
        broker_up()
        return child

    monkeypatch.setattr(daemon.subprocess, "Popen", popen_then_wait)
    monkeypatch.setattr(daemon, "START_WAIT_S", 0.2)
    monkeypatch.setattr(daemon, "ping", lambda *a, **k: None)
    rc = daemon.start(paths, port=0, test_mode=True, test_trust_uds=True)
    first = launches.children[0]
    assert rc == 1
    assert first.returncode == 0, "the broker that came up was not stopped by SIGTERM"
    assert _reaped(first.pid)
    assert ping(paths.sock) is None
    assert not paths.pidfile.exists()

    monkeypatch.setattr(daemon.subprocess, "Popen", popen)
    monkeypatch.setattr(daemon, "START_WAIT_S", EVENT_S)
    monkeypatch.setattr(daemon, "ping", ping)
    capsys.readouterr()
    assert daemon.start(paths, port=0, test_mode=True, test_trust_uds=True) == 0
    assert "switchboard is running" in capsys.readouterr().out
    second = launches.children[1]
    info = ping(paths.sock)
    assert info is not None and info["pid"] == second.pid
    assert daemon.stop(paths) == 0
    second.wait(timeout=EVENT_S)
