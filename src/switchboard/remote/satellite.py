"""``switchboard satellite``: the remote end of one link (DESIGN.md §27.4.8).

sshd starts it as the link key's forced command (a test-mode broker runs it
itself, the exec transport). It plays, on the remote host, the role the kernel
and ``/proc`` play for the broker's local members: it owns the home's broker
socket, and for each local connection it reports what this machine's kernel says
(the kernel peer, ``verify_mcp_peer``'s attest, a hook's process chain,
liveness), relaying the connection's lines over its stdio. The broker makes every
decision; this process decides nothing and runs nothing: it has no spawn site,
no pty and no network socket (``tests/unit/test_satellite_static.py``).

- **Start.** ``satellite.toml`` must name ``--name``; ``SSH_CONNECTION`` must be set
  (unless ``--test-mode``, which checks the home as the broker's does); neither
  stdin nor stdout may be a TTY. Then ``harden()``, and on Linux ``exposure()``: if
  another process of this user (not an ancestor, such as sshd's session process)
  holds the link's stdin or stdout (it opened ``/proc/<pid>/fd`` before the
  ``prctl``, or inherited them from the login shell), ``bye exposed`` and exit.
  Frames go out on a private dup of fd 1 and fd 1 points at stderr, so a stray
  print can't corrupt the link.
  ``run/satellite.lock`` (taking over an older satellite: replace marker, SIGTERM,
  3 s), then ``run/broker.lock`` for its whole life, so no broker runs from this
  home meanwhile (``bye local_broker`` if one does).
- **Link.** ``hello``; after ``welcome`` it binds the home's socket (0600).
  Requests outside ``REMOTE_METHODS`` are refused here with "run this on the
  desktop"; ``sys.ping`` and ``sys.status`` are answered here. ``watch`` →
  ``alive`` (then every 1 s), ``ping`` → ``pong``, ``status`` every 60 s.
- **Claude** (§27.5.6). For every Claude agent a ``watch`` names, it reads this
  home's Claude registry (``<sessions_dir>/<pid>.json``, with the broker's own
  pid and socket checks) and relays it every 250 ms (``reg``: status and ages
  only). Every ``deliver`` push goes through the last-mile check, which is the
  approval hold's enforcer on this machine whatever the desktop sends: it must
  carry ``chk``, ``chk`` must name the Claude this connection's MCP server was
  attested under at its ``mcp.hello`` (never another session's), that Claude
  must be watched and alive, and a fresh read must say ``chk.want``. If not,
  a fresh ``reg`` frame, then this satellite's own ``mcp.posted {ok: false,
  err}`` (``stale_status``, or ``no_chk``/``bad_chk`` for a push the broker
  should never have sent), marked ``facts.lastmile`` and with a negative
  request id, whose answer it drops; the push is dropped. ``chk`` never
  reaches the MCP server.
- **End.** stdin EOF, 10 s without a ping, a ``refuse``, or SIGTERM: ``bye``
  (``replaced`` when the replace marker names a newer satellite), close every
  local connection, unlink the socket if it is still ours, exit.

Test mode only: ``SWITCHBOARD_TEST_PID_SHIFT`` is added to every pid reported and
subtracted from every pid received (a desktop probe of a remote pid then hits no
process); ``SWITCHBOARD_TEST_CLOCK_SKEW`` shifts this machine's clock as a
remote with a wrong clock would; ``SWITCHBOARD_TEST_LINK_PROTO`` announces another
link protocol; ``SWITCHBOARD_TEST_FRAME_LOG`` appends every frame, both ways, to
that file (the secrets-canary test reads it).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import fcntl
import itertools
import json
import logging
import logging.handlers
import math
import os
import re
import signal
import socket
import stat
import sys
import time
from typing import Any

from switchboard import __version__, claude_registry
from switchboard.broker import proc
from switchboard.broker.peer import McpRefused, Peer, match_agent, verify_mcp_peer
from switchboard.models import HARNESSES
from switchboard.paths import Paths, UnsafePathError, hook_state_text, test_mode_refusal, write_hook_copy
from switchboard.remote import proto
from switchboard.remote.config import RemoteConfigError, read_satellite_conf

log = logging.getLogger("switchboard.satellite")

EXIT_REFUSED = 2
MAX_LINE = proto.MAX_LINE  # a client line, as on the broker's own socket
LOCAL_QUEUE = 5000  # lines queued for one local client (§27.4.5)
LOCAL_MAX_CONNS = 256  # the broker refuses more than 64; this only bounds the process
ALIVE_S = 1.0
REG_S = 0.25  # the relayed Claude registry (§27.5.6), as often as the broker polls its own
STATUS_S = 60.0
PING_TIMEOUT_S = 10.0
WELCOME_TIMEOUT_S = 10.0
TAKEOVER_WAIT_S = 3.0
OUT_BACKLOG_BYTES = 16 * 1024 * 1024
ON_DESKTOP = "run this on the desktop: the broker and the web UI run there"
PR_SET_DUMPABLE = 4


def harden() -> str:
    """Linux: ``prctl(PR_SET_DUMPABLE, 0)``, so same-user processes can neither ptrace
    this process nor open its ``/proc/<pid>/fd`` (forged frames) or environ. macOS
    already refuses ``task_for_pid`` to unentitled processes: nothing to do."""
    if not sys.platform.startswith("linux"):
        return "none"
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return "prctl" if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0 else "failed"
    except (OSError, AttributeError):
        return "failed"


def stdio_kind(fds: tuple[int, int] = (0, 1)) -> str:
    """What the link's stdin and stdout are: ``socket`` (the exec transport's socketpair,
    or sshd's), ``pipe`` (sshd's usual), ``tty`` or ``other``; for ``sys.status``."""
    kinds = set()
    for fd in fds:
        try:
            if os.isatty(fd):
                return "tty"
            m = os.fstat(fd).st_mode
        except OSError:
            return "other"
        kinds.add("socket" if stat.S_ISSOCK(m) else "pipe" if stat.S_ISFIFO(m) else "other")
    return kinds.pop() if len(kinds) == 1 else "mixed"


def link_fd_ids(fds: tuple[int, int] = (0, 1)) -> frozenset[tuple[int, int]]:
    """(device, inode) of the link's stdin and stdout: what another process holding them
    would show in its own ``/proc/<pid>/fd``."""
    out = set()
    for fd in fds:
        with contextlib.suppress(OSError):
            st = os.fstat(fd)
            out.add((st.st_dev, st.st_ino))
    return frozenset(out)


def exposure(ids: frozenset[tuple[int, int]], proc_root: str = "/proc") -> list[int]:
    """Linux: the pids of other processes of this user that hold the link's stdin or stdout
    (by device and inode), not counting this process and its ancestors (sshd's session
    process holds the far ends of its pipes). With pipes (sshd's usual stdio) a
    process of this user could open ``/proc/<satellite>/fd/0`` and ``1`` in the moment
    before ``harden()`` and keep them, to read and forge link frames; after ``harden()``
    no new one can, so a scan after it sees every holder that is still listable. A
    holder that made itself non-dumpable too can't be listed (its ``fd`` directory is
    refused) and is not seen: this is a check, not a boundary (§27.16)."""
    if not ids or not sys.platform.startswith("linux"):
        return []
    proc.set_no_spawn()  # /proc only, as for the rest of its life (pin_linux_clock)
    me = os.getpid()
    uid = os.getuid()
    skip = {me} | {p.pid for p in proc.ancestry(me, 64)}
    found: list[int] = []
    try:
        names = os.listdir(proc_root)
    except OSError:
        return []
    for name in names:
        if not name.isdigit() or int(name) in skip:
            continue
        base = f"{proc_root}/{name}"
        try:
            if os.stat(base).st_uid != uid:
                continue
            fds = os.listdir(f"{base}/fd")
        except OSError:
            continue  # gone, or its fds aren't ours to list (a non-dumpable process)
        for fd in fds:
            try:
                st = os.stat(f"{base}/fd/{fd}")
            except OSError:
                continue
            if (st.st_dev, st.st_ino) in ids:
                found.append(int(name))
                break
    return sorted(found)


def start_refusal(paths: Paths, name: str, *, test_mode: bool, environ: Any, fds: tuple[int, int] = (0, 1)
                  ) -> str | None:
    """Why this satellite may not start, or None."""
    try:
        conf = read_satellite_conf(paths)
    except FileNotFoundError:
        return f"{paths.home} is not a satellite home (no satellite.toml): run `switchboard remote accept` there"
    except (OSError, RemoteConfigError) as e:
        return str(e)
    if conf.name != name:
        return f"satellite.toml names {conf.name}, not {name}"
    if test_mode:
        why = test_mode_refusal(paths, home_given=True)
        if why:
            return why
    elif not environ.get("SSH_CONNECTION"):
        return "the satellite runs only as an ssh forced command (SSH_CONNECTION is not set)"
    for fd in fds:
        try:
            if os.isatty(fd):
                return "the satellite's stdin and stdout must not be a terminal"
        except OSError:
            return "the satellite needs its stdin and stdout"
    return None


def _env_int(environ: Any, key: str, lo: int, hi: int) -> int:
    raw = environ.get(key)
    if raw is None:
        return 0
    v = int(raw)
    if not lo <= v <= hi:
        raise ValueError(f"{key} out of range")
    return v


def _env_float(environ: Any, key: str) -> float:
    raw = environ.get(key)
    if raw is None:
        return 0.0
    v = float(raw)
    if v != v or abs(v) > 10 * 365 * 86400:
        raise ValueError(f"{key} out of range")
    return v


# ----------------------------------------------------------------- locks
def _read_pair(path: Any) -> tuple[int, float] | None:
    try:
        pid_s, start_s = open(path, encoding="ascii").read().split()
        return int(pid_s), float(start_s)
    except (OSError, ValueError):
        return None


def _write_pair(path: Any, pid: int, start: float) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(f"{pid} {start!r}\n")
    os.replace(tmp, path)


def _flock(path: Any) -> int | None:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _is_satellite(pid: int, start: float) -> bool:
    return proc.alive(pid, start) and "-m switchboard satellite" in proc.argv(pid, start)


def take_locks(paths: Paths, me: tuple[int, float]) -> tuple[int, int] | str:
    """(satellite.lock fd, broker.lock fd), or the ``bye`` reason: ``busy`` (another
    satellite that wouldn't go, or a lock holder that isn't a satellite) or
    ``local_broker`` (a broker runs from this home)."""
    run = paths.run_dir
    sat = _flock(run / "satellite.lock")
    if sat is None:
        old = _read_pair(run / "satellite.pid")
        if old is None or old[0] == me[0] or not _is_satellite(*old):
            return "busy"
        _write_pair(run / "satellite.replace", *me)
        log.info("taking over satellite pid %d", old[0])
        with contextlib.suppress(ProcessLookupError):
            os.kill(old[0], signal.SIGTERM)
        deadline = time.monotonic() + TAKEOVER_WAIT_S
        while sat is None and time.monotonic() < deadline:
            time.sleep(0.05)
            sat = _flock(run / "satellite.lock")
        if sat is None:
            return "busy"
    _write_pair(run / "satellite.pid", *me)
    brk = _flock(paths.lockfile)
    if brk is None:
        os.close(sat)
        return "local_broker"
    return sat, brk


def replaced_by_other(paths: Paths, me: tuple[int, float]) -> bool:
    """Does the replace marker name a live, newer satellite other than this one? The same
    check ``take_locks`` makes of an older one: a live process with a ``-m switchboard
    satellite`` argv that started after this one. (A Pi-local process that disguises its
    argv can still fake it: ``blocked(replaced)`` is a hint, not proof, §27.16.)"""
    other = _read_pair(paths.run_dir / "satellite.replace")
    if other is None or (other[0] == me[0] and proc.same_start(other[1], me[1])):
        return False
    return other[1] + 0.011 >= me[1] and _is_satellite(*other)


BOOT_ID_RE = re.compile(r"^[0-9a-f-]{8,64}$")


def boot_time(paths: Paths, boot_id: str, current: float) -> float:
    """The boot time this home's satellites compute start times from during boot
    ``boot_id``: the first one's reading of /proc/stat's btime, kept in ``run/boot_time``.
    btime moves when the wall clock is stepped (a Pi without an RTC syncing NTP, a WSL2
    resume), and every reconnect starts a new satellite: without this, the same process
    would get a new start time, so its watched pair would read as dead and its MCP server
    as another process (§27.4.6)."""
    f = paths.run_dir / "boot_time"
    try:
        saved_id, saved = f.read_text(encoding="ascii").split()
        v = float(saved)
        if saved_id == boot_id and math.isfinite(v) and v >= 0:
            return v
    except (OSError, ValueError, UnicodeDecodeError):
        pass
    tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
    with contextlib.suppress(OSError):
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as out:
            out.write(f"{boot_id} {current!r}\n")
        os.replace(tmp, f)
    return current


def pin_linux_clock(paths: Paths) -> str:
    """Linux: read the process table with /proc only (``proc.set_no_spawn``) and from this
    boot's first boot time (``boot_time``). Returns what it did, for the log."""
    if not sys.platform.startswith("linux"):
        return "none"
    proc.set_no_spawn()
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as f:
            bid = f.read().strip()
        now_btime = proc.read_linux_btime()
    except (OSError, ValueError, UnicodeDecodeError):
        return "no boot id"
    if not BOOT_ID_RE.match(bid) or now_btime <= 0:
        return "no boot id"
    proc.pin_btime(boot_time(paths, bid, now_btime))
    return "pinned"


