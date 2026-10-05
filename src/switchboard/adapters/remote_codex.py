"""Codex on a remote host (DESIGN.md §27.7, issue #63): tiers ``codex:link`` and ``codex:hook``.

Every broker-side Codex push path (``turn/start``, ``turn/steer``, ``codex queue``, the
lsof check of attached TUIs) talks to a Codex app-server on the broker's own machine
(``adapters/codex.py``), so none of it can reach a thread on another host. A remote
thread is woken **on its own machine** instead, by its own ``switchboard mcp`` server:

- **codex:link.** The thread's MCP server (on a satellite home, with a usable Codex
  control socket there) attached its link connection as the wake channel of the
  threads it serves (``mcp.attach {codex: true}``). An idle wake is a ``deliver`` push
  on it, with ``chk = {pid, start, want: idle}`` naming the thread's Codex process: the
  satellite relays it only to the MCP server attested under that process, while it
  lives, and the server starts the turn through that machine's app-server after its
  own checks (a Codex TUI attached, the thread proof, a fresh ``idle`` status;
  ``mcp/codex_wake.py``), then answers ``mcp.posted``. Accepted is confirmed, as a
  local ``turn/start`` is confirmed by the RPC's success (evidence
  ``link:turn/start``). A refusal because the thread is busy or unloaded right now
  (``not_idle``, ``not_loaded``, or the satellite's ``stale_status``) is an uncounted
  re-route; a few in a row back off too. Any other refusal is a counted failure with
  a backoff (1 s doubling to 30 s).
- **codex:hook** (pull only): no wake channel (no control socket on that machine, an
  MCP server that joined before the link, an older switchboard there). An open
  ``wait()`` (240 s) serves it when idle.
- **Mid-task,** either way: PostToolUse context (no ``turn/steer`` over the link yet).
- **SessionEnd** marks the thread ended: no wake until a later hook of that thread
  shows it running again (as locally).

Its thread id is taken from ``_meta.threadId`` (the credential check keys it by host);
the thread proof is made on its own machine, before its first wake. ``CodexAdapter``
never sees a remote row.
"""

from __future__ import annotations

import asyncio
from typing import Any

from switchboard.adapters.base import HOOK_CONTEXT_EVENTS, PullAdapter, SendError
from switchboard.clock import Clock, SystemClock
from switchboard.config import Config
from switchboard.models import Batch, HookEvent, Participant, Release, Route, split_session_key
from switchboard.remote.proto import STALE_STATUS

TIER = "codex:hook"
NOTE = "remote Codex: pull only"
TIER_LINK = "codex:link"
PATH = "turn_start"  # the same path name as a local Codex idle wake (reports, the Inspector)

POST_TIMEOUT_S = 20.0  # the server's lsof run, a thread/read and the turn/start
SEND_BACKOFF_S = (1.0, 30.0)
REROUTE_FREE = 2
REROUTE_BACKOFF_S = (1.0, 30.0)
# refusals that mean "not right now": the thread is busy, unloaded, or changed under the frame
REROUTE_ERRS = frozenset({"not_idle", "not_loaded", STALE_STATUS})
RESUME_EVENTS = frozenset({"UserPromptSubmit", "PostToolUse", "Stop", "Interrupt"})


def _host(p: Any) -> str:
    return getattr(p, "host", "") or ""


def thread_of(p: Participant) -> str:
    """The thread id of a remote Codex session (its key is ``codex@<host>:<thread id>``), else ''."""
    parts = split_session_key(p.session_key)
    return parts[2] if parts is not None and parts[0] == "codex" and parts[1] else ""


