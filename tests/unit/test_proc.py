"""proc: pid -> (ppid, start, uid), argv, ancestry, alive, tty (DESIGN.md §5.3)."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from switchboard.broker import proc


def test_info_self() -> None:
    me = proc.info(os.getpid())
    assert me is not None
    assert me.pid == os.getpid() and me.ppid == os.getppid() and me.uid == os.getuid()
    assert 0 < me.start <= time.time()
    again = proc.info(os.getpid())
    assert again is not None and again.start == me.start  # stable


def test_ps_fallback_agrees() -> None:
    fast, slow = proc.info(os.getpid()), proc._info_ps(os.getpid())
    assert fast is not None and slow is not None
    assert (fast.ppid, fast.uid) == (slow.ppid, slow.uid)
    assert abs(fast.start - slow.start) < 2.0  # ps has 1 s resolution


def test_missing_pid() -> None:
    assert proc.info(0) is None
    assert proc.info(-5) is None
    assert proc.info(2_000_000_000) is None
    assert not proc.alive(2_000_000_000, 1.0)
    assert not proc.alive(None, None)


def test_alive_needs_the_same_start_time() -> None:
    me = proc.info(os.getpid())
    assert me is not None
    assert proc.alive(os.getpid(), me.start)
    assert not proc.alive(os.getpid(), me.start + 1.0)
    assert not proc.alive(os.getpid(), None)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc zombie state is Linux-only")
def test_a_zombie_is_not_alive_on_linux() -> None:
    """An exited child its parent hasn't reaped keeps its pid and start time in
    /proc; `switchboard stop` and every liveness check must still see it as gone."""
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        live = proc.info(child.pid)
        assert live is not None and proc.alive(child.pid, live.start)
        assert child.stdin is not None
        child.stdin.close()  # the child exits; we don't reap it yet
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with open(f"/proc/{child.pid}/stat") as f:
                if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                    break
            time.sleep(0.01)
        else:
            raise AssertionError("child never became a zombie")
        assert proc.info(child.pid) is None
        assert not proc.alive(child.pid, live.start)
    finally:
        child.wait()


def test_ancestry_and_argv_of_a_child() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)", "yk-marker-arg"],
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # no controlling terminal, wherever pytest runs
    )
    try:
        deadline = time.time() + 5
        chain = []
        while time.time() < deadline:
            chain = proc.ancestry(child.pid)
            if chain:
                break
            time.sleep(0.02)
        assert chain[0].pid == child.pid
        assert chain[1].pid == os.getpid()
        a = proc.argv(chain[0].pid, chain[0].start)
        assert "yk-marker-arg" in a
        many = proc.argv_many(chain[:2])
        assert "yk-marker-arg" in many[child.pid] and many[os.getpid()]
        assert proc.tty(child.pid) is None  # a new session has no controlling terminal
    finally:
        child.kill()
        child.wait()
    assert not proc.alive(child.pid, chain[0].start)


def test_ancestry_depth_limit() -> None:
    assert len(proc.ancestry(os.getpid(), depth=1)) == 1
    full = proc.ancestry(os.getpid(), depth=64)
    assert full[-1].ppid <= 1 or len(full) == 64


def test_ancestry_to_root_reaches_pid_1() -> None:
    chain, complete = proc.ancestry_to_root(os.getpid())
    assert complete
    assert chain[0].pid == os.getpid()
    assert chain[-1].pid == 1 or chain[-1].ppid == 0
    assert all(a.ppid == b.pid for a, b in zip(chain, chain[1:], strict=False))
    # a cap that cuts the walk short is reported as incomplete
    short, complete = proc.ancestry_to_root(os.getpid(), cap=1)
    assert len(short) == 1 and not complete
    assert proc.ancestry_to_root(2_000_000_000) == ([], False)
    assert proc.ancestry_to_root(0) == ([], False)
    argvs = proc.argv_many(chain)
    assert all(argvs[p.pid] for p in chain)  # every ancestor's argv is readable


def test_ps_is_called_by_absolute_path() -> None:
    ps = proc.ps_bin()
    assert ps is not None and os.path.isabs(ps)