# ----------------------------------------------------------- local clients
class LocalConn:
    """One client of this machine on the satellite's socket."""

    def __init__(self, c: int, peer: Peer, writer: asyncio.StreamWriter):
        self.c = c
        self.peer = peer
        self.writer = writer
        self.q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=LOCAL_QUEUE)
        self.closed = False
        self.task: asyncio.Task[None] | None = None
        # the Claude this connection's MCP server was attested under at its last mcp.hello
        # (this machine's pid, start, messaging socket), or None: the only session a
        # deliver push on this connection may be for (§27.5.6)
        self.agent: tuple[int, float, str] | None = None

    def send(self, obj: dict[str, Any]) -> bool:
        if self.closed:
            return False
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        try:
            self.q.put_nowait(line)
        except asyncio.QueueFull:
            log.warning("local conn %d: output queue full; closing", self.c)
            self.close()
            return False
        return True

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            with contextlib.suppress(asyncio.QueueFull):
                self.q.put_nowait(None)

    async def run_writer(self) -> None:
        try:
            while True:
                line = await self.q.get()
                if line is None:
                    break
                self.writer.write(line)
                await self.writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.closed = True
            with contextlib.suppress(Exception):
                self.writer.close()


# -------------------------------------------------------------- satellite
class Satellite:
    def __init__(self, paths: Paths, name: str, *, test_mode: bool, sessions_dir: str, harden_state: str,
                 pid_shift: int = 0, clock_skew: float = 0.0, link_proto: int = proto.LINK_PROTO,
                 desktop: str = "", frame_log: str | None = None, stdio: str = "other"):
        self.paths = paths
        self.stdio = stdio
        self.name = name
        self.test_mode = test_mode
        self.sessions_dir = sessions_dir
        self.harden_state = harden_state
        self.shift = pid_shift
        self.skew = clock_skew
        self.link_proto = link_proto
        self.desktop = desktop
        me = proc.info(os.getpid())
        self.me = (os.getpid(), me.start if me else time.time())
        self.conns: dict[int, LocalConn] = {}
        self._ids = itertools.count(1)
        self.watch_n: int | None = None
        # (pid as the broker knows it, this machine's pid or None, start)
        self.watched: list[tuple[int, int | None, float]] = []
        # the watched Claude agents: pid as the broker knows it -> (this machine's pid or None,
        # start, the messaging socket its MCP server proved at hello)
        self.claude: dict[int, tuple[int | None, float, str | None]] = {}
        self._own_ids = itertools.count(1)  # this satellite's own requests: negative ids
        self.last_ping = time.monotonic()
        self.welcome: dict[str, Any] | None = None
        self.link_up_at: float | None = None
        self.hook_state = "not checked"
        self.server: asyncio.base_events.Server | None = None
        self.sock_ino: int | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.done: asyncio.Event | None = None
        self.bye_why: str | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self.frame_log = frame_log  # test mode only

    def _log_frame(self, way: str, data: bytes) -> None:
        if self.frame_log:
            with contextlib.suppress(OSError), open(self.frame_log, "ab") as f:
                f.write(way.encode() + b" " + data.rstrip(b"\n") + b"\n")

    def now(self) -> float:
        """This machine's clock (shifted in test mode, as a remote with a wrong clock)."""
        return time.time() + self.skew

    # ------------------------------------------------------------- frames
    def send(self, frame: dict[str, Any]) -> None:
        try:
            data = proto.encode(frame)
        except proto.FrameError:
            log.warning("a %s frame over the limit was not sent", frame.get("t"))
            return
        self.send_data(data)

    def send_data(self, data: bytes) -> None:
        """One encoded frame out on the link."""
        w = self.writer
        if w is None or w.transport.is_closing():
            return
        if w.transport.get_write_buffer_size() > OUT_BACKLOG_BYTES:
            log.warning("the link doesn't drain; ending")
            self.stop("eof")
            return
        self._log_frame(">", data)
        w.write(data)

    def stop(self, why: str) -> None:
        if self.bye_why is None:
            self.bye_why = why
        if self.done is not None:
            self.done.set()

    # --------------------------------------------------------------- pids
    def out_pid(self, pid: int) -> int:
        return pid + self.shift

    def in_pid(self, pid: int) -> int | None:
        p = pid - self.shift
        return p if p >= 1 else None

    def attest(self, peer: Peer, params: dict[str, Any]) -> dict[str, Any]:
        """``facts.attest`` for an ``mcp.hello``: the broker's own check, run here, on this
        machine's kernel peer and process table, with this home's Claude registry."""
        claimed = params.get("harness")
        claimed = claimed if isinstance(claimed, str) and claimed in HARNESSES else "unknown"
        sock = params.get("claude_socket")
        sock = sock if isinstance(sock, str) and len(sock) < 1024 else None
        ident = verify_mcp_peer(peer, claimed, claude_socket=sock, sessions_dir=self.sessions_dir,
                                test_mode=self.test_mode)
        a = {
            "harness": ident.harness,
            "mcp": [self.out_pid(ident.mcp_pid), ident.mcp_start],
            "agent": ([self.out_pid(ident.agent_pid), ident.agent_start]
                      if ident.agent_pid and ident.agent_start is not None else None),
            "evidence": ident.evidence,
            "tier_note": ident.tier_note,
            "claude_socket": ident.claude_socket,
        }
        try:
            proto.check_attest(a)
        except proto.FrameError:
            raise McpRefused("forbidden", "can't identify the MCP server process") from None
        return a

    def chain(self, peer: Peer) -> list[list[Any]] | None:
        """``facts.chain`` for a ``hook.event``: [pid, start, verdict] from the hook up, no argv."""
        if not peer.pid:
            return None
        chain = proc.ancestry(peer.pid, proto.MAX_CHAIN)
        if not chain or chain[0].pid != peer.pid or not proc.same_start(chain[0].start, peer.start):
            return None
        argvs = proc.argv_many(chain)
        return [[self.out_pid(p.pid), p.start, proto.verdict(argvs.get(p.pid, ""), match_agent)] for p in chain]

    # ------------------------------------------------------ local requests
    def ping_result(self) -> dict[str, Any]:
        return {"version": __version__, "pid": os.getpid(), "port": None, "test_mode": self.test_mode,
                "role": "satellite"}

    def status_result(self) -> dict[str, Any]:
        w = self.welcome or {}
        return {
            "role": "satellite",
            "name": self.name,
            "version": __version__,
            "pid": os.getpid(),
            "home": str(self.paths.home),
            "link": "up" if self.welcome is not None else "connecting",
            "since": self.link_up_at,
            "desktop": self.desktop or None,
            "desktop_version": w.get("version"),
            "rooms": w.get("rooms", []),
            "harnesses": w.get("harnesses", []),
            "hooks": self.hook_state,
            "harden": self.harden_state,
            "stdio": self.stdio,
            "test_mode": self.test_mode,
            "connections": len(self.conns),
        }

    def on_local_line(self, lc: LocalConn, line: bytes) -> None:
        def err(rid: Any, code: str, message: str) -> None:
            lc.send({"id": rid, "error": {"code": code, "message": message}})

        try:
            req = proto.loads(line)
        except (ValueError, UnicodeDecodeError):
            err(None, "bad_request", "invalid JSON")
            return
        if not isinstance(req, dict):
            err(None, "bad_request", "request must be an object")
            return
        rid, method = req.get("id"), req.get("method")
        params = req.get("params", {})
        params = {} if params is None else params
        if not isinstance(rid, int) or isinstance(rid, bool):
            err(None, "bad_request", "id must be an integer")
            return
        if not isinstance(method, str) or not isinstance(params, dict):
            err(rid, "bad_request", "method must be a string and params an object")
            return
        if method == "sys.ping":
            lc.send({"id": rid, "result": self.ping_result()})
            return
        if method == "sys.status":
            lc.send({"id": rid, "result": self.status_result()})
            return
        if method not in proto.REMOTE_METHODS:
            err(rid, "forbidden", ON_DESKTOP)
            return
        facts: dict[str, Any] | None = None
        if method == "mcp.hello":
            lc.agent = None
            try:
                a = self.attest(lc.peer, params)
            except McpRefused as e:
                err(rid, e.code, e.message)
                return
            facts = {"attest": a}
            mine = self.in_pid(a["agent"][0]) if a["harness"] == "claude" and a["agent"] else None
            if mine is not None and a["claude_socket"]:
                lc.agent = (mine, a["agent"][1], a["claude_socket"])
        elif method == "hook.event":
            ch = self.chain(lc.peer)
            if ch:
                facts = {"chain": ch}
        if self.skew and method in proto.TIME_PARAMS:
            # test mode: this machine's clients run on the same (wrong) clock
            field = proto.TIME_PARAMS[method][0]
            if proto.is_num(params.get(field)):
                params = {**params, field: params[field] + self.skew}
        params = proto.to_ages(method, params, self.now())
        try:
            data = proto.encode(proto.req(lc.c, {"id": rid, "method": method, "params": params}, facts))
        except proto.FrameError:
            data = b""
        if not data or len(data) > proto.MAX_REQ_FRAME:
            # re-encoded it grew past what the broker accepts from a link (numbers written longer)
            err(rid, "bad_request", "request too large")
            return
        self.send_data(data)

    async def serve_local(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        peer = Peer.from_socket(sock)
        if peer.uid != os.getuid() or len(self.conns) >= LOCAL_MAX_CONNS or self.welcome is None:
            writer.close()
            return
        lc = LocalConn(next(self._ids), peer, writer)
        self.conns[lc.c] = lc
        lc.task = asyncio.get_running_loop().create_task(lc.run_writer())
        self.send(proto.open_(lc.c))
        try:
            while not lc.closed:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    lc.send({"id": None, "error": {"code": "bad_request", "message": "line too long"}})
                    break
                except ConnectionError:
                    break
                if not line:
                    break
                if line.strip():
                    self.on_local_line(lc, line)
        finally:
            if self.conns.pop(lc.c, None) is not None:
                self.send(proto.close(lc.c))
            lc.close()
            if lc.task is not None:
                with contextlib.suppress(asyncio.TimeoutError, TimeoutError, Exception):
                    await asyncio.wait_for(lc.task, 2.0)

    # ------------------------------------------------------- link frames
    def on_frame(self, f: dict[str, Any]) -> None:
        t = f["t"]
        if t == "out":
            lc = self.conns.get(f["c"])
            if lc is None:
                return
            line = f["line"]
            rid = line.get("id")
            if proto.is_int(rid) and rid < 0:
                return  # the broker's answer to one of this satellite's own requests
            chk = f.get("chk")
            if (chk is not None or line.get("push") == "deliver") and not self.last_mile(lc, line, chk):
                return
            lc.send(line)  # chk rides beside the line, never inside it
        elif t == "close":
            lc = self.conns.pop(f["c"], None)
            if lc is not None:
                lc.close()
        elif t == "watch":
            self.watch_n = f["n"]
            self.watched = [(pid, self.in_pid(pid), start) for pid, start in f["procs"]]
            had_claude = bool(self.claude)
            self.claude = {pid: (self.in_pid(pid), start, sock) for pid, start, sock in f["claude"]}
            self.send_alive()
            if self.claude or had_claude:
                self.send_reg()  # at once, so a Claude that just joined has a view within a round trip
        elif t == "ping":
            self.last_ping = time.monotonic()
            self.send(proto.pong(f["n"]))
        elif t == "refuse":
            log.warning("the broker refused this link: %s", f["why"])
            self.stop("shutdown")
        elif t == "welcome":
            log.warning("a second welcome; ending")
            self.stop("shutdown")

    def send_alive(self) -> None:
        if self.watch_n is None:
            return
        # a pid below the test shift was never one of this machine's: gone, as far as it goes
        dead = [(pid, start) for pid, mine, start in self.watched if mine is None or not proc.alive(mine, start)]
        self.send(proto.alive(self.watch_n, dead))

    # ------------------------------------------------------------- Claude
    def claude_status(self, mine: int | None, start: float, sock: str | None) -> tuple[str | None, float | None]:
        """``(status, since)`` of a watched Claude agent on this machine, from this home's
        Claude registry, with the broker's own checks (``ClaudeAdapter.poll_once``): the
        file names this pid (or no pid) and the socket its MCP server proved (or none).
        ``(None, None)``: the process is gone or recycled, or the file is unreadable or
        another session's. ``since`` (``statusUpdatedAt``) is on this machine's clock.
        It never raises: a file nobody expected (a same-user process can write any) reads
        as unreadable, and the link and every other session's view carry on."""
        try:
            if mine is None or not proc.alive(mine, start):
                return None, None
            data = claude_registry.read_registry(self.sessions_dir, mine)
            if data is None or data.get("pid") not in (None, mine):
                return None, None
            path = data.get("messagingSocketPath")
            if sock and path is not None and path != sock:
                return None, None
            status, since = claude_registry.registry_status(data)
        except Exception:
            log.warning("pid %s: the Claude registry could not be read", mine)
            return None, None
        if status is None:
            return None, None
        # test mode: this machine's processes run on the same (wrong) clock as it does
        return status, (since + self.skew if since is not None else None)

    def send_reg(self) -> None:
        """The relayed registry of every watched Claude: statuses and ages, nothing else
        (no path, no session id, no text)."""
        t_read = self.now()
        read = [(pid, start, *self.claude_status(mine, start, sock))
                for pid, (mine, start, sock) in self.claude.items()]
        now = self.now()
        views = [(pid, start, status, (now - since) if status is not None and since is not None else None)
                 for pid, start, status, since in read]
        self.send(proto.reg(views, max(0.0, now - t_read)))

    def send_reg_if_watched(self) -> None:
        if self.claude:
            self.send_reg()

    def last_mile(self, lc: LocalConn, line: dict[str, Any], chk: dict[str, Any] | None) -> bool:
        """A ``deliver`` push (or any line that carries ``chk = {pid, start, want}``): relay
        it only if ``chk`` names the Claude this connection's MCP server was attested
        under, that Claude is watched and alive, and a read now says ``want``. This machine
        enforces the approval hold itself, whatever the desktop sends: nothing is ever
        posted into an approval prompt, and never into another session than ``chk`` names.
        Otherwise send a fresh ``reg`` (so the broker sees why), then this satellite's own
        ``mcp.posted {ok: false, err}`` for the batch, marked ``facts.lastmile``, and drop
        the push. ``err`` is ``stale_status`` for a status that changed after the broker's
        view (an uncounted re-route there), ``no_chk``/``bad_chk`` for a push the broker
        never sends (counted, so a broker fault shows as failed deliveries)."""
        err = proto.STALE_STATUS
        a = lc.agent
        if chk is None:
            err = proto.NO_CHK
        elif a is None or self.in_pid(chk["pid"]) != a[0] or not proc.same_start(a[1], chk["start"]):
            err = proto.BAD_CHK
        else:
            e = self.claude.get(chk["pid"])
            status = None
            if e is not None and proc.same_start(e[1], chk["start"]):
                status, _since = self.claude_status(a[0], a[1], a[2])
            if status == chk["want"]:
                return True
        log.info("conn %d: a push dropped (%s)", lc.c, err)
        self.send_reg()
        data = line.get("data")
        bid = data.get("batch_id") if line.get("push") == "deliver" and isinstance(data, dict) else None
        if proto.is_int(bid):
            posted = {"id": -next(self._own_ids), "method": "mcp.posted",
                      "params": {"batch_id": bid, "ok": False, "err": err}}
            self.send(proto.req(lc.c, posted, {"lastmile": True}))
        return False

    async def _read_link(self, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                line = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError):
                log.warning("a frame over the limit from the broker; ending")
                self.stop("shutdown")
                return
            except (ConnectionError, OSError):
                line = b""
            if not line:
                self.stop("eof")
                return
            self._log_frame("<", line)
            try:
                f = proto.decode(line, "b2s")
            except proto.FrameError as e:
                log.warning("a malformed frame from the broker (%s); ending", e.code)
                self.stop("shutdown")
                return
            if self.welcome is None:
                if f["t"] == "welcome":
                    self.welcome = f
                    self.welcomed.set()
                    continue
                if f["t"] == "refuse":
                    self.on_frame(f)
                    return
                continue  # nothing else before the welcome
            try:
                self.on_frame(f)
            except Exception:
                # one frame's failure drops that frame, never the link's reader (pings stop
                # being answered and every member of this host would go offline)
                log.exception("a %s frame from the broker failed", f["t"])

    async def _every(self, seconds: float, fn: Any) -> None:
        while True:
            await asyncio.sleep(seconds)
            try:
                fn()
            except Exception:
                log.exception("periodic task failed")

    def _check_ping(self) -> None:
        if time.monotonic() - self.last_ping > PING_TIMEOUT_S:
            log.warning("no ping for %.0f s; ending", PING_TIMEOUT_S)
            self.stop("eof")

    def _status(self) -> None:
        self.hook_state = hook_state_text(self.paths)
        self.send(proto.status(self.hook_state))

    # ------------------------------------------------------------- socket
    async def bind(self) -> None:
        path = self.paths.sock
        if path.exists() or path.is_symlink():
            st = os.lstat(path)
            if not stat.S_ISSOCK(st.st_mode):
                raise RuntimeError(f"{path} exists and is not a socket")
            path.unlink()  # a stale socket; we hold broker.lock, so no broker listens here
        old = os.umask(0o077)
        try:
            self.server = await asyncio.start_unix_server(self.serve_local, path=str(path), limit=MAX_LINE + 1)
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        self.sock_ino = os.lstat(path).st_ino

    def unlink(self) -> None:
        path = self.paths.sock
        with contextlib.suppress(OSError):
            if self.sock_ino is not None and os.lstat(path).st_ino == self.sock_ino:
                path.unlink()

    # ---------------------------------------------------------------- run
    async def _open_link(self, in_fd: int, out_fd: int) -> asyncio.StreamReader:
        """Streams over the link's fds. sshd may give stdin and stdout as one socket (a
        socketpair, as the exec transport does) or as two pipes; asyncio's pipe writer
        would take incoming data on a shared socket for a hang-up, so a socket is
        driven as a socket."""
        loop = asyncio.get_running_loop()
        si, so = os.fstat(in_fd), os.fstat(out_fd)
        if stat.S_ISSOCK(si.st_mode) and (si.st_dev, si.st_ino) == (so.st_dev, so.st_ino):
            os.close(in_fd)
            reader, self.writer = await asyncio.open_connection(sock=socket.socket(fileno=out_fd),
                                                                limit=proto.MAX_FRAME + 1)
            return reader
        reader = asyncio.StreamReader(limit=proto.MAX_FRAME + 1)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(in_fd, "rb", buffering=0))
        if stat.S_ISSOCK(so.st_mode):
            _r, self.writer = await asyncio.open_connection(sock=socket.socket(fileno=out_fd))
        else:
            wt, wp = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin,
                                                   os.fdopen(out_fd, "wb", buffering=0))
            self.writer = asyncio.StreamWriter(wt, wp, None, loop)
        return reader

    async def run(self, in_fd: int, out_fd: int) -> int:
        loop = asyncio.get_running_loop()
        self.done = asyncio.Event()
        self.welcomed = asyncio.Event()
        reader = await self._open_link(in_fd, out_fd)
        for sig, why in ((signal.SIGTERM, None), (signal.SIGINT, None), (signal.SIGHUP, "eof")):
            loop.add_signal_handler(sig, self._on_signal, why)
        self.hook_state = hook_state_text(self.paths)
        self.send(proto.hello(version=__version__, name=self.name, now=self.now(), hook_state=self.hook_state,
                              test_mode=self.test_mode, harden=self.harden_state, proto=self.link_proto))
        self._tasks.append(loop.create_task(self._read_link(reader)))
        try:
            await _wait_any(self.welcomed, self.done, timeout=WELCOME_TIMEOUT_S)
            if self.welcome is None:
                if not self.done.is_set():
                    log.warning("no welcome within %.0f s; ending", WELCOME_TIMEOUT_S)
                self.stop("shutdown")
            else:
                self.link_up_at = self.now()
                self.last_ping = time.monotonic()
                await self.bind()
                log.info("link up: satellite %s for %s", __version__, self.name)
                self._tasks.append(loop.create_task(self._every(ALIVE_S, self.send_alive)))
                self._tasks.append(loop.create_task(self._every(REG_S, self.send_reg_if_watched)))
                self._tasks.append(loop.create_task(self._every(1.0, self._check_ping)))
                self._tasks.append(loop.create_task(self._every(STATUS_S, self._status)))
                await self.done.wait()
        finally:
            await self.shutdown()
        return 0

    def _on_signal(self, why: str | None) -> None:
        if why is None:  # SIGTERM: a newer satellite taking over names itself in the replace marker
            why = "replaced" if replaced_by_other(self.paths, self.me) else "shutdown"
        self.stop(why)

    async def shutdown(self) -> None:
        why = self.bye_why or "shutdown"
        log.info("ending: %s", why)
        self.send(proto.bye(why))
        if self.server is not None:
            self.server.close()
        self.unlink()
        for lc in list(self.conns.values()):
            lc.close()
        self.conns.clear()
        for t in self._tasks:
            t.cancel()
        if self.writer is not None:
            with contextlib.suppress(Exception):
                self.writer.close()
                await asyncio.wait_for(self.writer.wait_closed(), 2.0)


