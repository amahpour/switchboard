"""The satellite's side of a remote Claude (DESIGN.md §27.5.6): the relayed registry and
the last-mile check, with this test playing the broker (``SatDriver``).

For the relay, the watched "Claude" is this test process itself (a live pid on the
satellite's machine), with a registry file in the Pi home's sessions dir. For the last
mile, a stand-in ``claude`` (a script copied to a path ending in ``/claude``) writes its
own registry file and runs a client of the Pi socket as its child, so the satellite
attests that client as that Claude's MCP server at its ``mcp.hello``, exactly as it
would a real one: a ``deliver`` push on that connection may only be for that session.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
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

SOCK = "/tmp/yk-sat-claude-inbox.sock"


@pytest.fixture
def pi() -> Iterator[Path]:
    d = make_pi_home()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def me() -> tuple[int, float]:
    info = proc.info(os.getpid())
    assert info is not None
    return os.getpid(), info.start


def write_reg(pi: Path, owner: int, **fields: Any) -> None:
    """``<pi sessions>/<owner>.json``, as Claude Code writes it (``fields`` override)."""
    data = {"pid": owner, "messagingSocketPath": SOCK, "status": "idle", **fields}
    p = pi / "claude-sessions" / f"{owner}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(p)


class Client:
    """A raw client of the Pi socket (as the Pi MCP server would be)."""

    def __init__(self, pi: Path):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.connect(str(Paths.from_home(pi).sock))
        self.s.settimeout(0.1)
        self.buf = b""

    def lines(self, wait: float = 0.5) -> list[dict[str, Any]]:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                chunk = self.s.recv(65536)
            except (TimeoutError, socket.timeout):
                continue
            if not chunk:
                break
            self.buf += chunk
        out = []
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            out.append(json.loads(line))
        return out

    def close(self) -> None:
        self.s.close()


def started(pi: Path) -> tuple[SatDriver, Client, int]:
    """A satellite past its welcome, watching this process as a Claude; a client connected."""
    sat = SatDriver(pi)
    sat.welcome()
    wait_for(lambda: Paths.from_home(pi).sock.exists(), what="the Pi socket")
    pid, start = me()
    sat.send(proto.watch(1, [(pid, start)], [(pid, start, SOCK)]))
    client = Client(pi)
    c = sat.recv_type("open")["c"]
    return sat, client, c


def recv_types(sat: SatDriver, *types: str, timeout: float = 5.0) -> list[dict[str, Any]]:
    """Frames of these types, in arrival order, until one of the last type arrives."""
    got = []
    deadline = time.monotonic() + timeout
    while True:
        f = sat.recv(max(0.1, deadline - time.monotonic()))
        assert f is not None, "EOF"
        if f["t"] in types:
            got.append(f)
            if f["t"] == types[-1]:
                return got


def deliver(bid: int = 7) -> dict[str, Any]:
    return {"push": "deliver", "data": {"batch_id": bid, "text": "[switchboard] #fpga: hi", "room": "#fpga",
                                         "sender": "alice"}}


# ---------------------------------------------------------------- the relay
def test_reg_relays_status_and_ages_every_250ms(pi: Path) -> None:
    pid, start = me()
    write_reg(pi, pid, status="idle", statusUpdatedAt=int((time.time() - 5) * 1000))
    sat, client, _c = started(pi)
    try:
        first = sat.recv_type("reg")
        [[vpid, vstart, status, since_age]] = first["views"]
        assert (vpid, status) == (pid, "idle") and proc.same_start(vstart, start)
        assert 4.0 < since_age < 7.0 and 0.0 <= first["read_age"] < 1.0
        proto.validate(first, "s2b")  # what the broker accepts
        t0 = time.monotonic()
        for _ in range(4):
            sat.recv_type("reg")
        assert time.monotonic() - t0 < 2.0  # about every 250 ms
        # a status change is relayed; no statusUpdatedAt means no since
        write_reg(pi, pid, status="busy")
        f = wait_for(lambda: (x := sat.recv_type("reg")) and x["views"][0][2] == "busy" and x, what="busy")
        assert f["views"][0][3] is None
        # nothing but pids, starts, statuses and ages: no path, no session id
        assert set(f) == {"t", "views", "read_age"} and SOCK not in json.dumps(f)
    finally:
        client.close()
        sat.close()


@pytest.mark.parametrize("bad", ["other_pid", "other_socket", "missing", "not_json"])
def test_reg_is_null_for_a_file_that_is_not_this_session(pi: Path, bad: str) -> None:
    pid, _start = me()
    if bad == "other_pid":
        write_reg(pi, pid, pid=pid + 1)
    elif bad == "other_socket":
        write_reg(pi, pid, messagingSocketPath="/tmp/someone-else.sock")
    elif bad == "not_json":
        (pi / "claude-sessions" / f"{pid}.json").write_text("{not json")
    sat, client, _c = started(pi)
    try:
        f = sat.recv_type("reg")
        assert [v[2:] for v in f["views"]] == [[None, None]]
    finally:
        client.close()
        sat.close()


def test_reg_is_null_for_a_dead_or_recycled_pid(pi: Path) -> None:
    pid, start = me()
    write_reg(pi, pid)
    sat = SatDriver(pi)
    try:
        sat.welcome()
        sat.send(proto.watch(1, [(pid, start - 100.0)], [(pid, start - 100.0, SOCK)]))  # another start time
        f = sat.recv_type("reg")
        assert [v[2] for v in f["views"]] == [None]
    finally:
        sat.close()


# ------------------------------------------------------ a real (stand-in) Claude
STANDIN = """\
import json, os, subprocess, sys
sessions, sock, client, pi_sock = sys.argv[1:5]
path = os.path.join(sessions, f"{os.getpid()}.json")
with open(path + ".tmp", "w") as f:
    json.dump({"pid": os.getpid(), "messagingSocketPath": sock, "status": "idle"}, f)
