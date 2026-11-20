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


def test_sweep_removes_only_old_marked_own_homes(tmp_path: Path) -> None:
    """Old, marked, own homes go; a young home, an unmarked one, a symlink and other names stay.

    Fails without the sweep (the old homes stay, and the stale count stays at the killed run's), and
    fails if it follows a symlink, ignores the marker or the age, or matches more than ``yk-*``.
    """
    root = tmp_path / "tmp"
    root.mkdir()
    old = STALE_HOME_AGE_S + 60
    stale = [_home(root, "yk-killed1", age_s=old), _home(root, "yk-killed2", age_s=old * 10)]
    young = _home(root, "yk-running", age_s=STALE_HOME_AGE_S - 60)
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
    home = _home(root, "yk-theirs", age_s=STALE_HOME_AGE_S * 2)
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


def test_sweep_removes_an_old_home_whose_maker_is_gone(tmp_path: Path) -> None:
    """An old home whose recorded maker has exited is removed: a killed run's homes still go."""
    root = tmp_path / "tmp"
    root.mkdir()
    home = _home(root, "yk-killed", age_s=STALE_HOME_AGE_S + 60, owner=f"{_gone_pid()}\n")
    assert sweep_stale_homes(root, now=NOW) == [home]
    assert not home.exists()


@pytest.mark.parametrize("marker", ["", "not a pid", "0", "-1", "99999999999999"])
def test_sweep_falls_back_to_the_age_for_a_marker_without_a_pid(tmp_path: Path, marker: str) -> None:
    """A marker with no usable pid (an older checkout's empty one, or garbage) leaves the age to decide:
    an old home goes, a young one stays. A pid of 0 or less is never handed to ``os.kill``, where it
    would name a process group and read as alive."""
    root = tmp_path / "tmp"
    root.mkdir()
    old = _home(root, "yk-old", age_s=STALE_HOME_AGE_S + 60, owner=marker)
    young = _home(root, "yk-young", age_s=STALE_HOME_AGE_S - 60, owner=marker)
    assert sweep_stale_homes(root, now=NOW) == [old]
    assert young.exists()


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


def test_sweep_returns_only_the_homes_it_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A home ``rmtree`` could not remove (errors ignored) is not reported as removed (review F4).

    Fails on a sweep that appends every home it tried, gone or not."""
    root = tmp_path / "tmp"
    root.mkdir()
    home = _home(root, "yk-stuck", age_s=STALE_HOME_AGE_S + 60)
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)
    assert sweep_stale_homes(root, now=NOW) == []
    assert home.exists()
