"""Remote hosts (DESIGN.md §27.4, §27.5): the broker's end of each link.

- ``RemoteManager``: one ``RemoteLink`` per ``[remote.<name>]`` in ``remotes.toml``.
  It dials a remote only while the ``remotes`` row holds the owner's consent for
  exactly the current config and no block (``RemoteRow.may_dial``); the config's
  hash is computed again from the files before every dial. Consent is human-only
  (``remote.enable``); an edit of the entry or its key files means "needs enable"
  again, and a narrowed ``rooms`` or ``harnesses`` ends the host's members that it
  no longer allows at once. It also ends the members of a host that has been
  unreachable for ``end_after_s`` (a removed remote's at once). Every change to a
  link (reload, enable, disable, shutdown) runs under one lock, so two of them
  never interleave.
- ``RemoteLink``: one child at a time with socketpair stdio: ``/usr/bin/ssh`` with
  the fixed argv of ``ssh_argv`` (§27.4.1), or, for a test-mode broker's exec
  transport, the satellite itself run on this machine; the noise-tolerant
  handshake; ``watch`` whenever the joined set of that host changes; pings; the
  state machine (disabled, connecting, up, down(reason), blocked(reason)) with
  backoff, where ssh's stderr and exit status name the reason (``classify_exit``:
  a host-key or auth failure blocks, a network failure retries); limits; notices
  in the remote's rooms. A frame from an abandoned child is dropped: it can
  never reach the broker. The
  satellite's ``reg`` frames (the relayed Claude registry) go through the host's
  ``RemoteView.registry`` to ``ClaudeAdapter.relay`` (M8d, §27.5.6).
- ``RemoteConn``: one connection of a client on the remote host, as the RPC
  server sees it. Its peer is ``RemotePeer(host)`` with no pid, uid or start
  time, so every local-identity check fails closed; ``rpc`` allows it only
  ``REMOTE_METHODS``. What its satellite vouched for (attest, hook chain) is kept
  per request (``rpc.REQUEST_FACTS``), never read from params.

``tests/unit/test_satellite_static.py`` checks this module has exactly one spawn site.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pwd
import random
import re
import secrets
import signal
import socket
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from switchboard import __version__
from switchboard.broker.peer import Peer
from switchboard.broker.rpc import Conn
from switchboard.broker.service import ServiceError
from switchboard.envelope import clean
from switchboard.models import Room, valid_host
from switchboard.paths import Paths
from switchboard.remote import proto
from switchboard.remote.config import (
    SSH_BIN,
    RemoteConfigError,
    RemoteEntry,
    entry_hash,
    host_key_alias,
    key_fingerprint,
    link_key_path,
    link_material,
    load_remotes,
    pin_path,
    remote_dir,
    remotes_path,
    ssh_files_problem,
    system_bin_problem,
)
from switchboard.remote.describe import BLOCK_HINTS, describe, fmt_ms

if TYPE_CHECKING:  # pragma: no cover
    from switchboard.broker.app import BrokerState
    from switchboard.broker.hosts import RemoteView

log = logging.getLogger("switchboard.remote")

BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 10.0)
BACKOFF_JITTER = 0.2
BACKOFF_RESET_UP_S = 60.0
PING_S = 2.0
PING_MISSES = 3  # unanswered pings: the link is down (about 6 s)
ENABLE_WAIT_S = 15.0
KILL_WAIT_S = 2.0
MAX_CONNS = 64
FRAME_RATE = 300  # satellite frames per second, averaged over FRAME_WINDOW_S
FRAME_BYTES_RATE = 8 * 1024 * 1024  # satellite bytes per second, averaged over FRAME_WINDOW_S
FRAME_WINDOW_S = 5.0
QUEUE_LINES = 5000  # the satellite's cap per local connection
SKEW_NOTICE_S = 5.0
STDERR_KEEP = 2048
MANAGER_TICK_S = 1.0
DEFAULT_END_AFTER_S = 900.0  # remotes.toml's default end_after_s
OUT_BACKLOG_BYTES = 16 * 1024 * 1024
REMOTES_EVENT_S = 0.2  # the web UI's `remotes` event is debounced (as the buddy list's)
STDERR_WAIT_S = 0.5  # after the child exited, for the last of its stderr


# ------------------------------------------------------------------ the ssh child
def ssh_argv(entry: RemoteEntry, paths: Paths) -> list[str]:
    """The ssh child's argv, exactly DESIGN.md §27.4.1: no config file (the owner's
    ``ControlMaster``, ``ForwardAgent``, ``RemoteForward`` or ``ProxyJump`` can never
    reach the link), no agent, only the link key, the host key pinned under its alias
    (an unknown or changed key aborts; no prompt of any kind), no forwarding of any
    kind, keepalives, and a fixed remote command that the forced command ignores."""
    return [
        SSH_BIN, "-F", "/dev/null", "-T", "-x", "-a", "-k", "-e", "none",
        "-i", str(link_key_path(paths, entry.name)), "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
        "-o", f"UserKnownHostsFile={pin_path(paths, entry.name)}", "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", f"HostKeyAlias={host_key_alias(entry.name)}", "-o", "StrictHostKeyChecking=yes",
        "-o", "UpdateHostKeys=no", "-o", "CheckHostIP=no",
        "-o", "BatchMode=yes", "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
        "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
        "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ClearAllForwardings=yes",
        "-o", "PermitLocalCommand=no",
        "-o", "LogLevel=ERROR", "-p", str(entry.port), "-l", entry.user, entry.host, "switchboard-satellite",
    ]


def passwd_home() -> str:
    """This user's home from the password database (what OpenSSH itself uses), never $HOME."""
    try:
        return pwd.getpwuid(os.getuid()).pw_dir or "/"
    except KeyError:
        return "/"


# ssh's stderr → why the link ended (§27.4.7). ssh relays the remote command's stderr to its
# own, so the buffer also holds whatever the remote printed (its login shell's rc files,
# the satellite, anything the forced command ran): text from the remote must never
# decide a block. A block is read from ssh's own fixed lines only (each whole line must
# match), only when ssh itself failed (exit 255) and only when nothing ever came back on
# the link's stdout (once the remote command runs, authentication and the host key have
# passed). No stderr text ever goes into a room notice: `remote status` shows it. A key file
# ssh refused comes before the auth failure it causes.
SSH_FAILED = 255  # ssh's own exit status for an error of its own (or a remote command's 255)
_SSH_BLOCKS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (reason, re.compile(pat, re.IGNORECASE)) for reason, pat in (
        ("host_key", r"host key verification failed\."
                     r"|@+ +warning: remote host identification has changed! +@+"
                     r"|no \S+ host key is known for \S+ and you have requested strict checking\."
                     r"|(?:\S+ )?host key for \S+ has changed and you have requested strict checking\."),
        ("files", r"@+ +warning: unprotected private key file! +@+"
                  r"|permissions 0[0-7]+ for '[^']*' are too open\."
                  r'|load key "[^"]*": (?:bad permissions|invalid format|error in libcrypto)'
                  r"|no such identity: .+|warning: identity file .+ not accessible: .+"),
        ("auth", r"\S+: permission denied \([a-z0-9,@.-]+\)\."
                 r"|received disconnect from \S+ port \d+:\d+: too many authentication failures(?: for \S+)?"
                 r"|no supported authentication methods available(?: \(server sent: [a-z0-9,@.-]*\))?"
                 r"|authentication failed\."),
        ("negotiate", r"unable to negotiate with \S+ port \d+: no matching .+"),
    )
)
# network trouble: down, retried with backoff (harmless if the remote's own text says it)
_SSH_DOWNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (reason, re.compile(pat, re.IGNORECASE)) for reason, pat in (
        ("dns", r"could not resolve hostname|name or service not known|nodename nor servname"
                r"|temporary failure in name resolution|no address associated with hostname"),
        ("refused", r"connection refused"),
        ("unreachable", r"no route to host|network is unreachable|host is down|host is unreachable"),
        ("keepalive", r"timeout, server \S+ not responding"),
        ("timeout", r"connection timed out|operation timed out|timed out during banner exchange"),
        ("closed", r"connection closed by|connection reset by|kex_exchange_identification|broken pipe"),
    )
)
EXIT_COMMAND = (126, 127)  # the remote's shell couldn't run the forced command
SATELLITE_REFUSED = 2  # remote/satellite.py EXIT_REFUSED, with "switchboard satellite: <why>" on stderr


