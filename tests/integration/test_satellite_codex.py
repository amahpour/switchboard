"""The satellite's side of a remote Codex wake (issue #63), with this test playing the broker
(``SatDriver``): a ``deliver`` to a Codex MCP server is relayed only for the Codex process
that server was attested under, with ``want: idle``, while that process lives; anything
else is dropped and reported (``bad_chk``, counted), as for Claude.

A stand-in ``codex`` (a script copied to a path ending in ``/codex``) runs a client of
the Pi socket as its child, which says ``mcp.hello`` as a Codex MCP server: the satellite
attests it exactly as it would a real one.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import child_env
from fakes.fake_link import SatDriver, make_pi_home, wait_for
from switchboard.broker import proc
from switchboard.paths import Paths
from switchboard.remote import proto

STANDIN = """\
import subprocess, sys
sys.exit(subprocess.call([sys.executable] + sys.argv[1:]))
"""

CLIENT = """\
import json, socket, sys, time
pi_sock = sys.argv[1]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
for _ in range(200):
    try:
        s.connect(pi_sock)
        break
    except OSError:
        time.sleep(0.05)
hello = {"id": 1, "method": "mcp.hello", "params": {"harness": "codex"}}
s.sendall((json.dumps(hello) + "\\n").encode())
for line in s.makefile("rb"):
    sys.stdout.buffer.write(line)
    sys.stdout.buffer.flush()
"""


@pytest.fixture
def pi() -> Iterator[Path]:
    d = make_pi_home()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


class StandInCodex:
    """``pid``/``start``: the "codex" process; its child says hello as its MCP server."""

    def __init__(self, pi: Path, tmp: Path):
        exe = tmp / "codex"
        exe.write_text(STANDIN)
        client = tmp / "client.py"
        client.write_text(CLIENT)
        self.p = subprocess.Popen([sys.executable, str(exe), str(client), str(Paths.from_home(pi).sock)],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, env=child_env(),
                                  start_new_session=True)
        self.pid = self.p.pid
        info = wait_for(lambda: proc.info(self.pid), what="the stand-in's start time")
        self.start = info.start
        self.buf = b""

    def lines(self, wait: float = 0.5) -> list[dict[str, Any]]:
        import select

        assert self.p.stdout is not None
        fd = self.p.stdout.fileno()
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
            if r:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                self.buf += chunk
        out = []
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            out.append(json.loads(line))
        return out

    def close(self) -> None:
        try:
            os.killpg(self.p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        self.p.wait(5)
        if self.p.stdout is not None:
            self.p.stdout.close()


def attested(pi: Path, tmp: Path) -> tuple[SatDriver, StandInCodex, int]:
    sat = SatDriver(pi)
    sat.welcome()
    wait_for(lambda: Paths.from_home(pi).sock.exists(), what="the Pi socket")
    cx = StandInCodex(pi, tmp)
    c = sat.recv_type("open")["c"]
    hello = sat.recv_type("req")
    a = hello["facts"]["attest"]
    assert hello["c"] == c and hello["line"]["method"] == "mcp.hello"
    assert (a["harness"], a["evidence"], a["claude_socket"]) == ("codex", "parent:codex", None)
    assert a["agent"][0] == cx.pid and proc.same_start(a["agent"][1], cx.start)
    return sat, cx, c


def deliver(bid: int = 7) -> dict[str, Any]:
    return {"push": "deliver", "data": {"batch_id": bid, "text": "[switchboard] #fpga: hi", "room": "#fpga",
                                         "sender": "alice", "thread_id": "019a-thread"}}


def own_report(sat: SatDriver, c: int, bid: int, err: str) -> None:
    deadline = time.monotonic() + 5.0
    while True:
        f = sat.recv(max(0.1, deadline - time.monotonic()))
        assert f is not None, "EOF"
        if f["t"] == "req":
            break
    assert f["c"] == c and f["facts"] == {"lastmile": True}
    assert f["line"]["method"] == "mcp.posted" and f["line"]["id"] < 0
    assert f["line"]["params"] == {"batch_id": bid, "ok": False, "err": err}


def test_a_wake_for_its_own_codex_is_relayed_without_chk(pi: Path, tmp_path: Path) -> None:
    sat, cx, c = attested(pi, tmp_path)
    try:
        line = deliver()
        sat.send(proto.out(c, line, {"pid": cx.pid, "start": cx.start, "want": "idle"}))
        got = wait_for(lambda: cx.lines(0.2), what="the relayed wake")
        assert got == [line]  # exactly the line: chk rides beside it, never inside
    finally:
        cx.close()
        sat.close()


@pytest.mark.parametrize("case", ["no_chk", "another_pid", "wrong_start", "want_busy"])
def test_a_wake_for_anything_else_is_dropped_and_reported(pi: Path, tmp_path: Path, case: str) -> None:
    sat, cx, c = attested(pi, tmp_path)
    try:
        chk: dict[str, Any] | None = {"pid": cx.pid, "start": cx.start, "want": "idle"}
        err = "bad_chk"
        if case == "no_chk":
            chk, err = None, "no_chk"
        elif case == "another_pid":
            chk = {"pid": os.getpid(), "start": cx.start, "want": "idle"}
        elif case == "wrong_start":
            chk = {"pid": cx.pid, "start": cx.start - 100.0, "want": "idle"}
        elif case == "want_busy":
            chk = {"pid": cx.pid, "start": cx.start, "want": "busy"}  # no mid-task push to Codex over a link
        sat.send(proto.out(c, deliver(51), chk))
        own_report(sat, c, 51, err)
        assert cx.lines(0.5) == []
    finally:
        cx.close()
        sat.close()
