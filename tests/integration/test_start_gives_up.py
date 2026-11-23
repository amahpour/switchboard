"""What a ``switchboard start`` that gives up does with the broker it launched (#929, DESIGN.md §2).

``daemon.start()`` launches the broker in a session of its own and waits for its socket. A start
that waits out ``START_WAIT_S`` leaves the broker running and names its pid: it may still be
applying a restore or migrating a large database, which ``run_foreground`` does before it serves the
socket, and stopping it there would mean it never comes up. A start the person cancels (Ctrl-C)
is not handled here: the broker is left running as before #929, and ``switchboard stop`` stops it.
These tests run ``daemon.start()`` in-process with ``subprocess.Popen`` wrapped: the wrapper swaps
in a stand-in broker (or launches the real one), waits for it to say it is ready (the event, never a
sleep), and only then hands it back, so the start's short deadline always passes with the child
alive. Each stand-in holds the write end of a pipe the test reads, and so does every process it
starts: EOF on the read end is the event that every one of them has exited.
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

# The stand-in broker: never opens the socket. argv: the pipe's write fd. It starts a grandchild in
# its own process group (the broker's), which inherits the pipe, then says "ready <grandchild pid>"
# and waits for a signal.
STAND_IN = r"""
import os, signal, subprocess, sys
fd = int(sys.argv[1])
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


class Launches:
    """What the wrapped Popen launched, and its cleanup: a child a test didn't stop outlives the
    start, so this stops each one, its whole group, before ``tmp_home`` is removed."""

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
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    lx: Launches,
) -> tuple[int, subprocess.Popen[bytes], int, int]:
    """Run ``daemon.start()`` on a stand-in broker that never opens its socket: (its return code, the
    child, the grandchild's pid, the pipe's read end)."""
    real_popen = subprocess.Popen
    r, w = lx.pipe()
    seen: dict[str, Any] = {}

    def popen(cmd: list[str], **kw: Any) -> subprocess.Popen[bytes]:
        argv = [sys.executable, "-I", "-c", STAND_IN, str(w)]
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


def test_a_timed_out_start_leaves_the_broker_running_and_names_it(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, launches: Launches, capsys: pytest.CaptureFixture[str]
) -> None:
    """A start whose broker doesn't answer in time returns 1 and leaves it running, since it may be
    migrating a large database, and says so with its pid and how to check and stop it. The test
    then stops it itself and sees every process in its group exit.

    Fails on origin/main: the start said "the broker did not start" and named no pid, so the
    assertion on the pid in its message fails."""
    rc, child, _grandchild, r = _stand_in_start(monkeypatch, tmp_home, launches)
    assert rc == 1
    err = capsys.readouterr().err
    assert f"the broker (pid {child.pid}) did not answer" in err, err
    assert "may still be starting" in err and "migrating a large database" in err, err
    assert "`switchboard status`" in err and "`switchboard stop`" in err, err
    assert "the broker did not start" not in err, err
    assert child.poll() is None, "start stopped a broker that may still be starting"
    os.killpg(child.pid, signal.SIGTERM)  # the test's own stop, while the leader is unreaped
    assert child.wait(timeout=EVENT_S) == -signal.SIGTERM
    assert _eof(r), "a process the broker started is still running"


def test_a_broker_up_just_after_the_deadline_stays_up_and_stop_stops_it(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, launches: Launches, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real broker, fully up by the time start gives up (start's own ping is made to miss it),
    is left running: start returns 1 naming its pid, the broker then answers on its socket, and a
    later `switchboard stop` stops it (socket and pid file gone).

    Fails on origin/main: start's message did not name the broker's pid ("the broker did not
    start"), so the assertion on the pid in its message fails."""
    paths = Paths.from_home(tmp_home)
    real_popen = subprocess.Popen

    def broker_up(child: subprocess.Popen[bytes]) -> None:
        deadline = time.monotonic() + EVENT_S
        while ping(paths.sock, timeout=1.0) is None:
            assert child.poll() is None, "the broker exited before it was up"
            assert time.monotonic() < deadline, f"the broker was not up within {EVENT_S}s"
            time.sleep(0.05)  # between pings of the socket, the event waited for

    def popen_then_wait(cmd: list[str], **kw: Any) -> subprocess.Popen[bytes]:
        kw["env"] = child_env()
        kw["pass_fds"] = tmp_homes.lock_fds_for(tmp_home)
        child = real_popen(cmd, **kw)
        launches.children.append(child)
        broker_up(child)
        return child

    monkeypatch.setattr(daemon.subprocess, "Popen", popen_then_wait)
    monkeypatch.setattr(daemon, "START_WAIT_S", 0.2)
    monkeypatch.setattr(daemon, "ping", lambda *a, **k: None)
    rc = daemon.start(paths, port=0, test_mode=True, test_trust_uds=True)
    first = launches.children[0]
    assert rc == 1
    assert f"the broker (pid {first.pid}) did not answer" in capsys.readouterr().err
    assert first.poll() is None, "start stopped the broker that came up"
    info = ping(paths.sock)
    assert info is not None and info["pid"] == first.pid
    # Put the real Popen and ping back before stop: on macOS stop runs `ps` to check the pid
    # (Linux uses a pidfd), and the wrapper above would take that `ps` for the broker.
    monkeypatch.setattr(daemon.subprocess, "Popen", real_popen)
    monkeypatch.setattr(daemon, "ping", ping)
    assert daemon.stop(paths) == 0
    assert first.wait(timeout=EVENT_S) == 0, "the broker did not exit cleanly on `switchboard stop`"
    assert ping(paths.sock) is None
    assert not paths.pidfile.exists()
