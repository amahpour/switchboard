"""Claude Code (DESIGN.md §9.2): tiers ``claude:inbox`` and ``claude:hook``.

- **claude:inbox.** The session's own ``switchboard mcp`` server (a live child of
  ``claude``, verified against the sessions registry) attached its broker
  connection as the push channel (``mcp.attach``). A batch is pushed to it
  (``push: deliver``) and the MCP server posts it into its parent session's
  inbox socket (``mcp/claude_inbox.py``); ``mcp.posted`` reports back.
  - Idle wake: only when the hook status is idle **and** the last registry
    read (at most 0.5 s old) says ``idle``. A new turn starts in ~40 ms.
  - Mid-task priority: pushed only to ``bypassPermissions`` members; everyone
    else gets it as PostToolUse / PostToolUseFailure / UserPromptSubmit
    context (pull), so a queued inbox frame never straddles an approval
    prompt (a rejected prompt would start a turn from it, FINDINGS §2 1.5).
  - Confirmed by the UserPromptSubmit whose prompt carries the batch token;
    expired when the member stays idle ``inbox_idle_expire_s`` after the post
    with no token, goes offline, or the post fails.
- **claude:hook.** No inbox (no token, no attach, or the MCP server's guard
  refused): mid-task works the same; an idle member is served by an open
  ``wait()`` or shown as parked.

``ClaudeRegistryPoller`` (in this adapter) reads ``<sessions_dir>/<pid>.json``
every 250 ms for joined Claude sessions. It sets and clears
``waiting-approval`` (``status == "waiting"``), and it ends a turn that
fired no Stop hook (Esc) once the registry has said ``idle`` for a second
after the last hook. The ``status`` field is undocumented (FINDINGS §2 1.5).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import stat
from dataclasses import dataclass
from typing import Any

from switchboard.adapters.base import HOOK_CONTEXT_EVENTS, Adapter
from switchboard.adapters.base import SendError as _BaseSendError
from switchboard.broker import proc
from switchboard.clock import Clock, SystemClock
from switchboard.config import Config
from switchboard.models import Batch, Participant, Release, Route

log = logging.getLogger("switchboard.claude")

REGISTRY_POLL_S = 0.25
REGISTRY_FRESH_S = 0.5  # an idle wake needs a registry read at most this old (§9.2)
REGISTRY_LOST_S = 3.0  # older than this: the registry is unreadable, the member is parked
REGISTRY_IDLE_GRACE_S = 1.0  # registry idle this long after the last hook ends a Stop-less turn
POST_TIMEOUT_S = 10.0
SEND_BACKOFF_S = (1.0, 30.0)
# After n consecutive unconfirmed frames (idle_no_token), the next idle wake
# waits inbox_idle_expire_s * 2**(n-1), at most this long; any confirmation
# resets it. From the warn count on, the member shows parked meanwhile.
EXPIRY_BACKOFF_MAX_S = 300.0
EXPIRY_PARK_AT = 3

TIER_INBOX = "claude:inbox"
TIER_HOOK = "claude:hook"


class SendError(_BaseSendError):
    pass


@dataclass(frozen=True)
class RegView:
    """One read of a session's registry file (kept in memory only)."""

    status: str | None
    read_at: float  # broker clock time of the read
    since: float  # when the registry says this status began (statusUpdatedAt, else first seen)


def read_registry(sessions_dir: str, pid: int) -> dict[str, Any] | None:
    """``<sessions_dir>/<pid>.json`` if it is a regular file owned by us and parses."""
    path = os.path.join(os.path.expanduser(str(sessions_dir)), f"{int(pid)}.json")
    try:
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > 1 << 20:
            return None
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def registry_view(data: dict[str, Any], prev: RegView | None, now: float) -> RegView:
    status = data.get("status")
    status = status if isinstance(status, str) and len(status) <= 32 else None
    since: float | None = None
    upd = data.get("statusUpdatedAt")
    if isinstance(upd, (int, float)) and not isinstance(upd, bool) and upd > 0:
        since = float(upd) / 1000.0 if upd > 1e11 else float(upd)  # epoch ms (seconds tolerated)
    if since is None:
        since = prev.since if prev is not None and prev.status == status else now
    return RegView(status=status, read_at=now, since=since)


