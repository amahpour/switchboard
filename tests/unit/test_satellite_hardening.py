"""The satellite is non-dumpable on Linux (DESIGN.md §27.4.8, §27.5.9).

A same-user process on the remote host (a prompt-injected agent) must not be able to
forge link frames by opening the satellite's ``/proc/<pid>/fd/1``, read its environ or
``ptrace`` it. ``satellite.harden()`` calls ``prctl(PR_SET_DUMPABLE, 0)``; after it the
kernel refuses all three to a process of the same uid. The control case shows the
same checks succeed against a default (dumpable) process, so the refusals are
``harden()``'s doing. Linux only: macOS has no ``/proc`` and already refuses
``task_for_pid`` to unentitled processes.
"""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import child_env

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="Linux only: prctl(PR_SET_DUMPABLE) and /proc")

PTRACE_ATTACH = 16
PTRACE_DETACH = 17

HELPER = """
import sys, time
from switchboard.remote.satellite import harden
print(harden() if sys.argv[1] == "harden" else "none", flush=True)
time.sleep(60)
"""


@pytest.fixture
def helper(request: pytest.FixtureRequest) -> Iterator[tuple[int, str]]:
    """A helper process of this test's uid: ``harden`` (it calls ``satellite.harden()``)
    or ``none`` (the control). Yields (pid, what harden() said)."""
    p = subprocess.Popen([sys.executable, "-c", HELPER, request.param], stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, env=child_env(), text=True)
    try:
        assert p.stdout is not None
        said = p.stdout.readline().strip()
        yield p.pid, said
    finally:
        p.kill()
        p.wait(10)


def ptrace(req: int, pid: int) -> int:
    """0, or the errno of ``ptrace(req, pid, 0, 0)``."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
    libc.ptrace.restype = ctypes.c_long
    return 0 if libc.ptrace(req, pid, None, None) == 0 else ctypes.get_errno()


def yama_scope() -> int:
    try:
        return int(Path("/proc/sys/kernel/yama/ptrace_scope").read_text().strip())
    except (OSError, ValueError):
        return 0


def refused(pid: int) -> None:
    with pytest.raises(PermissionError):
        os.close(os.open(f"/proc/{pid}/fd/1", os.O_WRONLY))  # the link's stdout: forged frames
    with pytest.raises(PermissionError):
        os.listdir(f"/proc/{pid}/fd")
    with pytest.raises(PermissionError):
        with open(f"/proc/{pid}/environ", "rb") as f:
            f.read()
    assert ptrace(PTRACE_ATTACH, pid) == errno.EPERM


@pytest.mark.parametrize("helper", ["harden"], indirect=True)
def test_proc_fd_environ_and_ptrace_refused_after_harden(helper: tuple[int, str]) -> None:
    pid, said = helper
    assert said == "prctl"
    refused(pid)


@pytest.mark.parametrize("helper", ["none"], indirect=True)
def test_default_process_is_open(helper: tuple[int, str]) -> None:
    """The control: without harden() the same uid can do all of it (so the test above
    shows harden()'s effect, not the container's)."""
    pid, said = helper
    assert said == "none"
    os.close(os.open(f"/proc/{pid}/fd/1", os.O_WRONLY))
    assert os.listdir(f"/proc/{pid}/fd")
    with open(f"/proc/{pid}/environ", "rb") as f:
        assert f.read()
    if yama_scope() >= 2:
        pytest.skip("Yama ptrace_scope >= 2: only an admin may ptrace here (checked the rest)")
    assert ptrace(PTRACE_ATTACH, pid) == 0  # a child of this process: Yama scope 1 allows it
    os.waitpid(pid, 0)  # its ptrace-stop
    assert ptrace(PTRACE_DETACH, pid) == 0


def test_a_running_satellite_is_not_dumpable() -> None:
    """``python -m switchboard satellite`` makes itself non-dumpable before its first read of
    the link (``switchboard/__main__.py``, then ``harden()``), and says so in its hello."""
    from fakes.fake_link import SatDriver, make_pi_home

    pi = make_pi_home()
    sat = SatDriver(pi)
    try:
        hello = sat.recv_type("hello")
        assert hello["harden"] == "prctl"
        refused(sat.p.pid)
        status = Path(f"/proc/{sat.p.pid}/status").read_text()
        assert "\nTracerPid:\t0\n" in status
    finally:
        sat.close()
        shutil.rmtree(pi, ignore_errors=True)
