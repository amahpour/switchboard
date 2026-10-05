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
every 250 ms for joined Claude sessions on this machine (through the broker's
local host view, DESIGN.md §27.5.6). Channels and registry views are keyed
``(host, pid)``. It sets and clears ``waiting-approval`` (``status == "waiting"``),
and it ends a turn that fired no Stop hook (Esc) once the registry has said
``idle`` for a second after the last hook. The ``status`` field is undocumented
(FINDINGS §2 1.5).

**A Claude session on a remote host** (DESIGN.md §27.5.6, §27.7) works the same
way through its host's link: its registry is read on that host by the satellite
and relayed (``reg`` frames, every 250 ms; ``relay``), and both kinds of read go
through one ``apply_view``. A relayed view is fresh for 1.5 s (a push) and lost
after 5 s (parked), against 0.5 s and 3 s locally, since LAN jitter would
otherwise defer wakes. A push to a remote session carries ``chk = {pid, start,
want}`` beside the frame (``want`` is ``busy`` for a mid-task priority batch,
``idle`` for a wake): the satellite relays it only to the connection whose own
attested Claude ``chk`` names, re-reads the registry a moment before, and if the
status is no longer ``want`` drops it and reports ``stale_status`` itself (marked
``facts.lastmile``), an uncounted re-route that waits for a newer view.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from dataclasses import dataclass
from typing import Any

from switchboard.adapters.base import HOOK_CONTEXT_EVENTS, Adapter
from switchboard.adapters.base import SendError as _BaseSendError
from switchboard.broker import proc
from switchboard.claude_registry import REGISTRY_IDLE, registry_status
from switchboard.clock import Clock, SystemClock
from switchboard.config import Config
from switchboard.models import LOCAL_HOST, Action, Batch, Participant, Release, Route
from switchboard.remote.proto import STALE_STATUS

log = logging.getLogger("switchboard.claude")

REGISTRY_POLL_S = 0.25
REGISTRY_FRESH_S = 0.5  # an idle wake needs a registry read at most this old (§9.2)
REGISTRY_LOST_S = 3.0  # older than this: the registry is unreadable, the member is parked
# the same for a session on a remote host, whose registry its link relays (§27.5.6)
REMOTE_FRESH_S = 1.5
REMOTE_LOST_S = 5.0
# stale_status re-routes (the satellite's last-mile check refused a frame) in a row
# before they back off too, as a Codex re-route does
REROUTE_FREE = 2
REROUTE_BACKOFF_S = (1.0, 30.0)
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


def _host(p: Any) -> str:
    """A participant's host ('' for this machine; stand-ins without the field are local)."""
    return getattr(p, "host", LOCAL_HOST)


@dataclass(frozen=True)
class RegView:
    """One read of a session's registry file (kept in memory only)."""

    status: str | None
    read_at: float  # broker clock time of the read
    since: float  # when the registry says this status began (statusUpdatedAt, else first seen)
    # the adapter's running count of views when this one arrived: "a view newer than a
    # stale_status refusal" is decided by arrival order, never by a (steppable) clock
    seq: int = dataclasses.field(default=0, compare=False)


def registry_view(data: dict[str, Any], prev: RegView | None, now: float) -> RegView:
    status, since = registry_status(data)
    if since is None:
        since = prev.since if prev is not None and prev.status == status else now
    return RegView(status=status, read_at=now, since=since)


def registry_transition(
    status: str, hooks_seen_at: float | None, reg: RegView, now: float
) -> tuple[str, bool] | None:
    """The status change a registry read implies: ``(new_status, bump_boundary)`` or None.

    - ``waiting`` (an approval prompt is open) holds every delivery (§7.4:
      only the registry and CodexLink touch waiting-approval).
    - When the prompt goes away the registry says what the session does now:
      ``idle`` or ``shell`` (the prompt was declined, which ends the turn with no Stop
      hook) or anything else (approved: the tool runs).
    - A busy member whose registry has said ``idle`` or ``shell`` for ``REGISTRY_IDLE_GRACE_S``
      since after its last hook ended a turn that fired no Stop (Esc).
    """
    if reg.status == "waiting":
        return None if status == "waiting-approval" else ("waiting-approval", False)
    if status == "waiting-approval":
        return ("idle", True) if reg.status in REGISTRY_IDLE else ("busy", False)
    if (
        status == "busy"
        and reg.status in REGISTRY_IDLE
        and hooks_seen_at is not None
        and reg.since > hooks_seen_at
        and now - reg.since >= REGISTRY_IDLE_GRACE_S
    ):
        return ("idle", True)
    return None


