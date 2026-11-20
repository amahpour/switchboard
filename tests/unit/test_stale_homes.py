"""The session-start sweep of stale test homes (#919, tests/conftest.py).

A killed run (SIGKILL, the OOM killer, a timeout, a cancelled job) skips ``tmp_home``'s ``finally``
and leaves its ``/tmp/yk-*`` home behind: 19,181 of them, 6 GB of a tmpfs ``/tmp`` in five days on a
shared machine. The sweep removes only what a run of ours made and nobody still uses.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import STALE_HOME_AGE_S, make_tmp_home, sweep_stale_homes

from switchboard.paths import TEST_MARKER

NOW = 1_790_000_000.0

# The sweep removes a home only once procfs shows nobody uses it; elsewhere it removes nothing.
needs_proc = pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir(),
    reason="the sweep removes homes only where procfs shows who uses them",
)


def _age(p: Path, seconds: float) -> None:
    os.utime(p, (NOW - seconds, NOW - seconds), follow_symlinks=False)


def _home(
    root: Path, name: str, *, marked: bool = True, age_s: float = 0.0, owner: str | None = None
) -> Path:
    d = root / name
    d.mkdir()
    (d / "broker.sock.d").mkdir()
    if marked:
        (d / TEST_MARKER).write_text(owner or "")
    _age(d, age_s)
    return d


@needs_proc
def test_sweep_removes_only_old_marked_own_homes(tmp_path: Path) -> None:
    """Old, marked, own homes go; a young home, an unmarked one, a symlink and other names stay.

    Fails without the sweep (the old homes stay, and the stale count stays at the killed run's), and
    fails if it follows a symlink, ignores the marker or the age, or matches more than ``yk-*``.
    """
    root = tmp_path / "tmp"
    root.mkdir()
    old = STALE_HOME_AGE_S + 60
    gone = f"{_gone_pid()}\n"
    stale = [
        _home(root, "yk-killed1", age_s=old, owner=gone),
        _home(root, "yk-killed2", age_s=old * 10, owner=gone),
    ]
    young = _home(root, "yk-running", age_s=STALE_HOME_AGE_S - 60, owner=gone)
    unmarked = _home(root, "yk-nomark-1", marked=False, age_s=old)
    other = _home(root, "pytest-of-someone", age_s=old)
    target = _home(tmp_path, "elsewhere", age_s=old)
    link = root / "yk-link"
    link.symlink_to(target, target_is_directory=True)
    _age(link, old)
    marker_file = root / "yk-file"
    marker_file.write_text("")
    _age(marker_file, old)

    removed = sweep_stale_homes(root, now=NOW)

    assert sorted(removed) == sorted(stale)
    assert not any(p.exists() for p in stale)
    for kept in (young, unmarked, other, target, marker_file):
        assert kept.exists(), kept
    assert link.is_symlink() and (target / TEST_MARKER).exists()


def test_sweep_keeps_a_home_owned_by_someone_else(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A marked, old home of another user is not ours to remove (a shared /tmp)."""
    root = tmp_path / "tmp"
    root.mkdir()
    home = _home(root, "yk-theirs", age_s=STALE_HOME_AGE_S * 2, owner=f"{_gone_pid()}\n")
    monkeypatch.setattr(os, "getuid", lambda: home.stat().st_uid + 1)
    assert sweep_stale_homes(root, now=NOW) == []
    assert home.exists()


def test_sweep_never_fails_the_run(tmp_path: Path) -> None:
    """A root that does not exist, or is not a directory, is nothing to sweep, never an error."""
    assert sweep_stale_homes(tmp_path / "missing", now=NOW) == []
    f = tmp_path / "file"
    f.write_text("")
    assert sweep_stale_homes(f, now=NOW) == []


def test_sweep_uses_the_clock_when_no_time_is_given(tmp_path: Path) -> None:
    """Without ``now`` the wall clock decides: a home made just now is young and stays."""
    root = tmp_path / "tmp"
    root.mkdir()
    d = root / "yk-now"
    d.mkdir()
    (d / TEST_MARKER).touch()
    assert time.time() - d.stat().st_mtime < STALE_HOME_AGE_S
    assert sweep_stale_homes(root) == []
    assert d.exists()


def test_sweep_runs_on_the_controller_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """``pytest_sessionstart`` sweeps once, from the controller; an xdist worker (``workerinput``) skips it.

    Fails if the hook is gone (nothing sweeps) or if every worker sweeps too (N racing sweeps).
    """
    import types

    import conftest

    calls: list[int] = []
    monkeypatch.setattr(conftest, "sweep_stale_homes", lambda: calls.append(1) or [])
    controller = types.SimpleNamespace(config=types.SimpleNamespace())
    worker = types.SimpleNamespace(config=types.SimpleNamespace(workerinput={"workerid": "gw0"}))
    conftest.pytest_sessionstart(worker)  # type: ignore[arg-type]
    assert calls == []
    conftest.pytest_sessionstart(controller)  # type: ignore[arg-type]
    assert calls == [1]


def _gone_pid() -> int:
    """The pid of a process that has exited and been reaped: started, waited for, so nothing has it."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_make_tmp_home_records_the_process_that_made_it() -> None:
    """The marker holds this process's pid, so the sweep can ask whether the home's run is still going.

    Fails on a ``make_tmp_home`` that only touches the marker (an empty marker: the age alone decides)."""
    home = make_tmp_home()
    try:
        assert (home / TEST_MARKER).read_text().strip() == str(os.getpid())
    finally:
        shutil.rmtree(home)


def test_sweep_keeps_an_old_home_whose_maker_is_alive(tmp_path: Path) -> None:
    """A home older than the cutoff whose recorded maker is still running is kept (#919, review F1).

    A broker writes to files that already exist, so the home's mtime need not move: a paused or long
    run's home looked stale and another session's sweep removed it under the running test. Fails on a
    sweep that reads only the age."""
    root = tmp_path / "tmp"
    root.mkdir()
    home = _home(root, "yk-paused", age_s=STALE_HOME_AGE_S * 4, owner=f"{os.getpid()}\n")
    assert sweep_stale_homes(root, now=NOW) == []
    assert home.exists()


@needs_proc
def test_sweep_removes_an_old_home_whose_maker_is_gone(tmp_path: Path) -> None:
    """An old home whose recorded maker has exited is removed: a killed run's homes still go."""
    root = tmp_path / "tmp"
    root.mkdir()
    home = _home(root, "yk-killed", age_s=STALE_HOME_AGE_S + 60, owner=f"{_gone_pid()}\n")
    assert sweep_stale_homes(root, now=NOW) == [home]
    assert not home.exists()


@pytest.mark.parametrize("marker", ["", "not a pid", "0", "-1", "99999999999999"])
def test_sweep_keeps_an_old_home_whose_marker_holds_no_pid(tmp_path: Path, marker: str) -> None:
    """A marker with no valid pid (an older checkout's empty one, or garbage) can't show the home's run
    is over, so an old home is kept, however old (review F1a). It replaces the test that let the age
    alone decide: an empty marker's home was removed on age, and a live run of an older checkout
    lost it. A pid of 0 or less is never handed to ``os.kill``, where it would name a process group."""
    root = tmp_path / "tmp"
    root.mkdir()
    old = _home(root, "yk-old", age_s=STALE_HOME_AGE_S * 10, owner=marker)
    assert sweep_stale_homes(root, now=NOW) == []
    assert old.exists()


def test_sweep_counts_a_maker_of_another_user_as_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os.kill(pid, 0)`` refused (``PermissionError``) means the process exists: its home stays."""
    root = tmp_path / "tmp"
    root.mkdir()
    home = _home(root, "yk-other", age_s=STALE_HOME_AGE_S * 2, owner="4242")

    def refused(pid: int, sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "kill", refused)
    assert sweep_stale_homes(root, now=NOW) == []
    assert home.exists()


@needs_proc
def test_sweep_returns_only_the_homes_it_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A home ``rmtree`` could not remove (errors ignored) is not reported as removed (review F4).

    Fails on a sweep that appends every home it tried, gone or not."""
    root = tmp_path / "tmp"
    root.mkdir()
    home = _home(root, "yk-stuck", age_s=STALE_HOME_AGE_S + 60, owner=f"{_gone_pid()}\n")
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)
    assert sweep_stale_homes(root, now=NOW) == []
    assert home.exists()


@needs_proc
def test_sweep_keeps_a_home_a_live_child_has_as_its_cwd(tmp_path: Path) -> None:
    """An old home whose maker is gone but where a process still runs (its cwd) is kept (review F1b).

    The marker's pid is the pytest process, but ``SubprocBroker`` starts a broker of its own that can
    outlive it. Fails on a sweep that asks only whether the marker's pid is alive."""
    root = tmp_path / "tmp"
    root.mkdir()
    home = _home(root, "yk-orphan", age_s=STALE_HOME_AGE_S * 2, owner=f"{_gone_pid()}\n")
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], cwd=home, stdin=subprocess.PIPE
    )
    try:
        assert sweep_stale_homes(root, now=NOW) == []
        assert home.exists()
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=30)


