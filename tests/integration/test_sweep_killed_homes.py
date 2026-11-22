"""The session-start sweep of the temp homes a killed run leaves (#922, tests/tmp_homes.py).

A killed run (SIGKILL, the OOM killer, a timeout) skips every cleanup and leaves its ``/tmp/yk-*``
homes; they once filled a RAM-backed /tmp. The sweep removes a home only when nobody holds its test
lock or its broker lock and no socket under it answers, and removes it in place, the marker and
the lock file last, holding the root's sweep lock. A killed run is simulated by the real thing: a
child process that made a home with ``make_locked_home`` and was SIGKILLed. Every wait here is for
an event (a line, an EOF, a process exit), bounded, never a sleep: a pipe is read with ``select``
and ``os.read`` against a deadline, never a buffered read that can block. Every child runs in a
process group of its own, and its ``finally`` kills the whole group (an orphaned grandchild holds
the same pipes) and reaps the child before any pipe is closed. Every sweep,
the nested pytest runs' included, runs against a root of the test's own, never the real /tmp
(``SWITCHBOARD_TEST_SWEEP_ROOT`` for a nested run).
"""

from __future__ import annotations

import errno
import fcntl
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import tmp_homes
from conftest import SubprocBroker, make_tmp_home
from tmp_homes import (
    BROKER_LOCK,
    BROKER_OUT,
    LOCK_NAME,
    SWEEP_LOCK,
    TEST_MARKER,
    remove_home,
    sweep_at_session_start,
    sweep_killed_homes,
)

if TYPE_CHECKING:
    from _pytest.pytester import Pytester

pytest_plugins = ["pytester"]

_TESTS_DIR = Path(__file__).resolve().parents[1]
_CONFTEST = _TESTS_DIR / "conftest.py"

# How long a pipe read may wait for a child's line or EOF: far more than a loaded machine needs.
PIPE_TIMEOUT_S = 30.0
# How long a nested pytest run may take before pytester kills it and the test fails.
INNER_RUN_TIMEOUT_S = 120.0

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

# A daemonized broker as far as the sweep can tell: holds LOCK_EX on the home's run/broker.lock
# (as broker/daemon.py does), not the test lock, and has no socket.
_BROKER_LOCK_HOLDER = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("locked", flush=True)
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


def _read(fd: int, until: Callable[[bytes], bool], what: str) -> bytes:
    """Read the raw pipe ``fd`` until ``until(data)`` or EOF, waiting at most PIPE_TIMEOUT_S in all:
    ``select`` then ``os.read``, so no read ever blocks past the deadline."""
    deadline = time.monotonic() + PIPE_TIMEOUT_S
    data = b""
    while not until(data):
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            raise AssertionError(f"no {what} within {PIPE_TIMEOUT_S}s (read {data!r})")
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        data += chunk
    return data


def _read_line(fd: int, what: str) -> str:
    data = _read(fd, lambda d: b"\n" in d, what)
    assert b"\n" in data, f"EOF before {what} (read {data!r})"
    return data.split(b"\n", 1)[0].decode()


def _read_to_eof(fd: int, what: str) -> bytes:
    return _read(fd, lambda d: False, what)


def _stop(p: subprocess.Popen[bytes]) -> None:
    """Every test's ``finally``: kill ``p``'s whole process group while ``p`` is unreaped (so its
    pid, the group's id, can't have been reused), which takes an orphaned grandchild with it; reap
    ``p``; only then close its pipes."""
    if p.returncode is None:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait(PIPE_TIMEOUT_S)
    for f in (p.stdin, p.stdout):
        if f is not None:
            f.close()


def _start(argv: list[str], stdin: int | None = None) -> tuple[subprocess.Popen[bytes], str]:
    """Start ``argv`` in a process group of its own and read its first line, bounded; a child whose
    line never comes is killed with its group."""
    p = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin is None else stdin,
        stdout=subprocess.PIPE,
        bufsize=0,
        start_new_session=True,
    )
    try:
        assert p.stdout is not None
        return p, _read_line(p.stdout.fileno(), "first line").strip()
    except BaseException:
        _stop(p)
        raise


def _start_run(root: Path, mode: str, stdin: int | None = None) -> tuple[subprocess.Popen[bytes], Path]:
    p, line = _start([sys.executable, "-c", _RUN, str(root), mode], stdin)
    home = Path(line)
    if not (home.parent == root and (home / TEST_MARKER).is_file()):
        _stop(p)
        raise AssertionError(home)
    return p, home