def registry_transition(status: str, hooks_seen_at: float | None, reg: RegView,
                        now: float) -> tuple[str, bool] | None:
    """The status change a registry read implies: ``(new_status, bump_boundary)`` or None.

    - ``waiting`` (an approval prompt is open) holds every delivery (§7.4:
      only the registry and CodexLink touch waiting-approval).
    - When the prompt goes away the registry says what the session does now:
      ``idle`` (the prompt was declined, which ends the turn with no Stop
      hook) or anything else (approved: the tool runs).
    - A busy member whose registry has said ``idle`` for ``REGISTRY_IDLE_GRACE_S``
      since after its last hook ended a turn that fired no Stop (Esc).
    """
    if reg.status == "waiting":
        return None if status == "waiting-approval" else ("waiting-approval", False)
    if status == "waiting-approval":
        return ("idle", True) if reg.status == "idle" else ("busy", False)
    if (status == "busy" and reg.status == "idle" and hooks_seen_at is not None
            and reg.since > hooks_seen_at and now - reg.since >= REGISTRY_IDLE_GRACE_S):
        return ("idle", True)
    return None


class ClaudeAdapter(Adapter):
    harness = "claude"
    serial_push = True  # one inbox frame in flight per session (§9.2)

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        # mcp_pid -> (mcp_start, rpc Conn): the attached push channels
        self.conns: dict[int, tuple[float | None, Any]] = {}
        self.registry: dict[int, RegView] = {}  # agent pid -> latest registry read
        self.pending_posts: dict[int, tuple[asyncio.Future[dict[str, Any]], Any]] = {}
        self.backoff: dict[int, tuple[float, int]] = {}  # participant id -> (until, failures)
        self.unconfirmed_at: dict[int, float] = {}  # participant id -> last idle_no_token expiry
        self.clock: Clock = SystemClock()
        self.runner: Any = None
        self._task: asyncio.Task[None] | None = None

    # -------------------------------------------------------- attach state
    def attach(self, mcp_pid: int, mcp_start: float | None, conn: Any) -> None:
        self.conns[int(mcp_pid)] = (mcp_start, conn)

    def detach(self, conn: Any) -> list[int]:
        """Forget every channel on ``conn``; pending posts on it fail. Returns the mcp pids."""
        gone = [pid for pid, (_s, c) in self.conns.items() if c is conn]
        for pid in gone:
            del self.conns[pid]
        for _bid, (fut, c) in list(self.pending_posts.items()):
            if c is conn and not fut.done():
                fut.set_result({"ok": False, "err": "disconnected"})
        return gone

    def conn_for(self, p: Participant | None) -> Any:
        if p is None or not p.mcp_pid or not p.claude_socket:
            return None
        e = self.conns.get(p.mcp_pid)
        if e is None or not proc.same_start(e[0], p.mcp_start) or getattr(e[1], "closed", False):
            return None
        return e[1]

    def attached(self, p: Participant | None) -> bool:
        return self.conn_for(p) is not None

    # --------------------------------------------------------- capabilities
    def tier(self, p: Participant | None) -> tuple[str, str | None]:
        return (TIER_INBOX, None) if self.attached(p) else (TIER_HOOK, None)

    def conn_tier(self, ident: Any, existing: Participant | None) -> tuple[str, str | None]:
        e = self.conns.get(int(ident.mcp_pid)) if ident is not None and ident.mcp_pid else None
        if (e is not None and ident.claude_socket and proc.same_start(e[0], ident.mcp_start)
                and not getattr(e[1], "closed", False)):
            return TIER_INBOX, None
        return TIER_HOOK, None

    def context_events(self, p: Participant) -> frozenset[str]:
        return HOOK_CONTEXT_EVENTS["claude"]

    def join_guidance(self, p: Participant, room: str) -> str:
        if self.attached(p):
            return (
                "Room messages arrive as a message from switchboard, or as context after a tool call."
                " They are never typed by your user. You don't need to call wait(): switchboard wakes"
                " this session when a message is for you."
            )
        return (
            "Room messages arrive as context after your tool calls, or as the result of"
            f' wait("{room}", {self.caps(p).wait_cap_s}) when you have nothing else to do.'
            " They are never typed by your user."
        )

    # -------------------------------------------------------------- routing
    def _backing_off(self, p: Participant, now: float) -> bool:
        b = self.backoff.get(p.id)
        return b is not None and now < b[0]

    def _unconfirmed_wait(self, p: Participant, now: float) -> float:
        """Seconds before the next frame after unconfirmed ones (0: go now).

        Frames that land but are never confirmed (a failing UserPromptSubmit
        hook, a changed prompt format) would otherwise be re-pushed every
        ``inbox_idle_expire_s``: each a counted wake and possibly a model turn."""
        t = self.unconfirmed_at.get(p.id)
        n = min(p.push_expiries, 20)
        if n <= 0 or t is None:
            return 0.0
        delay = min(self.cfg.claude.inbox_idle_expire_s * 2 ** (n - 1), EXPIRY_BACKOFF_MAX_S)
        return max(0.0, t + delay - now)

    def _registry_busy(self, p: Participant, now: float) -> bool:
        reg = self.registry.get(p.agent_pid or -1)
        return reg is not None and now - reg.read_at <= REGISTRY_FRESH_S and reg.status == "busy"

    def route(self, p: Participant, rel: Release, sink: Any, now: float) -> Route:
        if sink is not None:
            return Route("sink", path=sink.path, sink_id=sink.id)
        inbox = self.attached(p)
        if rel.kind == "priority":
            # Mid-task: the inbox only for bypass members (no approval prompt to
            # straddle), and only while a fresh registry read says the turn is
            # running (not waiting on a prompt: e.g. the human switched modes and
            # no hook has said so yet); everyone else pulls it at the next tool boundary.
            if (inbox and p.approval_mode == "bypass" and not self._backing_off(p, now)
                    and self._unconfirmed_wait(p, now) <= 0 and self._registry_busy(p, now)):
                return Route("push", path="inbox")
            return Route("pull", reason="next tool call")
        if not inbox:
            return Route("none", reason="idle and not listening: call wait() or poke it")
        if p.hooks_seen_at is None:
            return Route("none", reason="no switchboard hooks seen from this session: run `switchboard install claude`")
        if p.status not in ("idle", "starting"):
            return Route("defer", reason="turn still running")
        if self._backing_off(p, now):
            return Route("defer", reason="inbox post failed; retrying")
        if self._unconfirmed_wait(p, now) > 0:
            if p.push_expiries >= EXPIRY_PARK_AT:
                return Route("none", reason="inbox deliveries not confirmed; retrying later")
            return Route("defer", reason="inbox frame not confirmed; retrying")
        reg = self.registry.get(p.agent_pid or -1)
        if reg is None or now - reg.read_at > REGISTRY_LOST_S:
            return Route("none", reason="can't read the Claude session registry")
        if now - reg.read_at > REGISTRY_FRESH_S or reg.status != "idle":
            return Route("defer", reason="registry not idle yet")
        return Route("push", path="inbox")

    def expire_due(self, p: Participant, b: Batch, now: float) -> str | None:
        """Event-based expiry of an unconfirmed inbox frame (§8.7)."""
        if b.path != "inbox" or b.state != "offered":
            return None
        if p.status == "offline" or not p.active:
            return "offline"
        # 'starting' (after a broker restart or MCP reconnect) is idle for routing,
        # so it is idle here too: an idle session fires no hook to change it
        if b.posted_at is None or p.status not in ("idle", "starting"):
            return None
        reg = self.registry.get(p.agent_pid or -1)
        if reg is not None and reg.status not in (None, "idle"):
            return None  # the session is running again: the frame may still land
        idle_since = max(b.posted_at, p.status_at or 0.0)
        if reg is not None and reg.status == "idle":
            idle_since = max(idle_since, reg.since)
        if now - idle_since >= self.cfg.claude.inbox_idle_expire_s:
            return "idle_no_token"
        return None

    def push_expired(self, p: Participant, b: Batch, reason: str, now: float) -> None:
        if reason == "idle_no_token":
            self.unconfirmed_at[p.id] = now

    # ------------------------------------------------------------ transport
    async def send(self, p: Participant, batch: Batch, text: str, **meta: Any) -> float | None:
        """Hand one frame to the session's MCP server and wait for ``mcp.posted``."""
        conn = self.conn_for(p)
        if conn is None:
            self._failed(p)
            raise SendError("no inbox channel")
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending_posts[batch.id] = (fut, conn)
        try:
            conn.push("deliver", {"batch_id": batch.id, "text": text,
                                  "room": str(meta.get("room") or ""), "sender": str(meta.get("sender") or "")})
            res = await asyncio.wait_for(fut, POST_TIMEOUT_S)
        except (asyncio.TimeoutError, TimeoutError):
            self._failed(p)
            raise SendError("no mcp.posted") from None
        finally:
            self.pending_posts.pop(batch.id, None)
        if not res.get("ok"):
            self._failed(p)
            raise SendError(str(res.get("err") or "post failed")[:80])
        self.backoff.pop(p.id, None)
        t = res.get("t_post")
        return float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else None

    def posted(self, batch_id: int, conn: Any, result: dict[str, Any]) -> bool:
        """``mcp.posted`` from the connection the frame was handed to."""
        e = self.pending_posts.get(batch_id)
        if e is None or e[1] is not conn:
            return False
        if not e[0].done():
            e[0].set_result(result)
        return True

    def _failed(self, p: Participant) -> None:
        _until, n = self.backoff.get(p.id, (0.0, 0))
        n += 1
        delay = min(SEND_BACKOFF_S[0] * (2 ** (n - 1)), SEND_BACKOFF_S[1])
        self.backoff[p.id] = (self.clock.now() + delay, n)

    # ------------------------------------------------------------- registry
    def observe(self, agent_pid: int, data: dict[str, Any], now: float) -> tuple[RegView, bool]:
        """Record one registry read; returns (view, status changed)."""
        prev = self.registry.get(agent_pid)
        view = registry_view(data, prev, now)
        self.registry[agent_pid] = view
        return view, prev is None or prev.status != view.status

    def poll_once(self) -> None:
        st = self.runner.state
        engine = st.engine
        now = self.clock.now()
        seen: set[int] = set()
        for p in st.store.joined_participants():
            if p.harness != "claude" or not p.agent_pid:
                continue
            seen.add(p.agent_pid)
            data = read_registry(self.cfg.claude.sessions_dir, p.agent_pid)
            if data is None:
                continue  # stale view: no idle wake; the liveness check ends a dead session
            if data.get("pid") not in (None, p.agent_pid):
                continue
            sock = data.get("messagingSocketPath")
            if p.claude_socket and sock is not None and sock != p.claude_socket:
                continue
            view, changed = self.observe(p.agent_pid, data, now)
            tr = registry_transition(p.status, p.hooks_seen_at, view, now)
            acts: list[Any] = []
            if tr is not None:
                acts += engine.set_status(p, tr[0], "claude:registry", bump=tr[1])
            elif changed:
                acts += engine.evaluate_participant(p.id)
            if acts:
                self.runner.execute(acts)
        for pid in [x for x in self.registry if x not in seen]:
            del self.registry[pid]

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(REGISTRY_POLL_S)
            try:
                self.poll_once()
            except Exception:
                log.exception("claude registry poll failed")

    async def start(self, runner: Any) -> None:
        self.runner = runner
        self.clock = runner.state.clock
        self._task = asyncio.get_running_loop().create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        for fut, _c in self.pending_posts.values():
            if not fut.done():
                fut.set_result({"ok": False, "err": "stopping"})