async def _wait_any(*events: asyncio.Event, timeout: float) -> None:
    tasks = [asyncio.get_running_loop().create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()


# ------------------------------------------------------------------- main
def setup_logging(paths: Paths) -> None:
    """``logs/satellite.log`` (0600, rotating): ids and states only, never text."""
    handler = logging.handlers.RotatingFileHandler(paths.logs_dir / "satellite.log", maxBytes=2 * 1024 * 1024,
                                                   backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger("switchboard").setLevel(logging.INFO)


def _first_frame(out_fd: int, frame: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        os.write(out_fd, proto.encode(frame))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="switchboard satellite", description=argparse.SUPPRESS)
    ap.add_argument("--home", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--test-mode", action="store_true")
    args = ap.parse_args(argv)
    if not args.home or not os.path.isabs(args.home):
        # the forced command always names its home: never fall back on sshd's cwd
        print("switchboard satellite: --home must be an absolute path", file=sys.stderr)
        return EXIT_REFUSED
    os.umask(0o077)
    paths = Paths.from_home(args.home)
    why = start_refusal(paths, args.name, test_mode=args.test_mode, environ=os.environ)
    if why:
        print(f"switchboard satellite: {why}", file=sys.stderr)
        return EXIT_REFUSED
    shift, skew, link_proto, frame_log = 0, 0.0, proto.LINK_PROTO, None
    if args.test_mode:
        frame_log = os.environ.get("SWITCHBOARD_TEST_FRAME_LOG") or None
        try:
            shift = _env_int(os.environ, "SWITCHBOARD_TEST_PID_SHIFT", 0, 2**30)
            skew = _env_float(os.environ, "SWITCHBOARD_TEST_CLOCK_SKEW")
            link_proto = _env_int(os.environ, "SWITCHBOARD_TEST_LINK_PROTO", 0, 2**16) or proto.LINK_PROTO
        except ValueError as e:
            print(f"switchboard satellite: {e}", file=sys.stderr)
            return EXIT_REFUSED
    # before reading anything from the link: no same-user process may take this one over
    harden_state = harden()
    stdio = stdio_kind()
    # and none holds the link's stdio already (opened in the moment before harden(), or
    # inherited from the login shell): checked before the locks, so a refused satellite
    # never takes over a running one
    holders = exposure(link_fd_ids())
    # stdio hygiene: the link is private dups of fds 0 and 1; fd 1 becomes stderr (a stray
    # print can't corrupt the link) and fd 0 /dev/null (nothing else reads it)
    in_fd, out_fd = os.dup(0), os.dup(1)
    os.dup2(2, 1)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    try:
        paths.ensure()
    except UnsafePathError as e:
        print(f"switchboard satellite: unsafe path: {e}", file=sys.stderr)
        return EXIT_REFUSED
    setup_logging(paths)
    if holders:
        log.warning("refusing: other processes of this user hold the link's stdio: pids %s", holders[:16])
        _first_frame(out_fd, proto.bye("exposed"))
        return 0
    # before any start time is read (its own, the lock holder's, any client's)
    clock_state = pin_linux_clock(paths)
    log.info("start: %s (test mode %s, harden %s, stdio %s, process clock %s)", args.name, args.test_mode,
             harden_state, stdio, clock_state)
    from switchboard.config import ConfigError, load

    try:
        cfg = load(paths)
    except ConfigError as e:
        print(f"switchboard satellite: config error: {e}", file=sys.stderr)
        return EXIT_REFUSED
    me = proc.info(os.getpid())
    locks = take_locks(paths, (os.getpid(), me.start if me else time.time()))
    if isinstance(locks, str):
        log.warning("refusing: %s", locks)
        _first_frame(out_fd, proto.bye(locks))
        return 0
    try:
        conf = read_satellite_conf(paths)
    except (OSError, RemoteConfigError):
        conf = None
    write_hook_copy(paths)
    sat = Satellite(paths, args.name, test_mode=args.test_mode, sessions_dir=cfg.claude.sessions_dir,
                    harden_state=harden_state, pid_shift=shift, clock_skew=skew, link_proto=link_proto,
                    desktop=conf.desktop if conf else "", frame_log=frame_log, stdio=stdio)
    try:
        return asyncio.run(sat.run(in_fd, out_fd))
    finally:
        log.info("stopped")
        for fd in locks:
            with contextlib.suppress(OSError):
                os.close(fd)
