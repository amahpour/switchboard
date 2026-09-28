"""The satellite on its own host (DESIGN.md §27.4.3, §27.4.8): start checks, one per home,
takeover, and what the CLI says on a satellite home."""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import child_env
from fakes.fake_link import FakeLink, SatDriver, make_pi_home, wait_for
from switchboard.paths import Paths
from switchboard.remote.satellite import start_refusal


@pytest.fixture
def pi() -> Iterator[Path]:
    d = make_pi_home()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def run_satellite(home: Path, *extra: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "switchboard", "satellite", "--home", str(home), "--name", "fpga-pi",
                           *extra], env=child_env(**(env or {})), capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=30)


def test_refuses_without_satellite_toml() -> None:
    d = make_pi_home(satellite=False)
    try:
        r = run_satellite(d, "--test-mode")
        assert r.returncode == 2 and "not a satellite home" in r.stderr and r.stdout == ""
        # nor with a satellite.toml that names another remote
        from switchboard.remote.config import write_satellite_conf

        write_satellite_conf(Paths.from_home(d), "other-pi")
        r = run_satellite(d, "--test-mode")
        assert r.returncode == 2 and "names other-pi" in r.stderr
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_refuses_without_ssh_connection_outside_test_mode(pi: Path) -> None:
    r = run_satellite(pi)
    assert r.returncode == 2 and "SSH_CONNECTION" in r.stderr
    # with it (as under sshd) the start checks pass; a TTY on stdio does not
    paths = Paths.from_home(pi)
    env = {"SSH_CONNECTION": "192.0.2.10 5000 192.0.2.20 22"}
    r_fd, w_fd = os.pipe()
    try:
        assert start_refusal(paths, "fpga-pi", test_mode=False, environ=env, fds=(r_fd, w_fd)) is None
    finally:
        os.close(r_fd)
        os.close(w_fd)
    master, slave = os.openpty()
    try:
        why = start_refusal(paths, "fpga-pi", test_mode=False, environ=env, fds=(slave, slave))
        assert why and "terminal" in why
    finally:
        os.close(master)
        os.close(slave)
    # test mode checks the home as the broker's does
    assert start_refusal(paths, "fpga-pi", test_mode=True, environ={}) is None
    (pi / ".switchboard-test").unlink()
    assert "marker" in (start_refusal(paths, "fpga-pi", test_mode=True, environ={}) or "")


