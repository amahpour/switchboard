"""A desktop home and a remote ("Pi") home joined by an exec-transport link (DESIGN.md §27.13 T0).

``FakeLink`` makes both homes (short dirs under /tmp with the test marker), the
Pi home's ``satellite.toml`` and ``config.toml`` (its own Claude sessions dir,
so a desktop-side check can never pass by accident), and the desktop's
``remotes.toml`` with ``transport = "exec"``. Rooms are created, and the remote
enabled for its exact config (``enable=True``), in the desktop database before
the broker starts, so a broker under the production peer policy (``trust=False``)
needs no human command; ``trust=True`` (``AllowAllHumans``) is for the tests that
drive ``switchboard remote enable`` through the CLI. The satellite runs with
``SWITCHBOARD_TEST_PID_SHIFT`` (every pid it reports is 1,000,000,000 higher than
this machine's), so any desktop probe of a remote pid finds no process.

``SatDriver`` runs a satellite by hand with pipes and plays the broker's side of
the link, for the satellite's own tests.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from conftest import TEST_HUMAN, InProcBroker, SubprocBroker, child_env, make_tmp_home
from switchboard import db
from switchboard.clock import SystemClock
from switchboard.mcp.client import call_sync
from switchboard.paths import Paths
from switchboard.remote import proto
from switchboard.remote.config import entry_hash, parse_remotes, write_satellite_conf
from switchboard.store import Store

PID_SHIFT = 1_000_000_000
NAME = "fpga-pi"


def make_pi_home(name: str = NAME, *, desktop: str = "desk", satellite: bool = True) -> Path:
    d = Path(tempfile.mkdtemp(prefix="yk-pi-", dir="/tmp"))
    (d / ".switchboard-test").touch()
    sessions = d / "claude-sessions"
    sessions.mkdir(mode=0o700)
    (d / "config.toml").write_text(f'[claude]\nsessions_dir = "{sessions}"\n')
    if satellite:
        write_satellite_conf(Paths.from_home(d), name, desktop=desktop)
    return d


def remotes_toml(name: str, pi_home: Path, rooms: tuple[str, ...], **extra: Any) -> str:
    lines = [f"[remote.{name}]", 'transport = "exec"', f'home = "{pi_home}"',
             "rooms = [" + ", ".join(json.dumps(r) for r in rooms) + "]"]
    for k, v in extra.items():
        if v is not None:
            lines.append(f"{k} = {json.dumps(v)}")
    return "\n".join(lines) + "\n"


class FakeLink:
    def __init__(self, *, name: str = NAME, rooms: tuple[str, ...] = ("#fpga",),
                 desk_rooms: tuple[str, ...] | None = None, harnesses: list[str] | None = None,
                 max_members: int | None = None, end_after_s: int | None = None, kind: str = "subproc",
                 trust: bool = True, enable: bool = True, pid_shift: int = PID_SHIFT,
                 env: dict[str, str] | None = None, broker_cfg: Any = None):
        self.name = name
        self.rooms = rooms
        self.desk = make_tmp_home()
        self.pi = make_pi_home(name)
        self.desk_paths = Paths.from_home(self.desk)
        self.pi_paths = Paths.from_home(self.pi)
        self.pi_sessions = self.pi / "claude-sessions"
        self.kind = kind
        self.trust = trust
        self.env_extra = {"SWITCHBOARD_TEST_PID_SHIFT": str(pid_shift), **(env or {})}
        self.toml = remotes_toml(name, self.pi, rooms, harnesses=harnesses, max_members=max_members,
                                 end_after_s=end_after_s)
        (self.desk / "remotes.toml").write_text(self.toml)
        self._prepare(desk_rooms if desk_rooms is not None else rooms, enable)
        self.broker: Any = None
        self._saved_env: dict[str, str | None] = {}
        self.broker_cfg = broker_cfg

    # ------------------------------------------------------------- set-up
    def _prepare(self, rooms: tuple[str, ...], enable: bool) -> None:
        self.desk_paths.ensure()
        con = db.open_db(self.desk_paths.db)
        try:
            st = Store(con, SystemClock())
            for r in rooms:
                st.create_room(r, TEST_HUMAN, 60, 30)
            if enable:
                st.set_remote_enabled(self.name, self.config_hash(), "cli")
        finally:
            con.close()

    def config_hash(self) -> str:
        entry = parse_remotes((self.desk / "remotes.toml").read_text(), test_mode=True)[self.name]
        return entry_hash(self.desk_paths, entry)

    def start(self, *, wait_up: bool = True) -> "FakeLink":
        if self.kind == "inproc":
            for k, v in self.env_extra.items():
                self._saved_env[k] = os.environ.get(k)
                os.environ[k] = v
            self.broker = InProcBroker(self.desk, self.broker_cfg).start()
        else:
            self.broker = SubprocBroker(self.desk, trust=self.trust)
            self.broker.env.update(self.env_extra)
            self.broker.start()
        if wait_up:
            self.wait_state("up")
        return self

    def restart_broker(self, *, wait_up: bool = True) -> None:
        if self.kind == "inproc":
            self.broker.restart()
        else:
            self.broker.kill()
            self.broker = SubprocBroker(self.desk, trust=self.trust)
            self.broker.env.update(self.env_extra)
            self.broker.start()
        if wait_up:
            self.wait_state("up")

    def close(self) -> None:
        if self.broker is not None:
            if self.kind == "inproc":
                self.broker.stop()
            else:
                self.broker.kill()
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.desk, ignore_errors=True)
        shutil.rmtree(self.pi, ignore_errors=True)

    def __enter__(self) -> "FakeLink":
        return self if self.broker is not None else self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------ queries
    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
        """A request on the desktop broker's own socket."""
        return call_sync(self.desk_paths.sock, method, params or {}, timeout)

    def status(self) -> dict[str, Any]:
        return self.call("remote.status", {"name": self.name})["remotes"][0]

    def wait_state(self, state: str, timeout: float = 20.0, reason: str | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        st: dict[str, Any] = {}
        while time.monotonic() < deadline:
            st = self.status()
            if st["state"] == state and (reason is None or st.get("reason") == reason):
                if state != "up" or os.path.exists(self.pi_paths.sock):
                    return st
            time.sleep(0.05)
        raise AssertionError(f"remote never reached {state}{f' ({reason})' if reason else ''}: {st}")

    def messages(self, room: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        return self.call("room.history", {"room": room or self.rooms[0], "limit": limit})["messages"]

    def notices(self, room: str | None = None) -> list[str]:
        return [m["text"] for m in self.messages(room) if m["kind"] == "notice"]

    def members(self, room: str | None = None) -> list[dict[str, Any]]:
        return self.call("room.who", {"room": room or self.rooms[0]})["members"]

    def satellite_pid(self) -> int | None:
        try:
            return int((self.pi_paths.run_dir / "satellite.pid").read_text().split()[0])
        except (OSError, ValueError, IndexError):
            return None

    def cli(self, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        return self.run_cli(self.desk, *args, timeout=timeout)

    def pi_cli(self, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        return self.run_cli(self.pi, *args, timeout=timeout)

    def run_cli(self, home: Path, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "switchboard", "--home", str(home), *args],
                              env=child_env(**self.env_extra), capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, start_new_session=True)

    def write_remotes(self, text: str) -> None:
        p = self.desk / "remotes.toml"
        p.write_text(text)
        # a new mtime even on a coarse clock, so the manager sees the edit
        st = os.stat(p)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def wait_for(fn: Any, timeout: float = 10.0, interval: float = 0.05, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}: {last!r}")


class SatDriver:
    """A satellite run by hand with pipes: this test plays the broker's side of the link."""

    def __init__(self, pi_home: Path, name: str = NAME, *, test_mode: bool = True,
                 env: dict[str, str] | None = None, pid_shift: int = 0):
        e = child_env(**(env or {}))
        if pid_shift:
            e["SWITCHBOARD_TEST_PID_SHIFT"] = str(pid_shift)
        argv = [sys.executable, "-I", "-m", "switchboard", "satellite", "--home", str(pi_home), "--name", name]
        if test_mode:
            argv.append("--test-mode")
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=e, start_new_session=True)
        self.buf = b""

    def send(self, frame: dict[str, Any]) -> None:
        assert self.p.stdin is not None
        self.p.stdin.write(proto.encode(frame))
        self.p.stdin.flush()

    def recv(self, timeout: float = 10.0) -> dict[str, Any] | None:
        """The next frame, or None on EOF."""
        import select

        assert self.p.stdout is not None
        fd = self.p.stdout.fileno()
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("no frame from the satellite")
            r, _, _ = select.select([fd], [], [], left)
            if not r:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                return None
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return proto.loads(line)

    def recv_type(self, t: str, timeout: float = 10.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            f = self.recv(max(0.1, deadline - time.monotonic()))
            if f is None:
                raise AssertionError(f"EOF before a {t} frame")
            if f.get("t") == t:
                return f

    def welcome(self, rooms: list[str] | None = None, *, hello: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read the hello (unless given) and answer it with a welcome."""
        hello = hello or self.recv_type("hello")
        self.send(proto.welcome(version="0.0.0", link="0123456789abcdef", rooms=rooms or ["#fpga"],
                                harnesses=["claude", "codex", "cursor", "devin"],
                                limits={"max_conns": 64, "max_members": 8, "frame_rate": 300, "queue_lines": 5000}))
        return hello

    def close(self) -> int:
        with_stdin = self.p.stdin
        if with_stdin is not None and not with_stdin.closed:
            try:
                with_stdin.close()
            except OSError:
                pass
        try:
            return self.p.wait(10)
        except subprocess.TimeoutExpired:
            self.p.kill()
            return self.p.wait(5)

    def stderr(self) -> str:
        assert self.p.stderr is not None
        try:
            return self.p.stderr.read().decode(errors="replace")
        except OSError:
            return ""
