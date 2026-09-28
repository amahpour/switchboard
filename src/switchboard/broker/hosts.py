"""Host views: every process or registry probe about a participant (DESIGN.md §27.5.6).

A participant's pids are pids on its own host: ``''`` is this machine, any other
host is a remote whose satellite reports on it over the link (M8c). A probe of
this machine's process table for a remote participant would ask about some
unrelated local process, so nothing in the broker probes a participant directly:
it asks ``HostViews.view(p.host)``.

- ``LocalView`` answers from this machine's kernel (``broker.proc``) and this
  user's Claude session registry.
- ``RemoteView`` answers for one remote host, from what its link reports: the
  satellite's ``alive`` frames (M8c) and its relayed Claude registry (``reg``
  frames, M8d; ``registry``). It never asks this machine anything, and it can't
  walk a remote process chain (a remote hook brings its own, ``facts.chain``).
- A host nobody configured gets a fresh all-``None`` view, never the local one.

``None`` is never "dead": callers treat it as alive where a wrong "dead" would
let someone take a session over (a re-join, a Cursor bind), and skip the
participant where a wrong "dead" would end it (the liveness check).

``tests/unit/test_host_probes_static.py`` keeps every other module of the
package (``broker/proc.py`` aside) from calling ``proc.alive``, ``proc.ancestry``,
``proc.info``, ``proc.argv_many``, ``proc.argv`` or ``read_registry`` itself,
outside a short list of functions about this machine's own processes (a local
socket's kernel peer, the broker's pidfile, this machine's Codex daemon).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from switchboard.broker import proc
from switchboard.broker.proc import ProcInfo
from switchboard.claude_registry import read_registry
from switchboard.clock import Clock, SystemClock
from switchboard.models import LOCAL_HOST, Participant, valid_host
from switchboard.remote import proto


class LocalView:
    """This machine: its kernel and ``/proc``, and this user's Claude registry."""

    host = LOCAL_HOST

    def __init__(self, sessions_dir: str) -> None:
        self.sessions_dir = sessions_dir

    def alive(self, pid: int | None, start: float | None) -> bool | None:
        """The pid exists and started at ``start`` (never a recycled pid)."""
        return proc.alive(pid, start)

    def ancestry(self, pid: int, depth: int = 8) -> list[ProcInfo] | None:
        """[pid, parent, ...] up to ``depth`` entries (may be truncated)."""
        return proc.ancestry(pid, depth)

    def argv_many(self, procs: list[ProcInfo]) -> dict[int, str] | None:
        return proc.argv_many(procs)

    def read_registry(self, pid: int) -> dict[str, Any] | None:
        """``<sessions_dir>/<pid>.json`` of a Claude session on this machine, if readable."""
        return read_registry(self.sessions_dir, pid)


@dataclass(frozen=True)
class Relayed:
    """One Claude session's registry as its host's satellite read it (a ``reg`` frame),
    with the times on this broker's clock: ``status`` is None when the satellite couldn't
    read the file or it named another session; ``since`` None when it doesn't say."""

    status: str | None
    since: float | None
    read_at: float