def test_refuses_while_local_broker_runs(pi: Path) -> None:
    Paths.from_home(pi).ensure()
    fd = os.open(pi / "run" / "broker.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a broker running from this home holds it
    try:
        sat = SatDriver(pi)
        first = sat.recv()
        assert first == {"t": "bye", "why": "local_broker"}
        assert sat.close() == 0
        assert not (pi / "run" / "broker.sock").exists()
    finally:
        os.close(fd)
    # through the broker: blocked, with the fix in the notice
    link = FakeLink(kind="inproc")
    try:
        Paths.from_home(link.pi).ensure()
        fd = os.open(link.pi / "run" / "broker.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            link.start(wait_up=False)
            link.wait_state("blocked", reason="local_broker")
            assert any("a switchboard broker runs on the remote" in t for t in link.notices())
        finally:
            os.close(fd)
    finally:
        link.close()


def test_takes_over_a_stale_satellite(pi: Path) -> None:
    run = pi / "run"
    # a pidfile of a satellite long gone: nothing to take over, it just starts
    Paths.from_home(pi).ensure()
    (run / "satellite.pid").write_text("999999 12.5\n")
    a = SatDriver(pi)
    try:
        a.welcome()
        wait_for(lambda: (run / "broker.sock").exists(), what="a's socket")
        assert int((run / "satellite.pid").read_text().split()[0]) == a.p.pid
        # a second satellite (a reconnect whose old half-open link still has one) takes over
        b = SatDriver(pi)
        try:
            hello = b.recv_type("hello")
            assert hello["name"] == "fpga-pi"
            bye = a.recv_type("bye")
            assert bye["why"] == "replaced"  # a names the newer satellite that replaced it
            assert a.p.wait(10) == 0
            assert int((run / "satellite.pid").read_text().split()[0]) == b.p.pid
            b.welcome(hello=hello)
            wait_for(lambda: (run / "broker.sock").exists(), what="b's socket")
        finally:
            b.close()
    finally:
        a.close()
    # a satellite that ends on its own (EOF) says so and unlinks its socket
    assert not (run / "broker.sock").exists()


def test_start_refused_on_satellite_home(pi: Path) -> None:
    r = subprocess.run([sys.executable, "-m", "switchboard", "--home", str(pi), "start", "--test-mode"],
                       env=child_env(), capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    assert r.returncode == 1 and "this is a satellite home: the broker runs on desk" in r.stderr
    assert not (pi / "switchboard.db").exists()


def test_status_on_satellite_home() -> None:
    with FakeLink(trust=True) as link:
        r = link.pi_cli("status")
        assert r.returncode == 0, r.stderr
        assert r.stdout.startswith("switchboard satellite ") and "for fpga-pi: link up" in r.stdout
        assert "desktop desk" in r.stdout and "#fpga" in r.stdout
        from switchboard.mcp.client import call_sync

        st = call_sync(link.pi_paths.sock, "sys.status", {}, 5)
        assert st["stdio"] == "socket"  # the exec transport's socketpair (sshd's is usually "pipe")
        assert link.cli("remote", "disable", link.name).returncode == 0
        wait_for(lambda: not os.path.exists(link.pi_paths.sock), what="the satellite gone")
        r = link.pi_cli("status")
        assert r.returncode == 3 and "link down: the desktop (desk) dials this machine" in r.stdout


def _first_line(fd: int, timeout: float = 20.0) -> bytes:
    import select
    import time

    buf = b""
    deadline = time.monotonic() + timeout
    while b"\n" not in buf:
        left = deadline - time.monotonic()
        assert left > 0, buf
        r, _, _ = select.select([fd], [], [], left)
        if r:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
    return buf.split(b"\n", 1)[0]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the /proc fd scan runs on Linux")
@pytest.mark.parametrize("held", ["stdin", "stdout", "none"])
def test_refuses_when_another_process_holds_its_stdio(pi: Path, held: str) -> None:
    """With pipes (sshd's usual stdio), a process of the same user that holds the link's stdin
    or stdout (opened through /proc before the prctl, or inherited from the login shell) could
    read and forge frames: the satellite says ``bye exposed`` and never takes the home's
    locks. Its parent, which holds the far ends, doesn't count (M8c's deferred item, §27.16)."""
    import json

    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    keep = {"stdin": (in_r,), "stdout": (out_w,), "none": ()}[held]
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], pass_fds=keep,
                              env=child_env(), stdin=subprocess.DEVNULL)
    try:
        sat = subprocess.Popen([sys.executable, "-I", "-m", "switchboard", "satellite", "--home", str(pi), "--name",
                                "fpga-pi", "--test-mode"], stdin=in_r, stdout=out_w, stderr=subprocess.PIPE,
                               env=child_env())
        os.close(in_r)
        os.close(out_w)
        try:
            first = json.loads(_first_line(out_r))
            if held == "none":
                assert first["t"] == "hello", first
            else:
                assert first == {"t": "bye", "why": "exposed"}, first
                assert sat.wait(15) == 0
                assert not (pi / "run" / "satellite.pid").exists()  # refused before the locks
                log = (pi / "logs" / "satellite.log").read_text()
                assert f"pids [{holder.pid}]" in log
        finally:
            os.close(in_w)
            if sat.poll() is None:
                sat.wait(15)
            os.close(out_r)
            assert sat.stderr is not None
            sat.stderr.close()
    finally:
        holder.kill()
        holder.wait(5)