@needs_proc
def test_sweep_keeps_a_home_a_live_child_has_a_file_open_in(tmp_path: Path) -> None:
    """The same with the child's cwd elsewhere and a file in the home open (a broker's database): kept."""
    root = tmp_path / "tmp"
    root.mkdir()
    home = _home(root, "yk-db", age_s=STALE_HOME_AGE_S * 2, owner=f"{_gone_pid()}\n")
    db = home / "switchboard.db"
    db.write_text("")
    _age(home, STALE_HOME_AGE_S * 2)
    code = "import sys; f = open(sys.argv[1]); sys.stdin.read()"
    child = subprocess.Popen([sys.executable, "-c", code, str(db)], cwd=tmp_path, stdin=subprocess.PIPE)
    try:
        # wait for the event: the file shows among the child's fds
        fd_dir = Path(f"/proc/{child.pid}/fd")
        deadline = time.monotonic() + 30
        while not any(os.readlink(fd_dir / fd) == str(db) for fd in os.listdir(fd_dir)):
            assert time.monotonic() < deadline, "the child never opened the file"
            time.sleep(0.01)
        assert sweep_stale_homes(root, now=NOW) == []
        assert home.exists()
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=30)


@needs_proc
def test_sweep_keeps_a_home_with_a_unix_socket_bound_in_it(tmp_path: Path) -> None:
    """A Unix socket bound inside the home (a broker's ``broker.sock``) keeps it: an fd shows it only
    as ``socket:[inode]``, so the path is read from ``/proc/net/unix``."""
    import socket

    root = tmp_path / "tmp"
    root.mkdir()
    home = _home(root, "yk-sock", age_s=STALE_HOME_AGE_S * 2, owner=f"{_gone_pid()}\n")
    sock = socket.socket(socket.AF_UNIX)
    path = str(home / "b.sock")
    if len(path.encode()) > 100:
        pytest.skip("the temp path is too long for sun_path")
    try:
        sock.bind(path)
        _age(home, STALE_HOME_AGE_S * 2)
        assert sweep_stale_homes(root, now=NOW) == []
        assert home.exists()
    finally:
        sock.close()


