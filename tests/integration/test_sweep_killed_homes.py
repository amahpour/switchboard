"""The session-start sweep of the temp homes a killed run leaves (#922, tests/tmp_homes.py).

A killed run (SIGKILL, the OOM killer, a timeout) skips every cleanup and leaves its ``/tmp/yk-*``
homes; they once filled a RAM-backed /tmp. The sweep removes a home only when nobody holds its lock
and no socket under it answers. A killed run is simulated by the real thing: a child process that
made a home with ``make_locked_home`` and was SIGKILLed. Every wait here is for an event (a line, an
EOF, a process exit), never a sleep. The sweeps run against a root of their own, except the two
that check the hook in a real pytest run.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import tmp_homes
from conftest import SubprocBroker, make_tmp_home
from tmp_homes import LOCK_NAME, TEST_MARKER, sweep_at_session_start, sweep_killed_homes

if TYPE_CHECKING:
    from _pytest.pytester import Pytester

pytest_plugins = ["pytester"]

_TESTS_DIR = Path(__file__).resolve().parents[1]
_CONFTEST = _TESTS_DIR / "conftest.py"

# A run's process: makes a home exactly as make_tmp_home() does, says where, and waits on stdin.
# With "orphan" it first starts a grandchild that holds the home's lock (as SubprocBroker's broker
# does) and shares its stdin and stdout, so the grandchild outlives it when it is killed.
_RUN = f"""
import subprocess, sys
from pathlib import Path
sys.path.insert(0, {str(_TESTS_DIR)!r})
import tmp_homes
home = tmp_homes.make_locked_home(Path(sys.argv[1]))
if sys.argv[2] == "orphan":
    subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], pass_fds=tmp_homes.lock_fds_for(home)
    )
