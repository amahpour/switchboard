"""Host views: every process or registry probe about a participant (DESIGN.md §27.5.6).

A participant's pids are pids on its own host: ``''`` is this machine. A session over HTTPS (§49.3)
has a host of its own, its device, and no pids at all, so a probe of this machine's process table
for it would ask about some unrelated local process. Nothing in the broker probes a participant
directly: it asks ``HostViews.view(p.host)``.

- ``LocalView`` answers from this machine's kernel (``broker.proc``) and this user's Claude session
  registry.
- Any other host gets a view that can't tell anything (every answer is ``None``), never the local one.

``None`` is never "dead": callers treat it as alive where a wrong "dead" would let someone take a
session over (a re-join, a Cursor bind), and skip the participant where a wrong "dead" would end it
(the liveness check).

``tests/unit/test_host_probes_static.py`` keeps every other module of the package (``broker/proc.py``
aside) from calling ``proc.alive``, ``proc.ancestry``, ``proc.info``, ``proc.argv_many``,
``proc.argv`` or ``read_registry`` itself, outside a short list of functions about this machine's own
processes (a local socket's kernel peer, the broker's pidfile, this machine's Codex daemon).
"""

from __future__ import annotations

from typing import Any

from switchboard.broker import proc
from switchboard.broker.proc import ProcInfo
from switchboard.claude_registry import read_registry
from switchboard.models import LOCAL_HOST, Participant


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


class UnknownView:
    """A host this broker can't look at: every answer is "can't tell"."""

    def __init__(self, host: str) -> None:
        self.host = host

    def alive(self, pid: int | None, start: float | None) -> bool | None:
        return None

    def ancestry(self, pid: int, depth: int = 8) -> list[ProcInfo] | None:
        return None

    def argv_many(self, procs: list[ProcInfo]) -> dict[int, str] | None:
        return None

    def read_registry(self, pid: int) -> dict[str, Any] | None:
        return None


HostView = LocalView | UnknownView


class HostViews:
    """The broker's views of every host it has members on."""

    def __init__(self, sessions_dir: str) -> None:
        self.local = LocalView(sessions_dir)

    def view(self, host: str) -> HostView:
        """``''`` is this machine; anything else an all-unknown view (never the local one)."""
        return self.local if host == LOCAL_HOST else UnknownView(host)

    def alive(self, host: str, pid: int | None, start: float | None) -> bool | None:
        """Is ``(pid, start)`` on ``host`` alive? ``None``: this broker can't tell."""
        return self.view(host).alive(pid, start)

    def agent_alive(self, p: Participant) -> bool | None:
        """The participant's agent process (on its own host)."""
        return self.alive(p.host, p.agent_pid, p.agent_start)

    def mcp_alive(self, p: Participant) -> bool | None:
        """The participant's MCP server process (on its own host)."""
        return self.alive(p.host, p.mcp_pid, p.mcp_start)