def test_sweep_removes_nothing_without_procfs(tmp_path: Path) -> None:
    """Where there is no procfs (macOS, Windows) the sweep can't show a home unused, so it fails closed:
    an old home whose maker is gone is kept. Fails on a sweep that skips check 3 when it can't make it."""
    root = tmp_path / "tmp"
    root.mkdir()
    home = _home(root, "yk-noproc", age_s=STALE_HOME_AGE_S * 2, owner=f"{_gone_pid()}\n")
    assert sweep_stale_homes(root, now=NOW, proc=tmp_path / "no-proc") == []
    assert home.exists()


def test_sweep_removes_nothing_when_procfs_has_no_process_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A procfs that gives the pid limit but lists no process can't show who uses a home: nothing goes."""
    import conftest

    fake = tmp_path / "proc"
    (fake / "sys" / "kernel").mkdir(parents=True)
    (fake / "sys" / "kernel" / "pid_max").write_text("4194304\n")
    monkeypatch.setattr(conftest, "PROC_ROOT", fake)
    root = tmp_path / "tmp"
    root.mkdir()
    home = _home(root, "yk-empty-proc", age_s=STALE_HOME_AGE_S * 2, owner=f"{_gone_pid()}\n")
    assert sweep_stale_homes(root, now=NOW) == []
    assert home.exists()


def _pid_max() -> int:
    return int(Path("/proc/sys/kernel/pid_max").read_text().strip())


@pytest.mark.skipif(not Path("/proc/sys/kernel/pid_max").is_file(), reason="reads Linux's pid_max")
@pytest.mark.parametrize("marker", ["9999999999", "pid_max+1"])
def test_sweep_keeps_a_home_whose_marker_pid_is_beyond_the_platform_range(
    tmp_path: Path, marker: str
) -> None:
    """A marker pid above the OS's range is no pid: the home is kept, and the sweep doesn't raise (F5).

    ``os.kill(9999999999, 0)`` raises ``OverflowError``, not ``OSError``: on a sweep that took every
    ten-digit decimal for a pid it escaped ``pytest_sessionstart`` and aborted the run before any test."""
    if marker == "pid_max+1":
        marker = str(_pid_max() + 1)
    root = tmp_path / "tmp"
    root.mkdir()
    home = _home(root, "yk-huge", age_s=STALE_HOME_AGE_S * 2, owner=marker)
    assert sweep_stale_homes(root, now=NOW) == []
    assert home.exists()


@pytest.mark.parametrize(
    "error", [OverflowError("signed integer is greater than maximum"), ValueError("bad")]
)
def test_process_alive_counts_a_pid_kill_cannot_take_as_alive(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """``_process_alive`` treats ``OverflowError`` and ``ValueError`` from ``os.kill`` as alive, the side
    that keeps a home, instead of letting them out of the sweep (review F5)."""
    import conftest

    def kill(pid: int, sig: int) -> None:
        raise error

    monkeypatch.setattr(os, "kill", kill)
    assert conftest._process_alive(4242) is True


def test_sessionstart_survives_a_sweep_that_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever the sweep raises stays inside ``pytest_sessionstart``: the run goes on (review F5)."""
    import types

    import conftest

    def boom() -> list[Path]:
        raise OverflowError("signed integer is greater than maximum")

    monkeypatch.setattr(conftest, "sweep_stale_homes", boom)
    controller = types.SimpleNamespace(config=types.SimpleNamespace())
    conftest.pytest_sessionstart(controller)  # type: ignore[arg-type]
