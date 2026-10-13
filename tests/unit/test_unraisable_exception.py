"""A leaked object fails the test that leaked it, not a later one (issue #277, DESIGN.md §12).

Before #277, an object whose ``__del__`` raised (a leaked asyncio transport, #276) only ever
printed a ``PytestUnraisableExceptionWarning`` — in whichever test the cyclic garbage collector
happened to next run in, since nothing forced a collection at a known point. A warning doesn't
fail anything, so the suite stayed green and the leak drifted between tests.

``pyproject.toml`` now turns that warning into ``error::pytest.PytestUnraisableExceptionWarning``,
and ``tests/conftest.py``'s ``fail_on_unraisable_exception`` autouse fixture collects generations
0 and 1 after every test, so an object a test leaves unreachable is reclaimed, and any exception
its ``__del__`` raises is reported (now as an error), inside that same test's own teardown.

This is checked against a real, isolated pytest run (``pytester``, run as a subprocess so its
warnings filters and GC state never touch this suite's own), built from the project's actual
``pyproject.toml`` settings and ``conftest.py`` fixture — not a re-description of them — so a
revert of either piece fails this test, not just the behavior it protects.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from _pytest.pytester import Pytester, RunResult

pytest_plugins = ["pytester"]

_REPO_ROOT = Path(__file__).resolve().parents[2]

# A cycle a plain reference count never frees (the object refers to itself), with a __del__ that
# raises once collected: the leaked-transport shape from #276, minus asyncio.
_LEAKY_MODULE = """
class _Leaky:
    def __init__(self):
        self.ref = self

    def __del__(self):
        raise RuntimeError("leaked")
"""

_TEST_MODULE = '''
import leaky

def test_leaks_a_cycle():
    obj = leaky._Leaky()
    obj = None  # the only external reference; what's left is the self-cycle alone

def test_after():
    """Runs after the leak, in its own test: proves a caught leak doesn't drift here."""
    pass
'''

# tests/conftest.py's fail_on_unraisable_exception fixture, reproduced (not imported: pytester's
# inner run is a separate process with its own rootdir, so it cannot see this suite's conftest).
_GC_CONFTEST = """
import gc
import pytest

@pytest.fixture(autouse=True)
def fail_on_unraisable_exception():
    yield
    gc.collect(1)
"""


def _pyproject_filterwarnings() -> list[str]:
    """The real ``[tool.pytest.ini_options] filterwarnings`` from this repo's pyproject.toml."""
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    return data["tool"]["pytest"]["ini_options"]["filterwarnings"]


def _run(pytester: Pytester, *, with_fix: bool) -> RunResult:
    pytester.makepyfile(leaky=_LEAKY_MODULE, test_leak=_TEST_MODULE)
    args = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if with_fix:
        # The real filterwarnings list, read from pyproject.toml (not retyped here), plus the
        # real autouse fixture, reproduced in this inner project's own conftest.py.
        pytester.makeconftest(_GC_CONFTEST)
        for f in _pyproject_filterwarnings():
            args += ["-W", f]
    return pytester.run(*args)


def test_a_leaked_del_fails_its_own_test_with_the_fix(pytester: Pytester) -> None:
    """With #277's filterwarnings and the autouse gc.collect(1): both tests' own bodies pass, but
    the leaking one's teardown now errors on its RuntimeError (pytest reports a ``__del__``
    failure as a teardown error, not a body failure; either way the run exits non-zero) — and
    the error names *that* test, not the one after it. That's the whole point: found where it
    leaks."""
    result = _run(pytester, with_fix=True)
    result.assert_outcomes(passed=2, errors=1)
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*ERROR*test_leaks_a_cycle*"])
    assert "RuntimeError: leaked" in result.stdout.str()


def test_the_same_leak_passes_silently_without_the_fix(pytester: Pytester) -> None:
    """The control: with neither piece (pytest's and this repo's defaults before #277), the exact
    same leak is at most a warning, and the run exits clean. Without this, the test above checks
    nothing (CLAUDE.md: "a test that passes either way checks nothing")."""
    result = _run(pytester, with_fix=False)
    result.assert_outcomes(passed=2, errors=0, failed=0)
    assert result.ret == 0


@pytest.mark.parametrize("entry", _pyproject_filterwarnings())
def test_filterwarnings_entries_are_well_formed(entry: str) -> None:
    """A guard a typo in pyproject.toml's filterwarnings would fail: pytest's own ``warnings``
    action/category/message/module/lineno fields, each present if non-empty."""
    fields = entry.split(":")
    assert 1 <= len(fields) <= 5, entry
    assert fields[0] in {
        "error",
        "ignore",
        "always",
        "default",
        "module",
        "once",
    }, fields[0]