class RemoteView:
    """One remote host, as its link reports it (DESIGN.md §27.5.6).

    ``alive(pid, start)``: ``False`` once the satellite has reported the pair dead
    (gone or recycled: final, a pid never comes back with the same start time);
    ``True`` while the pair is watched, an ``alive`` frame that answered a watch
    including it is at most ``FRESH_S`` old, and the link is up; ``None`` (can't
    tell) otherwise. Nothing is ever read from this machine.

    The broker's link (``broker/remote.py``) feeds it: ``link_up``/``link_down``,
    ``set_watch`` for every ``watch`` frame it sends, ``on_alive`` for every
    ``alive`` frame it receives, ``registry`` for every ``reg`` frame.

    ``registry(views, read_age, recv)``: the relayed Claude registry of the watched
    Claude sessions, rebased to this broker's clock (``read_at = recv - read_age``,
    ``since = recv - since_age``, each clamped, §27.4.6). Only pairs the broker
    watches are taken; the Claude adapter applies them (``ClaudeAdapter.relay``)."""

    FRESH_S = 3.0
    DEAD_MAX = 4096

    def __init__(self, host: str, clock: Clock | None = None) -> None:
        self.host = host
        self.clock: Clock = clock or SystemClock()
        self.up = False
        self._watched: dict[tuple[int, float], int] = {}  # pair -> the first watch seq that named it
        self._alive_at: dict[tuple[int, float], float] = {}  # pair -> when an alive frame vouched for it
        self._dead: dict[tuple[int, float], None] = {}  # insertion-ordered, bounded

    # ------------------------------------------------------------- feed
    def link_up(self) -> None:
        self.up = True
        self._alive_at.clear()

    def link_down(self) -> None:
        self.up = False
        self._alive_at.clear()

    def set_watch(self, n: int, pairs: set[tuple[int, float]]) -> None:
        """The broker sent watch ``n`` naming ``pairs``."""
        self._watched = {p: self._watched.get(p, n) for p in pairs}
        for p in list(self._alive_at):
            if p not in pairs:
                del self._alive_at[p]

    def on_alive(self, n: int, dead: list[tuple[int, float]]) -> set[tuple[int, float]]:
        """An ``alive`` frame answering watch ``n``: returns the watched pairs newly dead."""
        now = self.clock.now()
        new: set[tuple[int, float]] = set()
        gone = set(dead)
        for d in dead:
            if d not in self._dead:
                self._dead[d] = None
                if d in self._watched:
                    new.add(d)
        while len(self._dead) > self.DEAD_MAX:
            self._dead.pop(next(iter(self._dead)))
        for p, first in self._watched.items():
            if first <= n and p not in gone:
                self._alive_at[p] = now
            elif p in gone:
                self._alive_at.pop(p, None)
        return new

    def registry(self, views: list[tuple[int, float, str | None, float | None]], read_age: float,
                 recv: float) -> dict[tuple[int, float], Relayed]:
        """A ``reg`` frame received at ``recv``: the relayed status of each watched Claude
        pair (anything else in it is dropped), on this broker's clock."""
        read_at = proto.rebase_field("read_age", read_age, recv)
        if read_at is None:  # the frame's validator takes only numbers
            return {}
        out: dict[tuple[int, float], Relayed] = {}
        for pid, start, status, since_age in views:
            k = self._key(pid, start, self._watched)
            if k is None:
                continue
            since = proto.rebase_field("since_age", since_age, recv) if since_age is not None else None
            out[k] = Relayed(status=status, since=since, read_at=read_at)
        return out

    # ----------------------------------------------------------- answers
    def _key(self, pid: int, start: float, table: dict[tuple[int, float], Any]) -> tuple[int, float] | None:
        k = (pid, start)
        if k in table:
            return k
        for p in table:
            if p[0] == pid and proc.same_start(p[1], start):
                return p
        return None

    def alive(self, pid: int | None, start: float | None) -> bool | None:
        if not pid or start is None:
            return None
        if self._key(pid, start, self._dead) is not None:
            return False
        if not self.up:
            return None
        k = self._key(pid, start, self._alive_at)
        if k is None:
            return None
        return True if self.clock.now() - self._alive_at[k] <= self.FRESH_S else None

    def ancestry(self, pid: int, depth: int = 8) -> list[ProcInfo] | None:
        return None  # a remote hook brings its chain (facts.chain); nothing else walks one

    def argv_many(self, procs: list[ProcInfo]) -> dict[int, str] | None:
        return None

    def read_registry(self, pid: int) -> dict[str, Any] | None:
        # never a file on this machine: a remote session's registry arrives relayed
        # (``registry``), read on its own host by its satellite
        return None


HostView = LocalView | RemoteView


class HostViews:
    """The broker's views of every host it has members on."""

    def __init__(self, sessions_dir: str, clock: Clock | None = None) -> None:
        self.local = LocalView(sessions_dir)
        self.clock: Clock = clock or SystemClock()
        self._remotes: dict[str, RemoteView] = {}

    def add_remote(self, host: str) -> RemoteView:
        """The view of a configured remote (created once; the link feeds it)."""
        if not valid_host(host):
            raise ValueError(f"not a host name: {host!r}")
        v = self._remotes.get(host)
        if v is None:
            v = self._remotes[host] = RemoteView(host, self.clock)
        return v

    def remove_remote(self, host: str) -> None:
        self._remotes.pop(host, None)

    @property
    def remotes(self) -> dict[str, RemoteView]:
        return dict(self._remotes)

    def view(self, host: str) -> HostView:
        """``''`` is this machine; a configured remote its view; anything else an
        all-unknown view (never the local one)."""
        if host == LOCAL_HOST:
            return self.local
        v = self._remotes.get(host)
        return v if v is not None else RemoteView(host, self.clock)

    def alive(self, host: str, pid: int | None, start: float | None) -> bool | None:
        """Is ``(pid, start)`` on ``host`` alive? ``None``: this broker can't tell."""
        return self.view(host).alive(pid, start)

    def agent_alive(self, p: Participant) -> bool | None:
        """The participant's agent process (on its own host)."""
        return self.alive(p.host, p.agent_pid, p.agent_start)

    def mcp_alive(self, p: Participant) -> bool | None:
        """The participant's MCP server process (on its own host)."""
        return self.alive(p.host, p.mcp_pid, p.mcp_start)