class RemoteCodexAdapter(PullAdapter):
    serial_push = True  # one wake in flight per thread

    def __init__(self, cfg: Config):
        super().__init__("codex", cfg)
        # (host, mcp pid) -> (mcp start, the link connection): the attached wake channels
        self.conns: dict[tuple[str, int], tuple[float | None, Any]] = {}
        self.pending_posts: dict[int, tuple[asyncio.Future[dict[str, Any]], Any]] = {}
        self.backoff: dict[int, tuple[float, int]] = {}  # participant id -> (until, failures)
        self.reroutes: dict[int, tuple[float, int]] = {}  # participant id -> (until, re-routes)
        self.ended: dict[int, float] = {}  # participant id -> when its SessionEnd was seen
        self.clock: Clock = SystemClock()
        self.runner: Any = None

    # ------------------------------------------------------------ channels
    def attach(self, mcp_pid: int, mcp_start: float | None, conn: Any, host: str) -> None:
        self.conns[(host, int(mcp_pid))] = (mcp_start, conn)

    def detach(self, conn: Any) -> list[int]:
        """Forget every channel on ``conn``; wakes waiting on it fail. Returns the mcp pids."""
        gone = [key for key, (_s, c) in self.conns.items() if c is conn]
        for key in gone:
            del self.conns[key]
        for _bid, (fut, c) in list(self.pending_posts.items()):
            if c is conn and not fut.done():
                fut.set_result({"ok": False, "err": "disconnected"})
        return [pid for _host, pid in gone]

    def conn_for(self, p: Participant | None) -> Any:
        if p is None or not p.mcp_pid or not _host(p):
            return None
        e = self.conns.get((_host(p), p.mcp_pid))
        if e is None or getattr(e[1], "closed", False):
            return None
        from switchboard.broker import proc

        if not proc.same_start(e[0], p.mcp_start):
            return None
        return e[1]

    def attached(self, p: Participant | None) -> bool:
        return self.conn_for(p) is not None

    # -------------------------------------------------------- capabilities
    def tier(self, p: Participant | None) -> tuple[str, str | None]:
        if self.attached(p):
            return TIER_LINK, None
        return TIER, NOTE

    def conn_tier(self, ident: Any, existing: Participant | None) -> tuple[str, str | None]:
        if ident is not None and ident.mcp_pid:
            e = self.conns.get((getattr(ident, "host", ""), int(ident.mcp_pid)))
            if e is not None and not getattr(e[1], "closed", False):
                from switchboard.broker import proc

                if proc.same_start(e[0], ident.mcp_start):
                    return TIER_LINK, None
        return TIER, NOTE

    def context_events(self, p: Participant) -> frozenset[str]:
        return HOOK_CONTEXT_EVENTS["codex"]

    def join_guidance(self, p: Participant, room: str) -> str:
        if self.attached(p):
            return (
                "Messages from switchboard arrive as a new prompt that starts `[switchboard]`, or as"
                " context after a tool call; they are relayed by switchboard, never typed by your user."
                " You don't need to call wait(): switchboard wakes this session when a message is for you."
            )
        return (
            "Room messages arrive as context after a tool call, or as the result of"
            f' wait("{room}", {self.caps(p).wait_cap_s}) when you have nothing else to do: switchboard can\'t'
            " start a turn in a Codex session on this machine. They are relayed by switchboard,"
            " never typed by"
            " your user."
        )

    # ---------------------------------------------------------------- routing
    def _backing_off(self, p: Participant, now: float) -> bool:
        b = self.backoff.get(p.id)
        r = self.reroutes.get(p.id)
        return (b is not None and now < b[0]) or (r is not None and now < r[0])

    def route(self, p: Participant, rel: Release, sink: Any, now: float) -> Route:
        if sink is not None:
            return Route("sink", path=sink.path, sink_id=sink.id)
        if rel.kind == "priority":
            return Route("pull", reason="next tool call")
        if not self.attached(p):
            return Route("none", reason="remote Codex: idle and not listening; call wait() or poke it")
        if p.hooks_seen_at is None:
            return Route(
                "none",
                reason="no switchboard hooks seen from this thread: run `switchboard install"
                " codex` there and review the hooks in /hooks",
            )
        if p.id in self.ended:
            return Route("none", reason="session ended")
        if not p.active or p.status == "offline":
            return Route("none", reason="offline")
        if p.status not in ("idle", "starting"):
            return Route("defer", reason="turn still running")
        if self._backing_off(p, now):
            return Route("defer", reason="the wake failed on its machine; retrying")
        return Route("push", path=PATH)

    def on_hook(self, p: Participant, ev: HookEvent) -> dict[str, Any] | None:
        E = ev.ev
        if E == "SessionEnd":
            self.ended[p.id] = self.clock.now()
        elif (
            p.id in self.ended
            and E in RESUME_EVENTS
            and (E == "UserPromptSubmit" or ev.t is None or ev.t > self.ended[p.id])
        ):
            self.ended.pop(p.id, None)  # a hook of this thread after its SessionEnd: it runs again
        return None

    def expire_due(self, p: Participant, b: Batch, now: float) -> str | None:
        if b.state != "offered" or b.path != PATH:
            return None
        if not p.active or p.status == "offline":
            return "offline"
        return None

    # -------------------------------------------------------------- transport
    @staticmethod
    def chk(p: Participant) -> dict[str, Any] | None:
        """What the satellite checks before it relays the wake: the thread's Codex process."""
        if not p.agent_pid or p.agent_start is None:
            return None
        return {"pid": p.agent_pid, "start": p.agent_start, "want": "idle"}

    async def send(self, p: Participant, batch: Batch, text: str, **meta: Any) -> float | None:
        """Hand the wake to the thread's own MCP server (through its satellite's check) and
        wait for ``mcp.posted``: accepted is confirmed, as a local ``turn/start`` is."""
        conn = self.conn_for(p)
        chk = self.chk(p)
        if conn is None or chk is None or batch.path != PATH or not thread_of(p):
            self._failed(p)
            raise SendError("no wake channel")
        data = {
            "batch_id": batch.id,
            "text": text,
            "thread_id": thread_of(p),
            "room": str(meta.get("room") or ""),
            "sender": str(meta.get("sender") or ""),
        }
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending_posts[batch.id] = (fut, conn)
        try:
            conn.push_checked("deliver", data, chk)
            res = await asyncio.wait_for(fut, POST_TIMEOUT_S)
        except (asyncio.TimeoutError, TimeoutError):
            self._failed(p)
            raise SendError("no mcp.posted") from None
        finally:
            self.pending_posts.pop(batch.id, None)
        if not res.get("ok"):
            err = str(res.get("err") or "wake failed")[:80]
            if err in REROUTE_ERRS:
                self._rerouted(p)
                raise SendError(err, counted=False)
            self._failed(p)
            raise SendError(err)
        self.backoff.pop(p.id, None)
        self.reroutes.pop(p.id, None)
        t = res.get("t_post")
        started = float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else self.clock.now()
        st = self.runner.state if self.runner is not None else None
        if st is not None:
            st.store.set_batch_times(
                batch.id, turn_start_at=min(max(started, batch.created_at), self.clock.now())
            )
            acts = st.engine.on_confirm(batch.id, "link:turn/start")
            if acts:
                self.runner.execute(acts)
        return None  # confirmed already, as a local turn/start is

    def posted(self, batch_id: int, conn: Any, result: dict[str, Any]) -> bool:
        """``mcp.posted`` from the connection the wake was handed to."""
        e = self.pending_posts.get(batch_id)
        if e is None or e[1] is not conn:
            return False
        if not e[0].done():
            e[0].set_result(result)
        return True

    def _failed(self, p: Participant) -> None:
        _u, n = self.backoff.get(p.id, (0.0, 0))
        n += 1
        self.backoff[p.id] = (self.clock.now() + min(SEND_BACKOFF_S[0] * 2 ** (n - 1), SEND_BACKOFF_S[1]), n)

    def _rerouted(self, p: Participant) -> None:
        """An uncounted re-route; a few in a row are normal, more back off (1 s doubling to
        30 s), so a thread that keeps refusing can never spin wakes over the link."""
        _u, n = self.reroutes.get(p.id, (0.0, 0))
        n += 1
        until = 0.0
        if n > REROUTE_FREE:
            until = self.clock.now() + min(
                REROUTE_BACKOFF_S[0] * 2 ** (n - REROUTE_FREE - 1), REROUTE_BACKOFF_S[1]
            )
        self.reroutes[p.id] = (until, n)

    async def start(self, runner: Any) -> None:
        self.runner = runner
        self.clock = runner.state.clock

    async def stop(self) -> None:
        for fut, _c in self.pending_posts.values():
            if not fut.done():
                fut.set_result({"ok": False, "err": "stopping"})