def classify_exit(stderr: str, returncode: int | None, default: str = "eof", *,
                  ran: bool = False) -> tuple[str, str]:
    """``(state, reason)`` for an ssh child that ended (or whose link hit EOF).

    ``ran``: something came back on the link's stdout, so the remote command ran (the
    host key and the link key were accepted). Only when it didn't and ssh exited 255 does
    one of ssh's own lines block (``host_key``, ``auth``, ``files``, ``negotiate``: each
    needs the owner). Then network trouble is down and retried; the forced command not
    found or refused by the satellite blocks (it needs the owner on the remote); any
    other failure is ``exit <n>``, and a clean end is ``default``."""
    if not ran and returncode == SSH_FAILED:
        lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
        for reason, pat in _SSH_BLOCKS:
            if any(pat.fullmatch(ln) for ln in lines):
                return "blocked", reason
    for reason, pat in _SSH_DOWNS:
        if pat.search(stderr):
            return "down", reason
    if returncode in EXIT_COMMAND:
        return "blocked", "command"
    if returncode == SATELLITE_REFUSED and "switchboard satellite:" in stderr:
        return "blocked", "satellite"
    if returncode not in (None, 0):
        return "down", f"exit {returncode}"
    return "down", default


def last_line(text: str) -> str | None:
    """The last non-blank line of a child's stderr, cleaned for a notice or status."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return clean(lines[-1])[:200] if lines else None


def one_line(text: str, n: int = 200) -> str:
    """Text the remote wrote (its ``hook_state``), for the owner's status and the web UI:
    cleaned (no control, bidi or zero-width characters) and on one line."""
    return " ".join(clean(text).split())[:n]


def pinned_fingerprints(pin: str) -> list[str]:
    """``<type> SHA256:<b64>`` for each host key pinned in ``known_hosts`` (for the remotes
    panel: what an Enable trusts)."""
    out = []
    for line in pin.splitlines():
        words = line.split()
        if words and words[0].startswith("@"):
            words = words[1:]
        if len(words) >= 3:
            fp = key_fingerprint(f"{words[1]} {words[2]}")
            if fp:
                out.append(f"{words[1]} {fp}")
    return out


class LinkClosed(Exception):
    """An attempt ended: ``state`` is 'down' or 'blocked', ``reason`` a code."""

    def __init__(self, state: str, reason: str, notice: str | None = None, level: str = "warn",
                 detail: str | None = None):
        super().__init__(reason)
        self.state = state
        self.reason = reason
        self.notice = notice  # for the remote's rooms: never text from the remote or a local path
        self.level = level
        self.detail = detail  # for the owner only (`remote status`), never a room


@dataclass
class RemotePeer(Peer):
    """The peer of a remote connection: a process on another host. No pid, uid or
    start time on this machine, so ``AllowAllHumans``, ``ProcessPeerPolicy``,
    ``verify_mcp_peer`` and a local hook lookup all refuse it (§27.5.2)."""

    host: str = ""


class RemoteConn(Conn):
    """A connection of a client on a remote host, carried by that host's link."""

    remote = True

    def __init__(self, link: "RemoteLink", attempt: "Attempt", c: int):
        self.id = next(Conn._ids)  # the same counter as local connections: sinks keyed by id stay unique
        self.server = link.mgr.state.rpc
        self.writer = None  # type: ignore[assignment]
        self.peer = RemotePeer(pid=None, uid=None, start=None, host=link.name)
        self.out = None  # type: ignore[assignment]
        self.closed = False
        self.tails = []
        self.mcp = None
        self.link = link
        self.attempt = attempt
        self.c = c

    @property
    def host(self) -> str:  # type: ignore[override]
        return self.link.name

    @property
    def facts(self) -> dict[str, Any]:
        from switchboard.broker.rpc import REQUEST_FACTS

        return REQUEST_FACTS.get() or {}

    def send(self, obj: dict[str, Any]) -> None:
        if self.closed:
            return
        self.link.send_frame(self.attempt, proto.out(self.c, obj))

    def push_checked(self, kind: str, data: dict[str, Any], chk: dict[str, Any]) -> None:
        """A push the satellite checks just before relaying it (§27.5.6): ``chk``
        (``{pid, start, want}``, a Claude ``deliver``) rides beside the push, never
        inside it; the satellite strips it."""
        if self.closed:
            return
        self.link.send_frame(self.attempt, proto.out(self.c, {"push": kind, "data": data}, chk))

    def close(self) -> None:
        """The broker ends this connection: the satellite closes its client, and the
        local path's cleanup runs here (the satellite doesn't echo a ``close``)."""
        if not self.closed:
            self.link.send_frame(self.attempt, proto.close(self.c))
            if self.attempt.conns.get(self.c) is self:
                del self.attempt.conns[self.c]
            self.link._conn_gone(self)

    async def run_writer(self) -> None:  # pragma: no cover - frames go out through the link
        return None


@dataclass
class Attempt:
    """One child (one dial). Frames are accepted only from the link's current attempt."""

    n: int
    link_id: str
    proc: Any = None
    sock: socket.socket | None = None
    writer: asyncio.StreamWriter | None = None
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    conns: dict[int, RemoteConn] = field(default_factory=dict)
    stderr: bytearray = field(default_factory=bytearray)
    stderr_task: asyncio.Task[None] | None = None
    ran: bool = False  # a byte came back on stdout: the remote command ran (auth and host key passed)
    ping_n: int = 0
    pong_n: int = -1
    ping_sent: dict[int, float] = field(default_factory=dict)
    up_at: float | None = None
    frames: deque[float] = field(default_factory=deque)
    sizes: deque[tuple[float, int]] = field(default_factory=deque)
    window_bytes: int = 0
    conn_warned: bool = False
    first_pong: asyncio.Event = field(default_factory=asyncio.Event)
    abort: LinkClosed | None = None  # set from outside the frame loop (no pong, backlog)