print(home, flush=True)
sys.stdin.read()
"""


@pytest.fixture
def root() -> Iterator[Path]:
    """A short root of the test's own (a socket under it must fit sun_path), not named ``yk-*``."""
    d = Path(tempfile.mkdtemp(prefix="yksweep-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)
        tmp_homes.release_removed_homes()


def _start_run(root: Path, mode: str, stdin: int | None = None) -> tuple[subprocess.Popen[str], Path]:
    p = subprocess.Popen(
        [sys.executable, "-c", _RUN, str(root), mode],
        stdin=subprocess.PIPE if stdin is None else stdin,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert p.stdout is not None
    home = Path(p.stdout.readline().strip())
    assert home.parent == root and (home / TEST_MARKER).is_file(), home
    return p, home


def _kill(p: subprocess.Popen[str]) -> None:
    p.send_signal(signal.SIGKILL)
    assert p.wait(30) == -signal.SIGKILL


def _held(home: Path) -> bool:
    """Whether some process holds the home's lock (LOCK_EX can't be had without waiting)."""
    fd = os.open(home / LOCK_NAME, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def _dead_home(root: Path) -> Path:
    """A home whose only holder (this process) has let go: what a killed run leaves."""
    home = tmp_homes.make_locked_home(root)
    tmp_homes.release_home(home)
    return home


def test_a_killed_runs_home_is_removed(root: Path) -> None:
    """A process makes a home and is SIGKILLed: no cleanup ran, the kernel dropped its lock, and
    the sweep removes the home. Fails if the sweep is missing or can't tell a dead holder."""
    p, home = _start_run(root, "plain")
    assert _held(home)
    _kill(p)
    result = sweep_killed_homes(root)
    assert result.removed == [home] and not result.in_use and not result.failed
    assert not os.path.lexists(home)


def test_a_home_whose_maker_is_alive_is_kept(root: Path) -> None:
    """The control for the test above: the same home while its maker still runs is kept, and is
    removed once the maker is killed. Fails if the sweep takes a home that is in use."""
    p, home = _start_run(root, "plain")
    try:
        result = sweep_killed_homes(root)
        assert result.in_use == [home] and not result.removed
        assert (home / TEST_MARKER).is_file()
    finally:
        _kill(p)
    assert sweep_killed_homes(root).removed == [home]


def test_a_home_held_by_a_child_that_outlived_its_parent_is_kept(root: Path) -> None:
    """The maker starts a child that holds the home's lock, then is SIGKILLed: the orphaned child
    still uses the home, so it stays (#920's second finding: the maker isn't the only user). Once
    the child exits it goes. Fails if liveness is the maker's alone (a pid in the marker)."""
    r, w = os.pipe()
    try:
        p, home = _start_run(root, "orphan", stdin=r)
    finally:
        os.close(r)
    try:
        _kill(p)
        assert _held(home)
        result = sweep_killed_homes(root)
        assert result.in_use == [home] and not result.removed
        assert (home / TEST_MARKER).is_file()
    finally:
        os.close(w)  # the orphan reads EOF and exits; its stdout, shared with the run, then closes
    assert p.stdout is not None and p.stdout.read() == ""
    p.stdout.close()
    assert sweep_killed_homes(root).removed == [home]


def test_a_home_with_a_live_socket_and_no_lock_holder_is_kept(root: Path) -> None:
    """A daemonized broker (broker/daemon.py) doesn't inherit the lock and can outlive pytest: a home
    with a listening socket under it is kept although nobody holds the lock. Once the socket is
    closed (the file stays, a connect is refused) the home goes. Fails if only the lock counts."""
    home = _dead_home(root)
    (home / "run").mkdir()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(str(home / "run" / "broker.sock"))
        s.listen(8)
        result = sweep_killed_homes(root)
        assert result.in_use == [home] and not result.removed
    finally:
        s.close()
    assert (home / "run" / "broker.sock").exists()
    assert sweep_killed_homes(root).removed == [home]


def test_a_home_without_both_the_lock_file_and_the_marker_is_left(root: Path) -> None:
    """A home with the marker and no lock file (made before #922, or not by make_tmp_home) and one
    with the lock file and no marker (mid-creation) are not candidates, nor is a ``yk-*`` symlink
    to a dead home or a directory not named ``yk-*``. Fails if the sweep removes what it can't prove
    unused."""
    old = Path(tempfile.mkdtemp(prefix="yk-", dir=root))
    (old / TEST_MARKER).touch()
    making = Path(tempfile.mkdtemp(prefix="yk-", dir=root))
    (making / LOCK_NAME).touch()
    other = root / "not-a-home"
    other.mkdir()
    (other / TEST_MARKER).touch()
    (other / LOCK_NAME).touch()
    target = _dead_home(Path(tempfile.mkdtemp(dir=root)))
    link = root / "yk-link"
    link.symlink_to(target)
    result = sweep_killed_homes(root)
    assert result.removed == [] and result.in_use == [] and result.failed == []
    assert all(d.is_dir() for d in (old, making, other, target)) and link.is_symlink()


def test_a_failing_home_never_stops_the_sweep_or_raises(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An exception from one home (an injected OverflowError, as os.kill raised in #920's try) is
    noted, and the next home is still swept; the session-start wrapper writes one line and returns.
    Fails if an error escapes the sweep."""
    bad, good = _dead_home(root), _dead_home(root)
    real = tmp_homes.has_live_socket

    def flaky(home: Path) -> bool:
        if home == bad:
            raise OverflowError("signed integer is greater than maximum")
        return real(home)

    monkeypatch.setattr(tmp_homes, "has_live_socket", flaky)
    lines: list[str] = []
    result = sweep_at_session_start(True, lines.append, root)
    assert result is not None and result.removed == [good] and bad.is_dir()
    assert len(result.failed) == 1 and "OverflowError" in result.failed[0]
    assert len(lines) == 1 and "removed 1" in lines[0] and "1 not checked" in lines[0]

    monkeypatch.setattr(tmp_homes, "sweep_killed_homes", lambda root: 1 / 0)
    assert sweep_at_session_start(True, lines.append, root) is None
    assert "the sweep failed, nothing removed: ZeroDivisionError" in lines[-1]


def test_an_xdist_worker_never_sweeps(root: Path) -> None:
    """Only the controller sweeps, once a run: a worker leaves even a dead home alone."""
    home = _dead_home(root)
    assert sweep_at_session_start(False, print, root) is None
    assert home.is_dir()


def test_a_subproc_broker_holds_its_homes_lock() -> None:
    """``SubprocBroker`` passes the home's lock to its broker, so a broker that outlives pytest keeps
    its home locked: with this process's own lock let go, the broker still holds it until it is
    killed. Fails without ``pass_fds`` in SubprocBroker.start."""
    home = make_tmp_home()
    try:
        b = SubprocBroker(home).start()
        try:
            tmp_homes.release_home(home)
            assert _held(home)
        finally:
            b.kill()
        assert not _held(home)
    finally:
        shutil.rmtree(home, ignore_errors=True)
        tmp_homes.release_home(home)


_INNER_CONFTEST = f"""
import importlib.util
import sys

_spec = importlib.util.spec_from_file_location("sb_root_conftest", {str(_CONFTEST)!r})
sb = importlib.util.module_from_spec(_spec)
sys.modules["sb_root_conftest"] = sb
_spec.loader.exec_module(sb)
import tmp_homes

if BREAK:
    def _boom(root=None):
        raise OverflowError("signed integer is greater than maximum")

    tmp_homes.sweep_killed_homes = _boom
pytest_sessionstart = sb.pytest_sessionstart
"""


def test_a_failing_sweep_never_aborts_the_run(pytester: Pytester) -> None:
    """A real pytest run with the root conftest's ``pytest_sessionstart`` and a sweep that raises:
    the run still passes and says the sweep failed. Fails if the hook lets the error out (pytest
    stops with an INTERNALERROR and runs no test)."""
    pytester.makeconftest("BREAK = True\n" + _INNER_CONFTEST)
    pytester.makepyfile(test_one="def test_one():\n    pass\n")
    result = pytester.run(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider")
    assert result.ret == 0, result.stdout.str() + result.stderr.str()
    result.assert_outcomes(passed=1)
    assert "the sweep failed, nothing removed: OverflowError" in result.stdout.str()


def test_a_run_sweeps_a_killed_runs_home_in_tmp_once_under_xdist(pytester: Pytester) -> None:
    """The hook as the suite runs it: a dead ``/tmp/yk-*`` home is removed by a run with two
    workers, and the count is written once, by the controller. Fails if the hook isn't wired
    (``test_an_xdist_worker_never_sweeps`` checks that a worker doesn't sweep)."""
    home = _dead_home(tmp_homes.TMP_HOME_ROOT)
    try:
        pytester.makeconftest("BREAK = False\n" + _INNER_CONFTEST)
        pytester.makepyfile(test_one="def test_one():\n    pass\n\ndef test_two():\n    pass\n")
        result = pytester.run(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "2")
        assert result.ret == 0, result.stdout.str() + result.stderr.str()
        assert not os.path.lexists(home)
        assert result.stdout.str().count("switchboard test homes: removed ") == 1
    finally:
        shutil.rmtree(home, ignore_errors=True)
