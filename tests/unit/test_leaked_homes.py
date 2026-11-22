"""The guard that fails a run which leaves a temp home behind (#919, tests/conftest.py).

A ``World`` that stopped its broker but never removed its ``make_tmp_home()`` left 109 marked
``/tmp/yk-*`` homes after every full run, and nothing failed. ``no_homes_left_behind`` (session,
autouse) checks, once every fixture is torn down, that each home this process made is gone, and
fails the run naming the ones that are not. Here a real pytest run, in a subprocess and under
xdist, loads that very fixture and ``make_tmp_home`` from tests/conftest.py.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from conftest import TMP_HOME_PREFIX, TMP_HOME_ROOT, homes_left_behind

from switchboard.paths import TEST_MARKER

if TYPE_CHECKING:
    from _pytest.pytester import Pytester, RunResult

pytest_plugins = ["pytester"]

# How long an inner pytest run may take before pytester kills it and the test fails.
INNER_RUN_TIMEOUT_S = 120.0

_CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"

# The inner run's conftest: the real fixture and make_tmp_home, from this suite's conftest loaded
# under a name of its own (the inner project's conftest is ``conftest`` itself).
_INNER_CONFTEST = f"""
import importlib.util
import sys

_spec = importlib.util.spec_from_file_location("sb_root_conftest", {str(_CONFTEST)!r})
sb = importlib.util.module_from_spec(_spec)
sys.modules["sb_root_conftest"] = sb
_spec.loader.exec_module(sb)
no_homes_left_behind = sb.no_homes_left_behind
tmp_home = sb.tmp_home
"""

_TESTS = """
import shutil
from pathlib import Path

from sb_root_conftest import make_tmp_home

NOTE = Path(__file__).with_name("homes.txt")


def _note(home):
    with NOTE.open("a") as f:
        f.write(f"{home}\\n")


def test_removes_its_home():
    home = make_tmp_home()
    _note(home)
    shutil.rmtree(home)


def test_leaks_its_home():
    if not LEAK:
        return
    _note(make_tmp_home())
"""


def _run(
    pytester: Pytester, *, leak: bool, workers: int = 0, tests: str = _TESTS
) -> tuple[RunResult, list[Path]]:
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(test_homes=f"LEAK = {leak!r}\n" + tests)
    args = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if workers:
        args += ["-n", str(workers)]
    result = pytester.run(*args, timeout=INNER_RUN_TIMEOUT_S)
    note = pytester.path / "homes.txt"
    homes = [Path(p) for p in note.read_text().split()] if note.exists() else []
    return result, homes


def _clean(homes: list[Path]) -> None:
    for h in homes:
        if h.parent == TMP_HOME_ROOT and h.name.startswith(TMP_HOME_PREFIX):
            shutil.rmtree(h, ignore_errors=True)


def test_a_leaked_home_fails_the_run_and_is_named(pytester: Pytester) -> None:
    """A test that makes a home and never removes it: every test passes, then the session teardown
    errors and the run exits non-zero, naming the home and the test that made it. Fails if the guard
    is gone, runs before the fixtures that remove homes, or doesn't name what leaked."""
    result, homes = _run(pytester, leak=True)
    try:
        assert len(homes) == 2
        leaked = homes[1]
        assert leaked.exists() and not homes[0].exists()
        assert result.ret != 0, result.stdout.str()
        result.assert_outcomes(passed=2, errors=1)
        out = result.stdout.str()
        assert "1 temp home(s) made by make_tmp_home() were not removed" in out
        assert f"{leaked}  made by test_homes.py::test_leaks_its_home" in out
        assert str(homes[0]) not in out
    finally:
        _clean(homes)


def test_a_leaked_home_fails_a_run_under_xdist(pytester: Pytester) -> None:
    """The same leak with two workers: the worker that made it reports it and the run fails, as the
    suite runs in CI (-n). Fails if the guard only works in a single process."""
    result, homes = _run(pytester, leak=True, workers=2)
    try:
        assert result.ret != 0, result.stdout.str()
        assert "were not removed" in result.stdout.str()
        leaked = [h for h in homes if h.exists()]
        assert (
            len(leaked) == 1
            and f"{leaked[0]}  made by test_homes.py::test_leaks_its_home" in result.stdout.str()
        )
    finally:
        _clean(homes)


# A test whose tmp_home teardown fails part way: the removal's rmtree of the home's "run" removes one
# file, then raises (as a disk error or a kill would stop it), for the rest of the inner run.
_FAILING_CLEANUP = """
import errno
import os

import tmp_homes


def _stops(name, dir_fd):
    os.unlink(os.path.join(name, "f"), dir_fd=dir_fd)
    raise OSError(errno.EIO, "Input/output error", name)


def test_cleanup_fails_part_way(tmp_home):
    with open(os.path.join(os.path.dirname(__file__), "homes.txt"), "a") as f:
        f.write(f"{tmp_home}\\n")
    (tmp_home / "run").mkdir()
    (tmp_home / "run" / "f").write_text("x")
    (tmp_home / "run" / "g").write_text("x")
    tmp_homes._rmtree_at = _stops
"""


def test_a_fixture_cleanup_that_fails_part_way_is_caught(pytester: Pytester) -> None:
    """``tmp_home``'s teardown removes its home in place (tmp_homes.remove_home), so when that removal
    fails part way the home stays under its own name, with its marker and lock file, and
    ``no_homes_left_behind`` fails the run naming it. Fails if the removal renames the home first (the
    remainder has another name, nothing counts it, and the run passes)."""
    result, homes = _run(pytester, leak=False, tests=_FAILING_CLEANUP)
    try:
        assert len(homes) == 1
        (home,) = homes
        assert (home / TEST_MARKER).is_file() and (home / "run").is_dir()
        assert result.ret != 0, result.stdout.str()
        result.assert_outcomes(passed=1, errors=1)
        out = result.stdout.str()
        assert "1 temp home(s) made by make_tmp_home() were not removed" in out
        assert f"{home}  made by test_homes.py::test_cleanup_fails_part_way" in out
    finally:
        _clean(homes)


def test_removed_homes_and_another_runs_home_pass(pytester: Pytester) -> None:
    """The control: when every home is removed the run passes, and a marked home some other run
    made, present all along, is never counted. Without this the tests above check nothing."""
    theirs = Path(tempfile.mkdtemp(prefix=TMP_HOME_PREFIX, dir=TMP_HOME_ROOT))
    (theirs / TEST_MARKER).touch()
    try:
        result, homes = _run(pytester, leak=False)
        _clean(homes)
        assert result.ret == 0, result.stdout.str()
        result.assert_outcomes(passed=2, errors=0)
        assert str(theirs) not in result.stdout.str()
    finally:
        shutil.rmtree(theirs, ignore_errors=True)


def test_homes_left_behind_counts_only_what_is_still_there(tmp_path: Path) -> None:
    """The check itself: a home still present is reported with the test that made it, a removed one
    is not, and a symlink left in a home's place still counts. Fails if it reports a removed home or
    misses one that stayed."""
    stayed, gone, link = tmp_path / "yk-stayed", tmp_path / "yk-gone", tmp_path / "yk-link"
    stayed.mkdir()
    os.symlink(tmp_path / "nowhere", link)
    made = {stayed: "t.py::a", gone: "t.py::b", link: "t.py::c"}
    assert homes_left_behind(made) == {stayed: "t.py::a", link: "t.py::c"}
    assert homes_left_behind({gone: "t.py::b"}) == {}