class RemoteLink:
    def __init__(self, mgr: "RemoteManager", entry: RemoteEntry, config_hash: str):
        self.mgr = mgr
        self.entry = entry
        self.name = entry.name
        self.hash = config_hash
        self.view: RemoteView = mgr.state.hosts.add_remote(entry.name)
        self.state = "disabled"
        self.reason = "not_enabled"
        self.since = mgr.now()
        self.down_since = mgr.now()  # not up since (for end_after_s): broker start, or the last up
        self.attempt: Attempt | None = None
        self._seq = 0
        self.failures = 0
        self.retry_at: float | None = None
        self.task: asyncio.Task[None] | None = None
        self.wake = asyncio.Event()
        self.waiters: list[asyncio.Future[None]] = []
        self.rtt_ms: float | None = None
        self.sat_version: str | None = None
        self.sat_proto: int | None = None
        self.sat_test_mode = False
        self.harden: str | None = None
        self.skew_s: float | None = None
        self.hooks: str | None = None
        self.stderr_tail = ""
        self.end_detail: str | None = None  # why the last attempt ended, for `remote status` only
        self.watch_n = 0
        self._watch: tuple[frozenset[tuple[int, float]], tuple[tuple[int, float, str | None], ...]] | None = None
        self._skew_noted = False
        self._harden_noted = False
        self._version_noted: str | None = None
        self._down_noted: str | None = None  # a failing link's notice, once until it is next up
        self.last_up_for = 0.0  # how long the last attempt was up

    # ------------------------------------------------------------ helpers
    @property
    def st(self) -> "BrokerState":
        return self.mgr.state

    def now(self) -> float:
        return self.mgr.now()

    def row(self) -> Any:
        return self.st.store.remote_row(self.name)

    def files_changed(self) -> bool:
        """The entry's key files no longer match the hash this link was built with (an
        edit the manager hasn't reloaded yet): read again before every dial, never cached."""
        return entry_hash(self.st.paths, self.entry) != self.hash

    def may_dial(self) -> bool:
        row = self.row()
        return row is not None and row.may_dial(self.hash) and not self.files_changed()

    def consent_state(self) -> tuple[str, str]:
        """(state, reason) when not dialing: needs enable, disabled, or blocked."""
        row = self.row()
        if row is None:
            return "disabled", "not_enabled"
        if row.enabled_at is None:
            return "disabled", "disabled"  # enabled once, then `remote disable`
        if not row.enabled_for(self.hash) or self.files_changed():
            return "disabled", "config_changed"
        if row.blocked:
            return "blocked", row.blocked_reason or "blocked"
        return "disabled", "not_enabled"

    def _set(self, state: str, reason: str = "") -> None:
        if state == self.state and reason == self.reason:
            return
        was_up = self.state == "up"
        self.state, self.reason, self.since = state, reason, self.now()
        if was_up and state != "up":
            self.down_since = self.now()
        log.info("remote %s: %s%s", self.name, state, f" ({reason})" if reason else "")
        self.mgr.changed(self)

    def notice(self, text: str, level: str = "info") -> None:
        """A notice in each of the remote's rooms that exists."""
        svc = self.st.service
        for name in self.entry.rooms:
            room: Room | None = self.st.store.get_room(name)
            if room is not None:
                with contextlib.suppress(Exception):
                    svc.post_notice(room, text, level=level)

    def _wake_waiters(self) -> None:
        for f in self.waiters:
            if not f.done():
                f.set_result(None)
        self.waiters.clear()

    # ------------------------------------------------------------ control
    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.get_running_loop().create_task(self._supervise())
            if not self.may_dial():
                self._set(*self.consent_state())

    async def stop(self, state: str | None = None, reason: str = "") -> None:
        """End the supervisor and the attempt it owned (kill and reap the child). Only
        that attempt: never one a newer supervisor started meanwhile. The manager runs
        every stop under its lock, so none starts meanwhile anyway."""
        t, self.task = self.task, None
        a = self.attempt
        if t is not None:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if a is not None:
            # the supervisor normally ended it; a cancel during its cleanup leaves the rest here
            await self._end_attempt(a)
        if state is not None:
            self._set(state, reason)
        self._wake_waiters()

    async def restart(self) -> None:
        """Drop the current attempt, if any, and dial again now."""
        await self.stop()
        self.dial_now()

    def dial_now(self) -> None:
        """An enable: dial at once (no backoff). An attempt already under way is left to
        finish; its outcome is the enable's answer."""
        self.failures = 0
        if self.task is None:
            self.start()
        self.wake.set()

    # ---------------------------------------------------------- supervisor
    async def _supervise(self) -> None:
        while True:
            if not self.may_dial():
                self._set(*self.consent_state())
                self._wake_waiters()
                self.wake.clear()
                await self.wake.wait()
                continue
            self._set("connecting")
            self.retry_at = None
            e: LinkClosed | None = None
            try:
                await self._run_attempt()
            except asyncio.CancelledError:
                raise
            except LinkClosed as x:
                e = x
            except Exception:
                log.exception("remote %s: link failed", self.name)
                e = LinkClosed("down", "error")
            if self.task is not asyncio.current_task():
                return  # replaced by a newer supervisor: the link's state is its, not ours
            up_for = self._closed(e or LinkClosed("down", "eof"))
            if self.state == "blocked" or not self.may_dial():
                continue
            if up_for >= BACKOFF_RESET_UP_S:
                self.failures = 0
            self.failures += 1
            delay = BACKOFF_S[min(self.failures, len(BACKOFF_S)) - 1]
            delay *= random.uniform(1 - BACKOFF_JITTER, 1 + BACKOFF_JITTER)
            self.retry_at = self.now() + delay
            self.wake.clear()
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(self.wake.wait(), delay)

    def _closed(self, e: LinkClosed) -> float:
        """An attempt ended with ``e``: the new state, the block (persisted), notices."""
        was_up = self.state == "up"
        up_for = self.last_up_for
        self.end_detail = e.detail
        if e.state == "blocked":
            with contextlib.suppress(ValueError):
                self.st.store.set_remote_blocked(self.name, e.reason)
            self.notice(e.notice or self._blocked_text(e.reason, detail=False), "warn")
        elif e.notice:
            # a failure that repeats at every retry (a flood, a malformed hello) is told once
            # until the link is next up, not every 10 s
            if was_up or self._down_noted != e.reason:
                self.notice(e.notice, e.level)
            self._down_noted = e.reason
        elif was_up:
            self.notice(f"{self.name}: link down ({e.reason}); its members are offline", "warn")
        self._set(e.state, e.reason)
        self._wake_waiters()
        return up_for

    # ------------------------------------------------------------- attempt
    def _argv(self) -> list[str]:
        e = self.entry
        if e.transport == "exec":
            return [sys.executable, "-I", "-m", "switchboard", "satellite", "--home", e.home, "--name", e.name,
                    "--test-mode"]
        # checked before every dial: a binary someone could replace, or a link key or pin
        # that is missing or readable by others, needs the owner
        # (the notice, read by the remote's agents, names no path of this machine's user;
        # `remote status` shows the owner which file)
        why = system_bin_problem(SSH_BIN)
        if why:
            raise LinkClosed("blocked", "ssh_bin", self._blocked_text("ssh_bin"), detail=why)
        why = ssh_files_problem(self.st.paths, e.name)
        if why:
            raise LinkClosed("blocked", "files", self._blocked_text("files"), detail=why)
        return ssh_argv(e, self.st.paths)

    def _blocked_text(self, reason: str, *, detail: bool = True) -> str:
        """A blocked notice: the reason code and its fixed hint, never text the remote
        printed (agents in the remote's rooms read notices as the system's) nor a local
        path; ``detail``: ``remote status`` shows the owner more (stderr, the file)."""
        hint = BLOCK_HINTS.get(reason, reason).replace("<name>", self.name)
        more = f" (`switchboard remote status {self.name}` shows the detail)" if detail else ""
        return (f"{self.name}: link blocked ({reason}): {hint}{more}. Once fixed, run"
                f" `switchboard remote enable {self.name}` on this machine")

    def _env(self) -> dict[str, str]:
        """A clean env (§0): no harness variables, no agent socket, nothing but these. The
        ssh child gets exactly ``PATH`` and the passwd ``HOME`` (§27.4.1)."""
        if self.entry.transport == "ssh":
            return {"PATH": "/usr/bin:/bin", "HOME": passwd_home()}
        env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/"), "LANG": os.environ.get("LANG", "C.UTF-8")}
        if self.st.test_mode:
            for k, v in os.environ.items():
                if k == "SWITCHBOARD_TEST" or k.startswith("SWITCHBOARD_TEST_") or k in (
                        "TMPDIR", "COVERAGE_PROCESS_CONFIG"):
                    env[k] = v
        return env

    async def _spawn(self, a: Attempt) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        argv = self._argv()
        mine, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # the one spawn site of the broker's remote links: a fixed argv, no shell, a clean
            # env, stdin and stdout one end of a socketpair (a pipe could be reopened through
            # /proc/<pid>/fd, a socket can't), stderr kept for the reason (§27.4.1)
            a.proc = await asyncio.create_subprocess_exec(
                *argv, stdin=theirs.fileno(), stdout=theirs.fileno(), stderr=asyncio.subprocess.PIPE,
                env=self._env(), cwd="/", start_new_session=True, close_fds=True)
        except OSError:
            mine.close()
            raise LinkClosed("down", "spawn_failed") from None
        finally:
            theirs.close()
        a.sock = mine
        reader, writer = await asyncio.open_unix_connection(sock=mine, limit=proto.MAX_FRAME + 1)
        a.writer = writer
        a.stderr_task = asyncio.get_running_loop().create_task(self._read_stderr(a))
        a.tasks.append(a.stderr_task)
        return reader, writer

    async def _read_stderr(self, a: Attempt) -> None:
        stream = a.proc.stderr if a.proc is not None else None
        if stream is None:
            return
        with contextlib.suppress(Exception):
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                a.stderr += chunk
                if len(a.stderr) > STDERR_KEEP:
                    del a.stderr[:-STDERR_KEEP]

    async def _ended(self, a: Attempt, default: str) -> LinkClosed:
        """Why attempt ``a``'s child ended its link (EOF): for ssh, what its stderr and
        exit status say (``classify_exit``: a host-key or auth failure before the remote
        command ran blocks; the notice carries only the reason and its hint, and
        ``remote status`` the stderr); for the exec transport ``exit <n>``; else
        ``default``."""
        p = a.proc
        if p is None:
            return LinkClosed("down", default)
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(p.wait(), 1.0)
        if a.stderr_task is not None and p.returncode is not None:
            # the child is gone: its stderr ends too; take the last of it
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(a.stderr_task), STDERR_WAIT_S)
        rc = p.returncode
        if self.entry.transport != "ssh":
            return LinkClosed("down", f"exit {rc}" if rc not in (None, 0) else default)
        err = bytes(a.stderr).decode("utf-8", "replace")
        state, reason = classify_exit(err, rc, default, ran=a.ran)
        if state != "blocked":
            return LinkClosed(state, reason)
        return LinkClosed(state, reason, self._blocked_text(reason))

    async def _run_attempt(self) -> None:
        self._seq += 1
        a = Attempt(n=self._seq, link_id=secrets.token_hex(8))
        self.attempt = a
        try:
            reader, _writer = await self._spawn(a)
            try:
                first = await proto.read_hello(reader)
            except proto.ShellNoise:
                raise LinkClosed("blocked", "shell_noise") from None
            except proto.LinkEOF as x:
                a.ran = x.got > 0
                raise await self._ended(a, "eof") from None
            except TimeoutError:
                raise LinkClosed("down", "timeout") from None
            a.ran = True
            if a is not self.attempt:
                raise LinkClosed("down", "abandoned")
            self._handshake(a, first)
            await self._frames(a, reader)
        finally:
            await self._end_attempt(a)

    def _handshake(self, a: Attempt, first: dict[str, Any]) -> None:
        recv = self.now()
        if first.get("t") == "bye":
            why = first.get("why")
            if why in proto.BYE_BLOCKS:
                raise LinkClosed("blocked", why)
            raise LinkClosed("down", why if why in proto.BYE_WHY else "bye")
        try:
            h = proto.validate(first, "s2b")
        except proto.FrameError as e:
            raise LinkClosed("down", "malformed", f"{self.name}: a malformed hello from the satellite ({e.code})")
        if h["proto"] != proto.LINK_PROTO:
            self.send_frame(a, proto.refuse("proto", f"link protocol {h['proto']} is not {proto.LINK_PROTO}"))
            raise LinkClosed("blocked", "proto", f"{self.name}: link blocked (proto): the satellite"
                             f" {h.get('version', '?')} speaks link protocol {h['proto']}, this broker"
                             f" {__version__} speaks {proto.LINK_PROTO}: install the same switchboard version"
                             f" on both machines, then run `switchboard remote enable {self.name}`")
        if h["name"] != self.name:
            self.send_frame(a, proto.refuse("name", "not the remote this link dials"))
            raise LinkClosed("blocked", "name")
        if h["test_mode"] and not self.st.test_mode:
            self.send_frame(a, proto.refuse("test_mode", "a test-mode satellite needs a test-mode broker"))
            raise LinkClosed("blocked", "test_mode")
        self.sat_version, self.sat_proto = h["version"], h["proto"]
        self.sat_test_mode, self.harden, self.hooks = h["test_mode"], h["harden"], one_line(h["hook_state"])
        self.skew_s = round(h["now"] - recv, 2)
        self.send_frame(a, proto.welcome(
            version=__version__, link=a.link_id, rooms=list(self.entry.rooms),
            harnesses=list(self.entry.harnesses),
            limits={"max_conns": MAX_CONNS, "max_members": self.entry.max_members, "frame_rate": FRAME_RATE,
                    "queue_lines": QUEUE_LINES}))
        a.up_at = self.now()
        self._down_noted = None
        self.view.link_up()
        self._watch = None
        self.refresh_watch()
        with contextlib.suppress(ValueError):
            self.st.store.touch_remote_up(self.name)
        self._set("up")
        self._hello_notices()
        loop = asyncio.get_running_loop()
        a.tasks.append(loop.create_task(self._pinger(a)))
        a.tasks.append(loop.create_task(self._announce(a)))

    def _hello_notices(self) -> None:
        """What the hello says the owner should know, each once while it lasts: another
        switchboard version, a clock off by more than 5 s, and a satellite that could not
        make itself non-dumpable (``harden: failed``, Linux only: then same-user processes
        there can read and forge its link frames, §27.4.8, §27.12)."""
        if self.sat_version != __version__ and self._version_noted != self.sat_version:
            self._version_noted = self.sat_version
            self.notice(f"{self.name}: satellite {self.sat_version}, this broker {__version__}: the link works;"
                        " install the same version on both machines when you can")
        skewed = self.skew_s is not None and abs(self.skew_s) > SKEW_NOTICE_S
        if skewed and not self._skew_noted:
            ahead = "ahead" if (self.skew_s or 0.0) > 0 else "behind"
            self.notice(f"{self.name}'s clock is {abs(self.skew_s or 0.0):.0f} s {ahead}; delivery is unaffected,"
                        " check NTP")
        self._skew_noted = skewed
        failed = self.harden == "failed"
        if failed and not self._harden_noted:
            self.notice(f"{self.name}: the satellite could not make itself non-dumpable (prctl failed): other"
                        " processes of that user there can read and forge its link frames; check `switchboard"
                        " remote status`", "warn")
        self._harden_noted = failed

    async def _announce(self, a: Attempt) -> None:
        """The link-up notice, once the first pong gives an RTT (§27.5.8)."""
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(a.first_pong.wait(), PING_S * PING_MISSES)
        if a is not self.attempt or self.state != "up":
            return
        row = self.row()
        via = (row.enabled_via if row is not None else None) or "?"
        when = time.strftime("%Y-%m-%d", time.localtime(row.enabled_at)) if row is not None and row.enabled_at else "?"
        rtt = f"{fmt_ms(self.rtt_ms)} ms" if self.rtt_ms is not None else "?"
        self.notice(f"{self.name}: link up (enabled via {via} by {self.st.cfg.human_name} on {when};"
                    f" satellite {self.sat_version}, rtt {rtt})")
        self.mgr.changed(self)  # the chip's first RTT
        self._wake_waiters()

    async def _pinger(self, a: Attempt) -> None:
        while True:
            a.ping_n += 1
            a.ping_sent[a.ping_n] = time.monotonic()
            while len(a.ping_sent) > 16:
                a.ping_sent.pop(next(iter(a.ping_sent)))
            self.send_frame(a, proto.ping(a.ping_n))
            if a.ping_n - max(a.pong_n, 0) > PING_MISSES:
                self._abort(a, LinkClosed("down", "no_pong"))
                return
            await asyncio.sleep(PING_S)

    def _abort(self, a: Attempt, e: LinkClosed) -> None:
        """End attempt ``a`` from outside its frame loop (the loop raises ``e``)."""
        a.abort = e
        if a.writer is not None:
            with contextlib.suppress(Exception):
                a.writer.transport.abort()

    async def _frames(self, a: Attempt, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                line = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError):
                raise LinkClosed("down", "malformed", f"{self.name}: link closed: a frame over"
                                 f" {proto.MAX_FRAME} bytes from the satellite; reconnecting") from None
            except (ConnectionError, OSError):
                line = b""
            if a.abort is not None:
                raise a.abort
            if not line:
                raise await self._ended(a, "eof")
            if a is not self.attempt:
                # abandoned: nothing it sends reaches the broker, and its end is no clean end
                raise LinkClosed("down", "abandoned")
            self._count(a, len(line))
            try:
                f = proto.decode(line, "s2b")
            except proto.FrameError as e:
                raise LinkClosed("down", "malformed", f"{self.name}: link closed: a malformed frame from the"
                                 f" satellite ({e.code}); its members are offline, reconnecting") from None
            if f["t"] == "req" and len(line) > proto.MAX_REQ_FRAME:
                # a remote request is no larger than a local one (rpc.MAX_LINE, plus the envelope)
                raise LinkClosed("down", "malformed", f"{self.name}: link closed: a request over"
                                 f" {proto.MAX_LINE} bytes from the satellite; reconnecting")
            await self._on_frame(a, f)

    def _count(self, a: Attempt, size: int) -> None:
        """The flood limits (§27.4.5): frames and bytes a second, averaged over 5 s. Every
        frame counts; parsing is what they bound."""
        now = time.monotonic()
        a.frames.append(now)
        a.sizes.append((now, size))
        a.window_bytes += size
        while a.frames and now - a.frames[0] > FRAME_WINDOW_S:
            a.frames.popleft()
        while a.sizes and now - a.sizes[0][0] > FRAME_WINDOW_S:
            a.window_bytes -= a.sizes.popleft()[1]
        if len(a.frames) > FRAME_RATE * FRAME_WINDOW_S:
            raise LinkClosed("down", "flood", f"{self.name}: link closed: more than {FRAME_RATE} frames a"
                             " second from the satellite; its members are offline, reconnecting")
        if a.window_bytes > FRAME_BYTES_RATE * FRAME_WINDOW_S:
            raise LinkClosed("down", "flood", f"{self.name}: link closed: more than"
                             f" {FRAME_BYTES_RATE // (1024 * 1024)} MiB a second from the satellite;"
                             " its members are offline, reconnecting")

    async def _on_frame(self, a: Attempt, f: dict[str, Any]) -> None:
        """One validated frame of attempt ``a``. A frame of any other attempt (an
        abandoned child's) is dropped here: a reconnect can't block itself (§27.4.7)."""
        if a is not self.attempt:
            return
        t = f["t"]
        if t == "req":
            conn = a.conns.get(f["c"])
            if conn is None or conn.closed:
                return  # a request of a connection that is gone
            await self.st.rpc.dispatch_remote(conn, f["line"], f.get("facts"), self.now())
        elif t == "open":
            c = f["c"]
            if c in a.conns:
                raise LinkClosed("down", "malformed", f"{self.name}: link closed: connection {c} opened twice")
            if len(a.conns) >= MAX_CONNS:
                self.send_frame(a, proto.close(c))
                if not a.conn_warned:
                    a.conn_warned = True
                    self.notice(f"{self.name}: more than {MAX_CONNS} connections at once from that machine;"
                                " refusing new ones", "warn")
                return
            a.conns[c] = RemoteConn(self, a, c)
        elif t == "close":
            conn = a.conns.pop(f["c"], None)
            if conn is not None:
                self._conn_gone(conn)
        elif t == "alive":
            dead = self.view.on_alive(f["n"], f["dead"])
            if dead:
                # a watched process is gone: end its session now, not at the next liveness tick
                asyncio.get_running_loop().call_soon(self._check_liveness)
        elif t == "pong":
            sent = a.ping_sent.pop(f["n"], None)
            a.pong_n = max(a.pong_n, f["n"])
            if sent is not None:
                self.rtt_ms = round((time.monotonic() - sent) * 1000.0, 1)
                a.first_pong.set()
        elif t == "status":
            hooks = one_line(f["hook_state"])
            if hooks != self.hooks:
                self.hooks = hooks
                self.mgr.changed(self)
        elif t == "reg":
            self._on_reg(f)
        elif t == "bye":
            why = f["why"]
            if why in proto.BYE_BLOCKS:
                raise LinkClosed("blocked", why)
            raise LinkClosed("down", why)
        else:  # a second hello
            raise LinkClosed("down", "malformed", f"{self.name}: link closed: a second hello from the satellite")

    def _check_liveness(self) -> None:
        with contextlib.suppress(Exception):
            self.st.agents.check_liveness()

    def _claude(self) -> Any:
        engine = getattr(self.st, "engine", None)
        return engine.adapters.get("claude") if engine is not None else None

    def _on_reg(self, f: dict[str, Any]) -> None:
        """The relayed Claude registry (§27.5.6): the host's view rebases it to this
        broker's clock and keeps only watched pairs; the Claude adapter applies it as a
        local read (the approval hold, Esc-ended turns, the freshness a push needs)."""
        now = self.now()
        got = self.view.registry(f["views"], f["read_age"], now)
        adapter = self._claude()
        if adapter is None or getattr(adapter, "runner", None) is None:
            return
        try:
            acts = adapter.relay(self.name, got, now)
        except Exception:
            log.exception("remote %s: relayed registry failed", self.name)
            return
        if acts:
            self.st.runner.execute(acts)

    def _conn_gone(self, conn: RemoteConn) -> None:
        """The client on the remote host went away (or the link did): the local path's
        cleanup, as ``RpcServer._handle`` runs it."""
        conn.closed = True
        try:
            self.st.agents.conn_closed(conn)
        except Exception:
            log.exception("remote %s: conn %d cleanup failed", self.name, conn.id)

    async def _end_attempt(self, a: Attempt) -> None:
        """Abandon ``a``: drop its frames, close its connections (members go offline,
        waits and parks end), kill and reap its child, close its socket."""
        if self.attempt is a:
            self.attempt = None
            self.view.link_down()
            adapter = self._claude()
            if adapter is not None:
                adapter.forget_host(self.name)  # its relayed registry views are void now
        self.last_up_for = (self.now() - a.up_at) if a.up_at is not None else 0.0
        for conn in list(a.conns.values()):
            self._conn_gone(conn)
        a.conns.clear()
        me = asyncio.current_task()
        for t in a.tasks:
            if t is not me:
                t.cancel()
        if a.writer is not None:
            with contextlib.suppress(Exception):
                a.writer.close()
        p = a.proc
        if p is not None and p.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                p.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(p.wait(), KILL_WAIT_S)
            except (asyncio.TimeoutError, TimeoutError):
                with contextlib.suppress(ProcessLookupError):
                    p.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(p.wait(), 5.0)
        if a.sock is not None:
            with contextlib.suppress(OSError):
                a.sock.close()
        for t in a.tasks:
            if t is not me and not t.done():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        self.stderr_tail = bytes(a.stderr[-STDERR_KEEP:]).decode("utf-8", "replace")

    # --------------------------------------------------------------- frames
    def send_frame(self, a: Attempt | None, frame: dict[str, Any]) -> None:
        if a is None or a is not self.attempt or a.writer is None:
            return
        try:
            data = proto.encode(frame)
        except proto.FrameError:
            log.warning("remote %s: a frame over the limit was not sent (%s)", self.name, frame.get("t"))
            if frame.get("t") == "out":
                self.send_frame(a, proto.out(frame["c"], {"id": frame["line"].get("id"), "error": {
                    "code": "internal", "message": "reply too large for the link"}}))
            return
        tr = a.writer.transport
        if tr.is_closing():
            return
        if tr.get_write_buffer_size() > OUT_BACKLOG_BYTES:
            self._abort(a, LinkClosed("down", "backlog"))
            return
        a.writer.write(data)

    def refresh_watch(self) -> None:
        """Send ``watch`` when this host's joined set changed: the (pid, start) of every
        agent and MCP process of its joined members; Claude agents with their socket."""
        procs: set[tuple[int, float]] = set()
        claude: list[tuple[int, float, str | None]] = []
        for p in self.st.store.joined_participants():
            if p.host != self.name:
                continue
            for pid, start in ((p.agent_pid, p.agent_start), (p.mcp_pid, p.mcp_start)):
                if pid and start is not None:
                    procs.add((pid, start))
            if p.harness == "claude" and p.agent_pid and p.agent_start is not None:
                claude.append((p.agent_pid, p.agent_start, p.claude_socket))
        key = (frozenset(procs), tuple(sorted(claude)))
        if key == self._watch:
            return
        a = self.attempt
        if a is None or a.up_at is None:
            return
        self._watch = key
        self.watch_n += 1
        self.view.set_watch(self.watch_n, procs)
        self.send_frame(a, proto.watch(self.watch_n, sorted(procs), sorted(claude)))

    # --------------------------------------------------------------- status
    def members(self) -> list[str]:
        names: list[str] = []
        for p in self.st.store.joined_participants():
            if p.host != self.name:
                continue
            for m in self.st.store.participant_memberships(p.id):
                if m.screen_name not in names:
                    names.append(m.screen_name)
        return names

    def info(self) -> dict[str, Any]:
        row = self.row()
        up = self.state == "up"
        retry = None
        if self.state == "down" and self.retry_at is not None:
            retry = max(0.0, round(self.retry_at - self.now(), 1))
        return {
            "name": self.name,
            "state": self.state,
            "reason": self.reason or None,
            "since": self.since,
            "retry_in_s": retry,
            "rtt_ms": self.rtt_ms if up else None,
            "version": self.sat_version,
            "proto": self.sat_proto,
            "skew_s": self.skew_s,
            "hooks": self.hooks,
            "harden": self.harden,
            "test_mode": self.sat_test_mode,
            "transport": self.entry.transport,
            "attempts": self._seq,  # dials since the broker started (the backoff shows here)
            "rooms": list(self.entry.rooms),
            "harnesses": list(self.entry.harnesses),
            "max_members": self.entry.max_members,
            "members": self.members(),
            "enabled": bool(row is not None and row.enabled_for(self.hash)),
            "enabled_via": row.enabled_via if row is not None else None,
            "enabled_at": row.enabled_at if row is not None else None,
            "last_up_at": row.last_up_at if row is not None else None,
            # why the last child ended, in its own words (its stderr's last line), for the owner
            "detail": self._detail() if self.state in ("down", "blocked") else None,
            # what an enable consents to (§27.5.8): the web UI shows it and sends the hash back
            "config_hash": self.hash,
            "dest": self.dest(),
            "host_keys": self.host_keys(),
            "hint": self.hint(),
        }

    def dest(self) -> str:
        e = self.entry
        if e.transport == "exec":
            return f"exec: {e.home}"
        host = f"[{e.host}]" if ":" in e.host else e.host
        return f"{e.user}@{host}:{e.port}"

    def host_keys(self) -> list[str]:
        if self.entry.transport == "exec":
            return []
        return pinned_fingerprints(link_material(self.st.paths, self.name)[1])

    def hint(self) -> str | None:
        """What the owner can do about a link that isn't up (the panel's hint; ``describe``
        has the same words for the CLI)."""
        if self.state == "blocked":
            return str(BLOCK_HINTS.get(self.reason, self.reason)).replace("<name>", self.name)
        if self.state == "disabled" and self.reason == "config_changed":
            return ("its remotes.toml entry or key files changed since you enabled it: check the destination"
                    " and host key, then enable it again")
        if self.state == "disabled" and self.reason == "not_enabled":
            return "never enabled: check the destination and host key, then enable it"
        if self.state == "disabled" and self.reason == "disabled":
            return "disabled: enabling dials it again"
        return None

    def _detail(self) -> str | None:
        """For the owner (``remote status``): why the last attempt ended, or the last line
        of its child's stderr (text the remote may have printed: never a room notice)."""
        return self.end_detail or last_line(self.stderr_tail)