os.replace(path + ".tmp", path)
sys.exit(subprocess.call([sys.executable, client, pi_sock, sock]))
"""

CLIENT = """\
import json, socket, sys, time
pi_sock, claude_sock = sys.argv[1:3]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
for _ in range(200):
    try:
        s.connect(pi_sock)
        break
    except OSError:
        time.sleep(0.05)
hello = {"id": 1, "method": "mcp.hello", "params": {"harness": "claude", "claude_socket": claude_sock}}
s.sendall((json.dumps(hello) + "\\n").encode())
for line in s.makefile("rb"):
    sys.stdout.buffer.write(line)
    sys.stdout.buffer.flush()
"""


class StandInClaude:
    """A stand-in Claude session on the Pi: ``pid``/``start`` are the "claude" process, whose
    child is a client of the Pi socket that says ``mcp.hello`` as its MCP server. ``lines``
    is what that client received (pushes relayed by the satellite)."""

    def __init__(self, pi: Path, tmp: Path):
        exe = tmp / "claude"
        exe.write_text(STANDIN)
        client = tmp / "client.py"
        client.write_text(CLIENT)
        self.p = subprocess.Popen([sys.executable, str(exe), str(pi / "claude-sessions"), SOCK, str(client),
                                   str(Paths.from_home(pi).sock)],
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
        with_suppress = (ProcessLookupError, PermissionError)
        try:
            os.killpg(self.p.pid, signal.SIGKILL)
        except with_suppress:
            pass
        self.p.wait(5)
        if self.p.stdout is not None:
            self.p.stdout.close()


def attested(pi: Path, tmp: Path, *, watch_claude: bool = True,
             others: tuple[tuple[int, float], ...] = ()) -> tuple[SatDriver, StandInClaude, int]:
    """A satellite past its welcome, a stand-in Claude whose MCP client said hello (attested
    as that Claude's), and a watch naming it (unless not ``watch_claude``) and ``others``."""
    sat = SatDriver(pi)
    sat.welcome()
    wait_for(lambda: Paths.from_home(pi).sock.exists(), what="the Pi socket")
    fc = StandInClaude(pi, tmp)
    c = sat.recv_type("open")["c"]
    hello = sat.recv_type("req")
    a = hello["facts"]["attest"]
    assert hello["c"] == c and hello["line"]["method"] == "mcp.hello"
    assert (a["harness"], a["evidence"], a["claude_socket"]) == ("claude", "parent:claude+registry", SOCK)
    assert a["agent"][0] == fc.pid and proc.same_start(a["agent"][1], fc.start)
    claude = [(fc.pid, fc.start, SOCK)] if watch_claude else []
    claude += [(pid, start, SOCK) for pid, start in others]
    procs = [(pid, start) for pid, start, _s in claude]
    sat.send(proto.watch(1, procs, claude))
    return sat, fc, c


def own_report(sat: SatDriver, c: int, bid: int, err: str) -> dict[str, Any]:
    """After a dropped push: a fresh reg, then the satellite's own mcp.posted, marked lastmile."""
    got = recv_types(sat, "reg", "req")
    req, reg = got[-1], got[-2]  # the reg right before the report is the fresh one
    assert reg["t"] == "reg" and req["c"] == c and req["facts"] == {"lastmile": True}
    assert req["line"]["method"] == "mcp.posted" and req["line"]["id"] < 0
    assert req["line"]["params"] == {"batch_id": bid, "ok": False, "err": err}
    return reg


# -------------------------------------------------------------- last mile
def test_last_mile_relays_when_status_matches_and_strips_chk(pi: Path, tmp_path: Path) -> None:
    sat, fc, c = attested(pi, tmp_path)
    try:
        write_reg(pi, fc.pid, status="idle")
        line = deliver()
        sat.send(proto.out(c, line, {"pid": fc.pid, "start": fc.start, "want": "idle"}))
        got = wait_for(lambda: fc.lines(0.2), what="the relayed push")
        assert got == [line]  # exactly the line: chk rides beside it, never inside
        # a push that is not a deliver and carries no chk is relayed as it is
        sat.send(proto.out(c, {"push": "notice", "data": {"text": "x"}}))
        assert wait_for(lambda: fc.lines(0.2), what="plain push") == [{"push": "notice", "data": {"text": "x"}}]
        # and a mid-task (bypass) push needs the session busy
        write_reg(pi, fc.pid, status="busy")
        sat.send(proto.out(c, deliver(8), {"pid": fc.pid, "start": fc.start, "want": "busy"}))
        assert wait_for(lambda: fc.lines(0.2), what="the busy push") == [deliver(8)]
    finally:
        fc.close()
        sat.close()


@pytest.mark.parametrize("case", ["busy_not_idle", "idle_not_busy", "waiting", "unwatched", "unreadable"])
def test_last_mile_drops_a_stale_push_and_reports_it(pi: Path, tmp_path: Path, case: str) -> None:
    status, want = {"busy_not_idle": ("busy", "idle"), "idle_not_busy": ("idle", "busy"),
                    "waiting": ("waiting", "idle")}.get(case, ("idle", "idle"))
    sat, fc, c = attested(pi, tmp_path, watch_claude=case != "unwatched")
    try:
        write_reg(pi, fc.pid, status=status)
        if case == "unreadable":
            (pi / "claude-sessions" / f"{fc.pid}.json").unlink()
        if case != "unwatched":
            sat.recv_type("reg")  # the one right after the watch
        sat.send(proto.out(c, deliver(41), {"pid": fc.pid, "start": fc.start, "want": want}))
        reg = own_report(sat, c, 41, "stale_status")
        # a fresh view first (none for a session the broker doesn't watch)
        assert [v[2] for v in reg["views"]] == ([] if case == "unwatched" else
                                               [None if case == "unreadable" else status])
        assert fc.lines(0.5) == []  # nothing reached the MCP server
        # the broker's answer to the satellite's own request is dropped too
        sat.send(proto.out(c, {"id": -1, "result": {}}))
        assert fc.lines(0.5) == []
    finally:
        fc.close()
        sat.close()


@pytest.mark.parametrize("case", ["no_chk", "no_chk_idle", "other_session", "wrong_start", "never_attested"])
def test_last_mile_holds_whatever_the_desktop_sends(pi: Path, tmp_path: Path, case: str) -> None:
    """The Pi enforces the approval hold itself (security review, M8d): a deliver without
    ``chk``, or with a ``chk`` that names another watched session (one that is idle) than
    the Claude this connection was attested under, never reaches the MCP server while its
    own session has a prompt open. Reported counted (``no_chk``/``bad_chk``): the broker
    never sends either, so it shows as failed deliveries there."""
    other = me()
    write_reg(pi, other[0], status="idle")  # another watched Claude, idle
    sat, fc, c = attested(pi, tmp_path, others=(other,))
    client: Client | None = None
    try:
        write_reg(pi, fc.pid, status="idle" if case == "no_chk_idle" else "waiting")
        chk: dict[str, Any] | None = {"pid": fc.pid, "start": fc.start, "want": "idle"}
        err = "bad_chk"
        if case.startswith("no_chk"):
            chk, err = None, "no_chk"
        elif case == "other_session":
            chk = {"pid": other[0], "start": other[1], "want": "idle"}
        elif case == "wrong_start":
            chk = {"pid": fc.pid, "start": fc.start - 100.0, "want": "idle"}
        target = c
        if case == "never_attested":
            client = Client(pi)  # a connection that never said hello as a verified Claude
            target = sat.recv_type("open")["c"]
        sat.send(proto.out(target, deliver(51), chk))
        own_report(sat, target, 51, err)
        assert fc.lines(0.5) == []
        if client is not None:
            assert client.lines(0.3) == []
    finally:
        if client is not None:
            client.close()
        fc.close()
        sat.close()


def test_the_satellites_own_ids_never_collide_with_a_client(pi: Path, tmp_path: Path) -> None:
    """Negative ids are the satellite's; a client's positive-id answers pass as before."""
    sat, fc, c = attested(pi, tmp_path)
    try:
        write_reg(pi, fc.pid, status="busy")
        ids = []
        for bid in (1, 2):
            sat.send(proto.out(c, deliver(bid), {"pid": fc.pid, "start": fc.start, "want": "idle"}))
            ids.append(recv_types(sat, "req")[0]["line"]["id"])
        assert ids[0] < 0 and ids[1] < 0 and ids[0] != ids[1]
        sat.send(proto.out(c, {"id": 5, "result": {"ok": True}}))
        assert wait_for(lambda: fc.lines(0.2), what="the answer") == [{"id": 5, "result": {"ok": True}}]
    finally:
        fc.close()
        sat.close()


# ------------------------------------------------------------ robustness
@pytest.mark.parametrize("bad", ["huge_int", "deep_json", "fifo"])
def test_a_malformed_registry_file_never_ends_the_link(pi: Path, bad: str) -> None:
    """A registry file a same-user process wrote to trip the reader (security review, M8d):
    the link keeps answering pings, the relay keeps coming, the view is only what can be
    read (a status without a time, or nothing)."""
    pid, start = me()
    path = pi / "claude-sessions" / f"{pid}.json"
    if bad == "huge_int":
        path.write_text('{"pid": %d, "status": "idle", "statusUpdatedAt": %s}' % (pid, "9" * 400))
    elif bad == "deep_json":
        path.write_text("[" * 200_000 + "]" * 200_000)
    else:
        os.mkfifo(path)  # no writer: a blocking open would hang the satellite's one event loop
    sat, client, c = started(pi)
    try:
        for n in (1, 2):
            f = sat.recv_type("reg", timeout=3.0)
            assert [v[2:] for v in f["views"]] == ([["idle", None]] if bad == "huge_int" else [[None, None]])
            sat.send(proto.ping(n))
            assert sat.recv_type("pong", timeout=3.0)["n"] == n
        # the last mile reads the same file: the push is dropped, reported, and the link lives
        sat.send(proto.out(c, deliver(61), {"pid": pid, "start": start, "want": "idle"}))
        assert recv_types(sat, "req")[0]["line"]["params"]["err"] in ("bad_chk", "stale_status")
        sat.send(proto.ping(3))
        assert sat.recv_type("pong", timeout=3.0)["n"] == 3
    finally:
        client.close()
        sat.close()