def _kill(p: subprocess.Popen[bytes]) -> None:
    """SIGKILL ``p`` alone (not its group) and wait until it is dead, without reaping it, so that
    ``_stop`` can still kill its group by its pid."""
    p.send_signal(signal.SIGKILL)
    info = os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOWAIT)
    assert info is not None and info.si_code == os.CLD_KILLED and info.si_status == signal.SIGKILL


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
    try:
        assert _held(home)
        _kill(p)
    finally:
        _stop(p)
    result = sweep_killed_homes(root)
    assert result.removed == [home] and not result.in_use and not result.failed
    assert not os.path.lexists(home)
    assert os.listdir(root) == [SWEEP_LOCK]  # nothing else: no remainder under any name


def test_a_home_whose_maker_is_alive_is_kept(root: Path) -> None:
    """The control for the test above: the same home while its maker still runs is kept, and is
    removed once the maker is killed. Fails if the sweep takes a home that is in use."""
    p, home = _start_run(root, "plain")
    try:
        result = sweep_killed_homes(root)
        assert result.in_use == [home] and not result.removed
        assert (home / TEST_MARKER).is_file()
        _kill(p)
    finally:
        _stop(p)
    assert sweep_killed_homes(root).removed == [home]


def test_a_home_held_by_a_child_that_outlived_its_parent_is_kept(root: Path) -> None:
    """The maker starts a child that holds the home's lock, then is SIGKILLed: the orphaned child
    still uses the home, so it stays (#920's second finding: the maker isn't the only user). Once
    the child exits it goes. Fails if liveness is the maker's alone (a pid in the marker)."""
    r, w = os.pipe()
    try:
        p, home = _start_run(root, "orphan", stdin=r)
    except BaseException:
        os.close(w)
        raise
    finally:
        os.close(r)
    try:
        try:
            _kill(p)
            assert _held(home)
            result = sweep_killed_homes(root)
            assert result.in_use == [home] and not result.removed
            assert (home / TEST_MARKER).is_file()
        finally:
            os.close(w)  # the orphan reads EOF and exits; its stdout, shared with the run, then closes
        assert p.stdout is not None and _read_to_eof(p.stdout.fileno(), "the orphan's EOF") == b""
    finally:
        _stop(p)
    assert sweep_killed_homes(root).removed == [home]


def test_a_home_whose_broker_lock_is_held_is_kept(root: Path) -> None:
    """``switchboard start`` daemonizes the broker without the test lock, and the CLI can return
    before the broker listens: a process holding the home's ``run/broker.lock`` (as daemon.py does
    from just after ``paths.ensure()``), with no test lock and no socket, keeps the home. Once it
    exits the home goes. Fails without the broker-lock check (the home is removed while held)."""
    home = _dead_home(root)
    (home / BROKER_LOCK[0]).mkdir()
    p, line = _start([sys.executable, "-c", _BROKER_LOCK_HOLDER, str(home.joinpath(*BROKER_LOCK))])
    try:
        assert line == "locked"
        result = sweep_killed_homes(root)
        assert result.in_use == [home] and not result.removed and not result.failed
        assert (home / TEST_MARKER).is_file()
    finally:
        _stop(p)
    assert sweep_killed_homes(root).removed == [home]


def test_a_home_whose_broker_is_starting_is_kept(root: Path) -> None:
    """``switchboard start`` opens ``logs/broker.out`` before it spawns the daemon, which takes
    ``run/broker.lock`` only once it runs: a home with broker.out and no broker.lock (and no test
    lock holder, no socket) is kept and counted in use. With a broker.lock nobody holds, the lock
    check decides and the home goes. Fails without the broker.out rule (the home is removed while
    a daemon may be starting in it)."""
    home = _dead_home(root)
    (home / BROKER_OUT[0]).mkdir()
    (home.joinpath(*BROKER_OUT)).write_text("")
    result = sweep_killed_homes(root)
    assert result.in_use == [home] and not result.removed and not result.failed
    assert "kept 1 in use" in result.line()
    (home / BROKER_LOCK[0]).mkdir()
    (home.joinpath(*BROKER_LOCK)).touch()
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