class RemoteManager:
    """Every remote of ``remotes.toml``; started after the RPC server (§27.3)."""

    def __init__(self, state: "BrokerState"):
        self.state = state
        self.links: dict[str, RemoteLink] = {}
        self.config_error: str | None = None
        self._stamp: tuple[tuple[int, int] | None, ...] | None = None
        self._task: asyncio.Task[None] | None = None
        self.running = False
        self.started_at = self.now()
        # reload, enable, disable and shutdown change links: one at a time (a stop of one
        # must never end the attempt another just started)
        self._ctl = asyncio.Lock()
        self._event: asyncio.TimerHandle | None = None  # the pending `remotes` web event
        self._event_had_links = False
        self._members_seen: dict[str, tuple[str, ...]] = {}  # each host's member names, last published

    def now(self) -> float:
        return self.state.clock.now()

    def changed(self, link: RemoteLink | None = None) -> None:
        """A link's state, its RTT, its members or the set of remotes changed: the web
        UI's ``remotes`` event (every remote's state, as ``GET /api/remotes`` has it),
        debounced to one per ``REMOTES_EVENT_S`` (§27.11)."""
        if self._event is not None or not self.running:
            return  # one pending already; or the manager is starting (the page asks) or shutting down
        if not self.links and not self._event_had_links:
            return  # no remotes, before or now: nothing for the UI
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop (a unit test): publish at once
            self._publish()
            return
        self._event = loop.call_later(REMOTES_EVENT_S, self._publish)

    def _publish(self) -> None:
        self._event = None
        self._event_had_links = bool(self.links)
        hub = self.state.hub
        if hub is None:
            return
        try:
            hub.remotes_changed(self.summary(), self.config_error)
        except Exception:
            log.exception("remotes event failed")

    # --------------------------------------------------------------- config
    def _file_stamp(self) -> tuple[tuple[int, int] | None, ...]:
        """(mtime, size) of ``remotes.toml`` and of each remote's key files (the link key
        and the pinned host key, which ``config_hash`` covers): any edit is seen within a
        second."""
        files = [remotes_path(self.state.paths)]
        for name in sorted(self.links):
            d = remote_dir(self.state.paths, name)
            files += [d / "id_ed25519.pub", d / "known_hosts"]
        out: list[tuple[int, int] | None] = []
        for f in files:
            try:
                st = os.stat(f)
                out.append((st.st_mtime_ns, st.st_size))
            except OSError:
                out.append(None)
        return tuple(out)

    async def reload(self) -> None:
        async with self._ctl:
            await self._reload()

    async def _reload(self) -> None:
        """Re-read ``remotes.toml`` (under ``_ctl``): new remotes get a link, changed ones
        need a new enable (their link stops), removed ones stop (their members are ended
        by the sweeper). Members a remote no longer allows are ended at once. A file that
        doesn't parse changes nothing (and ends nobody)."""
        self._stamp = self._file_stamp()
        try:
            entries = load_remotes(self.state.paths, test_mode=self.state.test_mode)
        except RemoteConfigError as e:
            if self.config_error != str(e):
                log.warning("remotes.toml refused: %s", e)
                self.config_error = str(e)
                self.changed()  # the page's "remotes.toml: not read" chip
            return
        self.config_error = None
        for name, entry in entries.items():
            h = entry_hash(self.state.paths, entry)
            link = self.links.get(name)
            if link is None:
                link = self.links[name] = RemoteLink(self, entry, h)
                if self.running:
                    link.start()
            elif link.hash != h or link.entry != entry:
                log.info("remote %s: config changed", name)
                was_up = link.state == "up"
                await link.stop()
                link.entry, link.hash = entry, h
                if was_up:
                    link.notice(f"{name}: remotes.toml changed (its entry or key files); the link needs a new"
                                f" enable (`switchboard remote enable {name}`); its members are offline", "warn")
                if self.running:
                    link.start()
            self.enforce(entry)
        for name in [n for n in self.links if n not in entries]:
            link = self.links.pop(name)
            await link.stop("disabled", "removed")
            self.state.hosts.remove_remote(name)
        self.changed()

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await self.reload()
        self.running = True
        for link in self.links.values():
            link.start()
        self._task = asyncio.get_running_loop().create_task(self._tick_loop())

    async def stop(self) -> None:
        self.running = False
        if self._event is not None:
            self._event.cancel()
            self._event = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        async with self._ctl:
            await asyncio.gather(*(link.stop() for link in self.links.values()), return_exceptions=True)

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(MANAGER_TICK_S)
            try:
                if self._file_stamp() != self._stamp:
                    await self.reload()
                self.sweep()
            except Exception:
                log.exception("remotes tick failed")

    def sweep(self) -> None:
        """End the members of a host unreachable for ``end_after_s`` (a removed remote's
        at once), so no ghosts stay in the buddy list (§27.4.7), and forget the consent of
        a remote that is no longer configured."""
        now = self.now()
        doomed: set[str] = set()
        for p in self.state.store.joined_participants():
            if not p.host or p.host in doomed:
                continue
            link = self.links.get(p.host)
            if link is None:
                if self.config_error is not None:
                    continue  # a remotes.toml that doesn't parse ends nobody
                if self.state.store.remote_row(p.host) is not None:
                    doomed.add(p.host)  # removed from remotes.toml: at once
                elif now - self.started_at >= DEFAULT_END_AFTER_S:
                    doomed.add(p.host)  # a host never configured here: the default wait
            elif link.state != "up" and now - max(link.down_since, self.started_at) >= link.entry.end_after_s:
                doomed.add(p.host)
        for host in sorted(doomed):
            self.end_members(host)
        if self.config_error is None:
            # the consent of a remote no longer in remotes.toml (a `remote remove` while the
            # broker was down, a table deleted by hand) goes with it: re-added, it needs a new
            # enable even with the very same files (§27.5.8). Its members were ended above.
            for row in self.state.store.remote_rows():
                if row.name not in self.links:
                    with contextlib.suppress(ValueError):
                        self.state.store.clear_remote(row.name)
                    log.info("remote %s: not in remotes.toml, its consent is forgotten", row.name)

    def enforce(self, entry: RemoteEntry) -> int:
        """The allowlists are a boundary, not a join-time gate (§27.5.2): a member of this
        host in a room its entry no longer lists leaves that room, and a session whose
        harness it no longer lists is ended. Run at every reload, so a narrowed entry (or
        one narrowed while the broker was down) revokes at once, not at the next enable."""
        st = self.state
        n = 0
        for p in st.store.joined_participants():
            if p.host != entry.name:
                continue
            harness_ok = p.harness in entry.harnesses or p.harness in ("unknown", "test")
            doomed = []
            for m in st.store.participant_memberships(p.id):
                room = st.store.room_by_id(m.room_id)
                if not harness_ok or room is None or room.name not in entry.rooms:
                    doomed.append(m)
            if not doomed:
                continue
            if harness_ok:
                for m in doomed:
                    st.store.end_membership(m.id, "not_allowed")
            else:
                doomed = st.store.end_participant(p.id, "not_allowed")
            for m in doomed:
                n += 1
                room = st.store.room_by_id(m.room_id)
                st.agents.run(st.engine.on_membership_ended(m.id, "not_allowed"))
                if room is None:
                    continue
                why = (f"{room.name} is no longer allowed for {entry.name}" if harness_ok
                       else f"{p.harness} is no longer allowed on {entry.name}")
                st.store.add_event("leave", room_id=room.id, membership_id=m.id, participant_id=p.id,
                                   data={"why": "not_allowed"})
                st.service._post(room, sender_name=m.screen_name, sender_kind="agent", sender_harness=p.harness,
                                 sender_membership_id=m.id, via="system", kind="leave", text=f"left ({why})",
                                 sender_host=entry.name)
        if n:
            log.info("remote %s: %d membership(s) no longer allowed, ended", entry.name, n)
            st.agents.refresh_index()
        return n

    def end_members(self, host: str, why: str = "unreachable") -> int:
        """End every member of ``host``: "left (<host> unreachable)" (or ``removed``)."""
        st = self.state
        n = 0
        for p in st.store.joined_participants():
            if p.host != host:
                continue
            n += 1
            ended = st.store.end_participant(p.id, why)
            for m in ended:
                st.agents.run(st.engine.on_membership_ended(m.id, why))
                room = st.store.room_by_id(m.room_id)
                if room is not None:
                    st.service._post(room, sender_name=m.screen_name, sender_kind="agent", sender_harness=p.harness,
                                     sender_membership_id=m.id, via="system", kind="leave",
                                     text=f"left ({host} {why})", sender_host=host)
        if n:
            log.info("remote %s: %d member(s) ended (%s)", host, n, why)
            st.agents.refresh_index()
        return n

    def refresh_watch(self) -> None:
        """A host's joined set may have changed: its ``watch``; and the web UI's event, only
        when a host's member names did change (this runs on every liveness tick)."""
        for link in self.links.values():
            link.refresh_watch()
        members = {name: tuple(link.members()) for name, link in self.links.items()}
        if members != self._members_seen:
            self._members_seen = members
            self.changed()

    # ------------------------------------------------------------ commands
    async def _link(self, name: str) -> RemoteLink:
        """The link of ``name`` after a fresh read of ``remotes.toml`` (under ``_ctl``)."""
        await self._reload()
        if self.config_error is not None:
            raise ServiceError("bad_request", f"remotes.toml: {self.config_error}")
        link = self.links.get(name)
        if link is None:
            raise ServiceError("not_found", f"no remote named {name[:40]} in remotes.toml")
        return link

    async def enable(self, name: str, *, via: str, actor: str = "", expect_hash: str | None = None) -> dict[str, Any]:
        """The owner's consent for this remote's current config; dial now and wait (up
        to 15 s) for the link to come up, block or fail (§27.5.8). Two enables at once,
        or an enable beside a reload, share one attempt: none ends the other's. With
        ``expect_hash`` (the web UI's Enable: the ``config_hash`` the page showed), the
        consent is for that config only: a config changed since is a conflict."""
        loop = asyncio.get_running_loop()
        async with self._ctl:
            link = await self._link(name)
            if expect_hash is not None and expect_hash != link.hash:
                self.changed(link)  # the page gets the config as it is now
                raise ServiceError("conflict", f"{name}: remotes.toml or its key files changed since the page showed"
                                               " them; check the remote's destination and host key, then enable again")
            self.state.store.set_remote_enabled(name, link.hash, via)
            self.state.store.add_event("remote", data={"what": "enable", "name": name, "via": via})
            log.warning("remote %s enabled via %s (%s)", name, via, actor or "?")
            self.changed(link)
            if link.state == "up" and link.attempt is not None:
                return self._result(link)
            # registered before the supervisor next runs: this waiter sees its next outcome
            fut: asyncio.Future[None] = loop.create_future()
            link.waiters.append(fut)
            link.dial_now()
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(fut, ENABLE_WAIT_S)
        return self._result(link)

    def _result(self, link: RemoteLink) -> dict[str, Any]:
        info = link.info()
        if info["state"] == "up":
            skew = info.get("skew_s") or 0.0
            rtt = f"{fmt_ms(info['rtt_ms'])} ms" if info.get("rtt_ms") is not None else "rtt ?"
            hooks = info.get("hooks") or "?"
            info["text"] = (f"link ok: satellite {info['version']} (proto {info['proto']}), rtt {rtt},"
                            f" clock {skew:+.2f} s, remote hooks {hooks}")
        else:
            info["text"] = describe(info)
        return info

    async def disable(self, name: str, *, via: str) -> dict[str, Any]:
        async with self._ctl:
            link = await self._link(name)
            self.state.store.set_remote_disabled(name)
            self.state.store.add_event("remote", data={"what": "disable", "name": name, "via": via})
            was = link.state
            await link.stop("disabled", "disabled")
            link.start()
            if was in ("up", "connecting"):
                link.notice(f"{name}: link disabled by {self.state.cfg.human_name} (via {via});"
                            " its members are offline", "warn")
            return self._result(link)

    async def remove(self, name: str, *, via: str) -> dict[str, Any]:
        """``switchboard remote remove`` (human only): stop the link, end the host's members
        at once ("left (<name> removed)") and drop its consent row (§27.5.8). The entry
        may already be gone from ``remotes.toml`` (the CLI edits it too); a later
        re-add has a new link key, so no old consent can ever match it."""
        if not valid_host(name):
            raise ServiceError("bad_request", "remote names look like fpga-pi")
        async with self._ctl:
            old = self.links.get(name)
            rooms = tuple(old.entry.rooms) if old is not None else ()
            was_up = old is not None and old.state == "up"
            await self._reload()
            link = self.links.pop(name, None)
            if link is not None:
                await link.stop("disabled", "removed")
                self.state.hosts.remove_remote(name)
            ended = self.end_members(name, "removed")
            had_row = self.state.store.clear_remote(name)
            self.state.store.add_event("remote", data={"what": "remove", "name": name, "via": via})
            log.warning("remote %s removed via %s", name, via)
            if old is not None and (was_up or ended):
                for room_name in rooms:
                    room = self.state.store.get_room(room_name)
                    if room is not None:
                        with contextlib.suppress(Exception):
                            self.state.service.post_notice(
                                room, f"{name}: remote removed by {self.state.cfg.human_name} (via {via});"
                                      " its link is closed and its members were ended", level="warn")
        return {"name": name, "ended": ended, "had_row": had_row, "configured": old is not None}

    async def status(self, name: str | None = None) -> dict[str, Any]:
        if self._file_stamp() != self._stamp:
            await self.reload()
        links = [self.links[n] for n in sorted(self.links) if name is None or n == name]
        if name is not None and not links:
            raise ServiceError("not_found", f"no remote named {name[:40]} in remotes.toml")
        return {"remotes": [self._result(link) for link in links], "config_error": self.config_error}

    def summary(self) -> list[dict[str, Any]]:
        """``sys.status``'s ``remotes`` (§27.11)."""
        return [self._result(self.links[n]) for n in sorted(self.links)]