class ClaudeAdapter(Adapter):
    harness = "claude"
    serial_push = True  # one inbox frame in flight per session (§9.2)

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        # (host, mcp_pid) -> (mcp_start, rpc Conn): the attached push channels
        self.conns: dict[tuple[str, int], tuple[float | None, Any]] = {}
        self.registry: dict[tuple[str, int], RegView] = {}  # (host, agent pid) -> latest registry read
        self.pending_posts: dict[int, tuple[asyncio.Future[dict[str, Any]], Any]] = {}
        self.backoff: dict[int, tuple[float, int]] = {}  # participant id -> (until, failures)
        # participant id -> (until, stale_status re-routes in a row): a remote session whose
        # satellite keeps refusing frames backs off too (REROUTE_FREE, REROUTE_BACKOFF_S)
        self.reroutes: dict[int, tuple[float, int]] = {}
        # participant id -> the view count (``view_seq``) when its last frame was refused as
        # stale: no new frame until a view that arrived after the refusal (the satellite sent
        # one just before it, which doesn't count). Arrival order, not time: a wall-clock
        # step can't hold a member in re-check.
        self.recheck: dict[int, int] = {}
        self.view_seq = 0  # registry views stored so far (local reads and relayed ones)
        self.unconfirmed_at: dict[int, float] = {}  # participant id -> last idle_no_token expiry
        self.clock: Clock = SystemClock()
        self.runner: Any = None
        self._task: asyncio.Task[None] | None = None

    # -------------------------------------------------------- attach state
    def attach(self, mcp_pid: int, mcp_start: float | None, conn: Any, host: str = LOCAL_HOST) -> None:
        self.conns[(host, int(mcp_pid))] = (mcp_start, conn)

    def detach(self, conn: Any) -> list[int]:
        """Forget every channel on ``conn``; pending posts on it fail. Returns the mcp pids."""
        gone = [key for key, (_s, c) in self.conns.items() if c is conn]
        for key in gone:
            del self.conns[key]
        for _bid, (fut, c) in list(self.pending_posts.items()):
            if c is conn and not fut.done():
                fut.set_result({"ok": False, "err": "disconnected"})
        return [pid for _host, pid in gone]

    def reg_view(self, p: Participant) -> RegView | None:
        """The latest registry view of ``p``'s agent (on its own host)."""
        return self.registry.get((_host(p), p.agent_pid or -1))

    @staticmethod
    def fresh_s(p: Participant) -> float:
        """How old a registry view may be for a push: 0.5 s on this machine, 1.5 s for a
        view relayed from a remote host (LAN jitter would otherwise defer wakes, §27.5.6)."""
        return REGISTRY_FRESH_S if _host(p) == LOCAL_HOST else REMOTE_FRESH_S

    @staticmethod
    def lost_s(p: Participant) -> float:
        """How old a registry view may be before the member is parked as unreadable."""
        return REGISTRY_LOST_S if _host(p) == LOCAL_HOST else REMOTE_LOST_S

    def conn_for(self, p: Participant | None) -> Any:
        if p is None or not p.mcp_pid or not p.claude_socket:
            return None
        e = self.conns.get((_host(p), p.mcp_pid))
        if e is None or not proc.same_start(e[0], p.mcp_start) or getattr(e[1], "closed", False):
            return None
        return e[1]

    def attached(self, p: Participant | None) -> bool:
        return self.conn_for(p) is not None

    # --------------------------------------------------------- capabilities
    def tier(self, p: Participant | None) -> tuple[str, str | None]:
        return (TIER_INBOX, None) if self.attached(p) else (TIER_HOOK, None)

    def conn_tier(self, ident: Any, existing: Participant | None) -> tuple[str, str | None]:
        e = (
            self.conns.get((getattr(ident, "host", LOCAL_HOST), int(ident.mcp_pid)))
            if ident is not None and ident.mcp_pid
            else None
        )
        if (
            e is not None
            and ident.claude_socket
            and proc.same_start(e[0], ident.mcp_start)
            and not getattr(e[1], "closed", False)
        ):
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

    def _rerouting(self, p: Participant, now: float) -> bool:
        r = self.reroutes.get(p.id)
        return r is not None and now < r[0]

    def _rechecking(self, p: Participant, reg: RegView) -> bool:
        """A frame to ``p`` was refused as stale: wait for a view that arrived after that."""
        t = self.recheck.get(p.id)
        return t is not None and reg.seq <= t

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
        reg = self.reg_view(p)
        return (
            reg is not None
            and now - reg.read_at <= self.fresh_s(p)
            and reg.status == "busy"
            and not self._rechecking(p, reg)
        )

    def route(self, p: Participant, rel: Release, sink: Any, now: float) -> Route:
        if sink is not None:
            return Route("sink", path=sink.path, sink_id=sink.id)
        inbox = self.attached(p)
        if rel.kind == "priority":
            # Mid-task: the inbox only for bypass members (no approval prompt to
            # straddle), and only while a fresh registry read says the turn is
            # running (not waiting on a prompt: e.g. the human switched modes and
            # no hook has said so yet); everyone else pulls it at the next tool boundary.
            if (
                inbox
                and p.approval_mode == "bypass"
                and not self._backing_off(p, now)
                and not self._rerouting(p, now)
                and self._unconfirmed_wait(p, now) <= 0
                and self._registry_busy(p, now)
            ):
                return Route("push", path="inbox")
            return Route("pull", reason="next tool call")
        if not inbox:
            return Route("none", reason="idle and not listening: call wait() or poke it")
        if p.hooks_seen_at is None:
            return Route(
                "none", reason="no switchboard hooks seen from this session: run `switchboard install claude`"
            )
        if p.status not in ("idle", "starting"):
            return Route("defer", reason="turn still running")
        if self._backing_off(p, now):
            return Route("defer", reason="inbox post failed; retrying")
        if self._rerouting(p, now):
            return Route("defer", reason="the session's status keeps changing under its frames; retrying")
        if self._unconfirmed_wait(p, now) > 0:
            if p.push_expiries >= EXPIRY_PARK_AT:
                return Route("none", reason="inbox deliveries not confirmed; retrying later")
            return Route("defer", reason="inbox frame not confirmed; retrying")
        reg = self.reg_view(p)
        if reg is None or now - reg.read_at > self.lost_s(p):
            return Route("none", reason="can't read the Claude session registry")
        if self._rechecking(p, reg):
            return Route("defer", reason="registry changed; re-checking")
        if now - reg.read_at > self.fresh_s(p) or reg.status not in REGISTRY_IDLE:
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
        reg = self.reg_view(p)
        if reg is not None and reg.status is not None and reg.status not in REGISTRY_IDLE:
            return None  # the session is running again: the frame may still land
        idle_since = max(b.posted_at, p.status_at or 0.0)
        if reg is not None and reg.status in REGISTRY_IDLE:
            idle_since = max(idle_since, reg.since)
        if now - idle_since >= self.cfg.claude.inbox_idle_expire_s:
            return "idle_no_token"
        return None

    def push_expired(self, p: Participant, b: Batch, reason: str, now: float) -> None:
        if reason == "idle_no_token":
            self.unconfirmed_at[p.id] = now

    # ------------------------------------------------------------ transport
    @staticmethod
    def chk(p: Participant, batch: Batch) -> dict[str, Any] | None:
        """What a remote host's satellite checks just before it relays this frame
        (§27.5.6): the session's agent process, and the registry status the route
        assumed: ``busy`` for a mid-task priority batch, ``idle`` for a wake."""
        if not p.agent_pid or p.agent_start is None:
            return None
        return {
            "pid": p.agent_pid,
            "start": p.agent_start,
            "want": "busy" if batch.kind == "priority" else "idle",
        }

    async def send(self, p: Participant, batch: Batch, text: str, **meta: Any) -> float | None:
        """Hand one frame to the session's MCP server and wait for ``mcp.posted``. On a
        remote host the frame goes through its satellite's last-mile check (``chk``)."""
        conn = self.conn_for(p)
        remote = getattr(conn, "remote", False) is True
        chk = self.chk(p, batch) if remote else None
        if conn is None or (remote and chk is None):
            self._failed(p)
            raise SendError("no inbox channel")
        data = {
            "batch_id": batch.id,
            "text": text,
            "room": str(meta.get("room") or ""),
            "sender": str(meta.get("sender") or ""),
        }
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending_posts[batch.id] = (fut, conn)
        try:
            if remote:
                conn.push_checked("deliver", data, chk)
            else:
                conn.push("deliver", data)
            res = await asyncio.wait_for(fut, POST_TIMEOUT_S)
        except (asyncio.TimeoutError, TimeoutError):
            self._failed(p)
            raise SendError("no mcp.posted") from None
        finally:
            self.pending_posts.pop(batch.id, None)
        if not res.get("ok"):
            err = str(res.get("err") or "post failed")[:80]
            if remote and err == STALE_STATUS and res.get("lastmile") is True:
                # the satellite's own report (``facts.lastmile``): the session's status changed
                # between the relayed view and the post, and nothing was posted; back to
                # pending, uncounted, until a newer view says otherwise. The same code from
                # the MCP connection itself is an ordinary counted failure, as locally.
                self._rerouted(p)
                raise SendError(STALE_STATUS, counted=False)
            self._failed(p)
            raise SendError(err)
        self.backoff.pop(p.id, None)
        self.reroutes.pop(p.id, None)
        self.recheck.pop(p.id, None)
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

    def _rerouted(self, p: Participant) -> None:
        """A ``stale_status`` re-route: wait for a registry view newer than this refusal;
        a few in a row are normal (the session changed under a frame), more back off
        too (1 s doubling to 30 s), so a status that keeps changing, or a remote that
        keeps refusing, can never spin frames over the link."""
        now = self.clock.now()
        self.recheck[p.id] = self.view_seq
        _u, n = self.reroutes.get(p.id, (0.0, 0))
        n += 1
        until = 0.0
        if n > REROUTE_FREE:
            until = now + min(REROUTE_BACKOFF_S[0] * 2 ** (n - REROUTE_FREE - 1), REROUTE_BACKOFF_S[1])
        self.reroutes[p.id] = (until, n)

    # ------------------------------------------------------------- registry
    def observe(
        self, agent_pid: int, data: dict[str, Any], now: float, host: str = LOCAL_HOST
    ) -> tuple[RegView, bool]:
        """Record one registry read; returns (view, status changed)."""
        key = (host, int(agent_pid))
        prev = self.registry.get(key)
        view = dataclasses.replace(registry_view(data, prev, now), seq=self._next_seq())
        self.registry[key] = view
        return view, prev is None or prev.status != view.status

    def _next_seq(self) -> int:
        self.view_seq += 1
        return self.view_seq

    def _local_view(self) -> Any:
        """This machine's host view: the broker's (``state.hosts``), else one made
        from this adapter's config (unit tests that drive ``poll_once`` directly)."""
        views = getattr(getattr(self.runner, "state", None), "hosts", None)
        if views is not None:
            return views.local
        from switchboard.broker.hosts import LocalView

        return LocalView(self.cfg.claude.sessions_dir)

    def apply_view(self, p: Participant, view: RegView, now: float, changed: bool) -> list[Action]:
        """What one registry view of ``p`` implies (§9.2): the approval hold, a declined or
        approved prompt, an Esc-ended turn, or (``changed``) a fresh routing decision.
        The same for a view read here (``poll_once``) and one relayed by a remote host's
        link (``relay``)."""
        engine = self.runner.state.engine
        tr = registry_transition(p.status, p.hooks_seen_at, view, now)
        if tr is not None:
            return engine.set_status(p, tr[0], "claude:registry", bump=tr[1])
        if changed:
            return engine.evaluate_participant(p.id)
        return []

    def poll_once(self) -> None:
        """Read the registry of every joined Claude session on this machine. Remote
        rows are never read here: their pids are pids on another host (§27.5.6)."""
        st = self.runner.state
        now = self.clock.now()
        view_of = self._local_view()
        seen: set[tuple[str, int]] = set()
        for p in st.store.joined_participants():
            if p.harness != "claude" or not p.agent_pid or p.host != LOCAL_HOST:
                continue
            seen.add((LOCAL_HOST, p.agent_pid))
            data = view_of.read_registry(p.agent_pid)
            if data is None:
                continue  # stale view: no idle wake; the liveness check ends a dead session
            if data.get("pid") not in (None, p.agent_pid):
                continue
            sock = data.get("messagingSocketPath")
            if p.claude_socket and sock is not None and sock != p.claude_socket:
                continue
            view, changed = self.observe(p.agent_pid, data, now)
            rechecked = self._recheck_done(p, view)
            acts = self.apply_view(p, view, now, changed or rechecked)
            if acts:
                self.runner.execute(acts)
        for key in [x for x in self.registry if x[0] == LOCAL_HOST and x not in seen]:
            del self.registry[key]

    def _recheck_done(self, p: Participant, view: RegView) -> bool:
        """The first view after a ``stale_status`` refusal: route again (its status may
        be the same as the one the refusal's own view reported)."""
        t = self.recheck.get(p.id)
        if t is None or view.seq <= t:
            return False
        del self.recheck[p.id]
        return True

    def relay(self, host: str, got: dict[tuple[int, float], Any], now: float) -> list[Action]:
        """A ``reg`` frame of ``host``'s link (§27.5.6): per watched Claude agent ``(pid,
        start)``, the status its satellite read on that host (``None``: unreadable, or not
        this session's file), when that status began (``since``, or ``None`` if the file
        doesn't say) and when it was read (``read_at``), both on this broker's clock.
        Checked and applied as ``poll_once`` does with a local read: an unreadable one
        leaves the last view to age (no push after 1.5 s, parked after 5 s)."""
        acts: list[Action] = []
        seen: set[tuple[str, int]] = set()
        for p in self.runner.state.store.joined_participants():
            if p.harness != "claude" or p.host != host or not p.agent_pid:
                continue
            e = next(
                (
                    v
                    for (pid, start), v in got.items()
                    if pid == p.agent_pid and proc.same_start(start, p.agent_start)
                ),
                None,
            )
            if e is None:
                continue
            key = (host, p.agent_pid)
            seen.add(key)
            if e.status is None:
                continue
            prev = self.registry.get(key)
            if e.since is not None:
                since = e.since
            else:
                since = prev.since if prev is not None and prev.status == e.status else e.read_at
            view = RegView(status=e.status, read_at=e.read_at, since=since, seq=self._next_seq())
            self.registry[key] = view
            # a view that makes the member routable again (a new status, the first fresh
            # one after a stale stretch, the first after a refusal) routes it at once
            changed = prev is None or prev.status != view.status or now - prev.read_at > self.fresh_s(p)
            rechecked = self._recheck_done(p, view)
            acts += self.apply_view(p, view, now, changed or rechecked)
        for key in [x for x in self.registry if x[0] == host and x not in seen]:
            del self.registry[key]
        return acts

    def forget_host(self, host: str) -> None:
        """``host``'s link went down: its relayed views are void (a new link relays anew)."""
        for key in [x for x in self.registry if x[0] == host]:
            del self.registry[key]

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