def test_a_home_that_cant_be_fully_inspected_is_kept(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable directory under a home could hide a live socket: a walk error (an injected
    PermissionError through ``onerror``, as ``os.fwalk`` reports a directory it can't open; root
    reads everything, so it is injected) keeps the home, and so does an ``lstat`` that fails for
    any reason but "gone". Fails if either error is ignored, as the first walk's handlers did."""
    home = _dead_home(root)
    (home / "run").mkdir()
    real_walk = tmp_homes._walk

    def walk_with_error(*args: object, **kwargs: object) -> Iterator[tuple[str, list[str], list[str], int]]:
        kwargs["onerror"](PermissionError(errno.EACCES, "Permission denied", str(home / "run")))  # type: ignore[operator]
        yield from real_walk(*args, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as m:
        m.setattr(tmp_homes, "_walk", walk_with_error)
        result = sweep_killed_homes(root)
    assert result.in_use == [home] and not result.removed and home.is_dir()

    real_lstat = tmp_homes._lstat_at

    def lstat_refused(name: str, dir_fd: int) -> os.stat_result:
        if name == "run":
            raise PermissionError(errno.EACCES, "Permission denied", name)
        return real_lstat(name, dir_fd)

    with monkeypatch.context() as m:
        m.setattr(tmp_homes, "_lstat_at", lstat_refused)
        result = sweep_killed_homes(root)
    assert result.in_use == [home] and not result.removed and home.is_dir()

    def lstat_gone(name: str, dir_fd: int) -> os.stat_result:
        if name == "run":
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", name)
        return real_lstat(name, dir_fd)

    with monkeypatch.context() as m:  # the control: an entry that vanished is no reason to keep it
        m.setattr(tmp_homes, "_lstat_at", lstat_gone)
        assert sweep_killed_homes(root).removed == [home]


def test_a_home_swapped_after_its_lock_is_taken_is_kept(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The checks and the removal are bound to the directory that was locked: if the name is moved
    aside and another home put in its place while the sweep looks inside (here, from the socket
    check), the new directory is not removed, nor is the old. Fails if the removal resolves the
    name again (the first sweep's ``rmtree(home)`` removed the newcomer)."""
    home = _dead_home(root)
    aside = root / "aside"
    real = tmp_homes.has_live_socket

    def swap(home_fd: int, path: str) -> bool:
        live = real(home_fd, path)
        os.rename(home, aside)
        home.mkdir()
        (home / TEST_MARKER).touch()
        (home / LOCK_NAME).touch()
        (home / "newcomer").touch()
        return live

    monkeypatch.setattr(tmp_homes, "has_live_socket", swap)
    result = sweep_killed_homes(root)
    assert result.in_use == [home] and not result.removed and not result.failed
    assert (home / "newcomer").is_file() and (aside / TEST_MARKER).is_file()


def _home_with_contents(root: Path) -> Path:
    home = tmp_homes.make_locked_home(root)
    for d in ("logs", "run", "zdata"):
        (home / d / "sub").mkdir(parents=True)
        (home / d / "sub" / "f").write_text("x")
    (home / "broker.toml").write_text("x")
    return home


def _rmtree_stops_after(n: int) -> Callable[[str, int], None]:
    """An ``rmtree`` of a home's directories that removes ``n`` of them, then, part way into the
    next, removes one file and raises: a removal interrupted after some entries."""
    calls = [0]

    def rmtree_at(name: str, dir_fd: int) -> None:
        calls[0] += 1
        if calls[0] > n:
            os.unlink(os.path.join(name, "sub", "f"), dir_fd=dir_fd)
            raise OSError(errno.EIO, "Input/output error", name)
        shutil.rmtree(name, dir_fd=dir_fd)

    return rmtree_at


def test_an_interrupted_removal_leaves_a_home_the_next_sweep_removes(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removal is done in place, the marker and the lock file last, so one stopped part way (here
    the second directory's ``rmtree`` fails after one file) leaves the home under its own name with
    both, and the next sweep removes it. The same holds for ``remove_home`` (the fixture's
    teardown) and for the sweep's own removal. Fails if the home is renamed first (the remainder
    has another name, and the leak check misses it) or if the marker or lock file goes before the
    rest (the remainder is no candidate and stays for good)."""
    home = _home_with_contents(root)
    with monkeypatch.context() as m:
        m.setattr(tmp_homes, "_rmtree_at", _rmtree_stops_after(1))
        remove_home(home)
    assert home not in tmp_homes._LOCKS  # its lock is let go: the sweep can take it
    assert (home / TEST_MARKER).is_file() and (home / LOCK_NAME).is_file()
    assert not (home / "logs").exists() and (home / "run").is_dir()  # some entries went, not all
    assert sorted(os.listdir(root)) == [home.name]
    result = sweep_killed_homes(root)
    assert result.removed == [home] and not result.failed and os.listdir(root) == [SWEEP_LOCK]

    swept = _home_with_contents(root)
    tmp_homes.release_home(swept)
    with monkeypatch.context() as m:
        m.setattr(tmp_homes, "_rmtree_at", _rmtree_stops_after(1))
        result = sweep_killed_homes(root)
    assert not result.removed and len(result.failed) == 1 and "OSError" in result.failed[0]
    assert (swept / TEST_MARKER).is_file() and (swept / LOCK_NAME).is_file()
    assert sweep_killed_homes(root).removed == [swept] and not os.path.lexists(swept)

    whole = tmp_homes.make_locked_home(root)
    remove_home(whole)  # the whole thing, as the tmp_home fixture does: nothing at all is left
    assert os.listdir(root) == [SWEEP_LOCK] and whole not in tmp_homes._LOCKS


def test_a_yk_gone_directory_without_a_marker_is_left(root: Path) -> None:
    """Nothing is chosen by its name: a ``yk-gone-x`` directory with no marker and no lock file (the
    old tombstones' name) is not removed, nor is one with only a lock file. Fails if any name
    prefix makes a directory removable (the tombstone sweep removed every ``yk-gone-*``)."""
    gone = root / "yk-gone-x"
    (gone / "run").mkdir(parents=True)
    (gone / "run" / "broker.db").write_text("x")
    lock_only = root / "yk-gone-y"
    lock_only.mkdir()
    (lock_only / LOCK_NAME).touch()
    result = sweep_killed_homes(root)
    assert result.removed == [] and result.in_use == [] and result.failed == []
    assert (gone / "run" / "broker.db").is_file() and (lock_only / LOCK_NAME).is_file()


def test_a_held_sweep_lock_makes_a_second_sweep_skip(root: Path) -> None:
    """One sweeper at a time: while another sweep holds the root's ``yk-sweep.lock``, a sweep removes
    nothing, says it skipped, and a dead home stays; once the lock is let go the home is swept. A
    ``yk-sweep.lock`` that is a symlink is refused, not followed, and nothing is swept. Fails without
    the sweep lock (the dead home is removed while another sweep may be removing it)."""
    home = _dead_home(root)
    fd = os.open(root / SWEEP_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lines: list[str] = []
        result = sweep_at_session_start(True, lines.append, root)
        assert result is not None and result.skipped and not result.removed and not result.in_use
        assert home.is_dir() and (home / TEST_MARKER).is_file()
        assert lines == [
            f"switchboard test homes: skipped, another sweep holds {root / SWEEP_LOCK}, "
            "nothing removed (#922)"
        ]
    finally:
        os.close(fd)
    result = sweep_killed_homes(root)
    assert not result.skipped and result.removed == [home]

    os.unlink(root / SWEEP_LOCK)
    target = root / "target"
    target.touch()
    (root / SWEEP_LOCK).symlink_to(target)
    other = _dead_home(root)
    result = sweep_killed_homes(root)
    assert not result.removed and len(result.failed) == 1 and other.is_dir()
    assert (root / SWEEP_LOCK).is_symlink()


def test_a_failed_make_leaves_nothing(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``make_locked_home`` failing at the lock file's open, at the flock or at the marker's write
    removes the directory it made, closes the fd it opened and forgets it, and the error goes on.
    Fails if a failure leaves the half-made home or an fd in ``_LOCKS``."""
    real_open = os.open
    opened: list[int] = []

    def open_failing(fail: str) -> Callable[..., int]:
        def fake(path: str | Path, *args: object, **kwargs: object) -> int:
            if Path(path).parent.parent == root and Path(path).name == fail:
                raise OSError(errno.ENOSPC, "No space left on device", str(path))
            fd = real_open(path, *args, **kwargs)  # type: ignore[arg-type]
            if Path(path).parent.parent == root and Path(path).name == LOCK_NAME:
                opened.append(fd)
            return fd

        return fake

    def flock_fails(fd: int, op: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    for what, name, value in (
        ("lock open", "open", open_failing(LOCK_NAME)),
        ("flock", "flock", flock_fails),
        ("marker", "open", open_failing(TEST_MARKER)),
    ):
        opened.clear()
        with monkeypatch.context() as m:
            if name == "flock":
                m.setattr(tmp_homes.os, "open", open_failing(""))
                m.setattr(tmp_homes.fcntl, "flock", value)
            else:
                m.setattr(tmp_homes.os, "open", value)
            with pytest.raises(OSError):
                tmp_homes.make_locked_home(root)
        assert os.listdir(root) == [], what
        assert not any(p.parent == root for p in tmp_homes._LOCKS), what
        for fd in opened:
            with pytest.raises(OSError):
                os.fstat(fd)  # closed
        assert opened or what == "lock open"


def test_a_failing_home_never_stops_the_sweep_or_raises(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An exception from one home (an injected OverflowError, as os.kill raised in #920's try) is
    noted, and the next home is still swept; the session-start wrapper writes one line and returns.
    Fails if an error escapes the sweep."""
    bad, good = _dead_home(root), _dead_home(root)
    real = tmp_homes.has_live_socket

    def flaky(home_fd: int, home: str) -> bool:
        if home == str(bad):
            raise OverflowError("signed integer is greater than maximum")
        return real(home_fd, home)

    monkeypatch.setattr(tmp_homes, "has_live_socket", flaky)
    lines: list[str] = []
    result = sweep_at_session_start(True, lines.append, root)
    assert result is not None and result.removed == [good] and bad.is_dir()
    assert len(result.failed) == 1 and "OverflowError" in result.failed[0]
    assert len(lines) == 1 and "removed 1" in lines[0] and "1 not checked" in lines[0]

    monkeypatch.setattr(tmp_homes, "sweep_killed_homes", lambda root: 1 / 0)
    assert sweep_at_session_start(True, lines.append, root) is None
    assert "the sweep failed, nothing removed: ZeroDivisionError" in lines[-1]


class _Broken:
    def write(self, s: str) -> int:
        raise OSError(errno.EPIPE, "Broken pipe")

    def flush(self) -> None:
        raise OSError(errno.EPIPE, "Broken pipe")


def test_a_failing_report_never_raises_and_is_told_apart(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When writing the line fails after a sweep that worked, the result still comes back and stderr
    says what was removed and that the report failed, not that the sweep did; when stderr fails
    too, nothing escapes. Fails if a report failure is reported as "nothing removed", or if the
    fallback's own error leaves ``pytest_sessionstart``."""
    home = _dead_home(root)

    def write_fails(line: str) -> None:
        raise OSError(errno.EIO, "Input/output error")

    result = sweep_at_session_start(True, write_fails, root)
    err = capsys.readouterr().err
    assert result is not None and result.removed == [home]
    assert "removed 1" in err and "the report failed: OSError" in err and "sweep failed" not in err

    _dead_home(root)
    monkeypatch.setattr(sys, "stderr", _Broken())
    result = sweep_at_session_start(True, write_fails, root)
    assert result is not None and len(result.removed) == 1
    monkeypatch.setattr(tmp_homes, "sweep_killed_homes", lambda root: 1 / 0)
    assert sweep_at_session_start(True, write_fails, root) is None


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
        remove_home(home)


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


def test_a_failing_sweep_never_aborts_the_run(
    pytester: Pytester, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real pytest run with the root conftest's ``pytest_sessionstart`` and a sweep that raises:
    the run still passes and says the sweep failed. Fails if the hook lets the error out (pytest
    stops with an INTERNALERROR and runs no test)."""
    monkeypatch.setenv("SWITCHBOARD_TEST_SWEEP_ROOT", str(root))
    pytester.makeconftest("BREAK = True\n" + _INNER_CONFTEST)
    pytester.makepyfile(test_one="def test_one():\n    pass\n")
    result = pytester.run(
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", timeout=INNER_RUN_TIMEOUT_S
    )
    assert result.ret == 0, result.stdout.str() + result.stderr.str()
    result.assert_outcomes(passed=1)
    assert "the sweep failed, nothing removed: OverflowError" in result.stdout.str()


def test_a_run_sweeps_a_killed_runs_home_once_under_xdist(
    pytester: Pytester, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook as the suite runs it: a dead home is removed by a run with two workers, and the
    count is written once, by the controller. The run sweeps the test's own root
    (``SWITCHBOARD_TEST_SWEEP_ROOT``), never the real /tmp, so it counts this home alone and touches
    no other session's. Fails if the hook isn't wired, or sweeps another root
    (``test_an_xdist_worker_never_sweeps`` checks that a worker doesn't sweep)."""
    monkeypatch.setenv("SWITCHBOARD_TEST_SWEEP_ROOT", str(root))
    home = _dead_home(root)
    pytester.makeconftest("BREAK = False\n" + _INNER_CONFTEST)
    pytester.makepyfile(test_one="def test_one():\n    pass\n\ndef test_two():\n    pass\n")
    result = pytester.run(
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "2", timeout=INNER_RUN_TIMEOUT_S
    )
    assert result.ret == 0, result.stdout.str() + result.stderr.str()
    assert not os.path.lexists(home) and os.listdir(root) == [SWEEP_LOCK]
    out = result.stdout.str()
    assert out.count("switchboard test homes: removed ") == 1
    assert f"switchboard test homes: removed 1 a killed run left in {root}, kept 0 in use" in out
