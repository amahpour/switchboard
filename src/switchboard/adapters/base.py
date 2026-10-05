"""The common adapter interface (DESIGN.md §9.1) and the M2 pull adapter.

``route`` is pure: given a participant, what the rules released and the
member's open sink (if any), it says how the batch goes out:
``push(path)`` (the adapter's ``send`` transports it), ``sink(id)`` (an open
wait() call returns it), ``pull`` (the next hook or tool call claims it) or
``none(reason)`` (parked: nothing can reach the agent right now).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from switchboard.config import Config
from switchboard.hook.switchboard_hook import CONTEXT_MAX as HOOK_CONTEXT_MAX
from switchboard.models import Batch, HookEvent, Participant, Release, Route

INLINE_PATHS = frozenset({"inbox", "wait", "read", "say"})

# Hook events whose reply may carry context, per harness (DESIGN.md §7.3,
# broker side; the hook script has its own copy of the output table).
HOOK_CONTEXT_EVENTS: dict[str, frozenset[str]] = {
    "claude": frozenset({"SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure"}),
    "codex": frozenset({"PostToolUse"}),
    "devin": frozenset({"PostToolUse"}),
    "cursor": frozenset({"PostToolUse", "PostToolUseFailure"}),
    # synthetic hook events from scripted test agents (test mode only)
    "test": frozenset({"UserPromptSubmit", "PostToolUse", "PostToolUseFailure"}),
}

# Events each harness registers (DESIGN.md §7.4), in canonical (Claude) spelling.
HOOK_EVENTS: dict[str, frozenset[str]] = {
    "claude": frozenset(
        {"SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop", "SessionEnd"}
    ),
    "codex": frozenset({"UserPromptSubmit", "PostToolUse", "Stop", "Interrupt", "SessionEnd"}),
    "cursor": frozenset(
        {"SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop", "SessionEnd"}
    ),
    "devin": frozenset(
        {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"}
    ),
    "test": frozenset(
        {"SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop", "SessionEnd"}
    ),
}


class SendError(Exception):
    """A push that didn't go out. The message is a fixed string (no text, no paths).

    ``counted=False`` marks a re-route (the batch goes back to pending without
    counting as a push failure): e.g. a Codex steer when the turn already ended."""

    def __init__(self, reason: str, *, counted: bool = True):
        super().__init__(reason)
        self.reason = reason[:80]
        self.counted = counted


@dataclass(frozen=True)
class Caps:
    ctx_max_chars: int
    wait_cap_s: int
    inline_paths: frozenset[str] = INLINE_PATHS


class Adapter:
    """Base class. Subclasses override what their harness supports."""

    harness = "unknown"
    # At most one unconfirmed push frame per session, across its rooms (Claude:
    # a second frame would queue behind the turn the first one starts).
    serial_push = False

    def __init__(self, cfg: Config):
        self.cfg = cfg

    # -- capabilities -------------------------------------------------------
    def caps(self, p: Participant | None = None) -> Caps:
        c = self.cfg
        table = {
            "claude": (c.delivery.batch_max_chars, c.claude.wait_cap_s),
            "codex": (c.codex.ctx_max_chars, c.codex.wait_cap_s),
            "cursor": (c.cursor.ctx_max_chars, c.cursor.wait_cap_s),
            "devin": (c.devin.ctx_max_chars, c.devin.wait_cap_s),
        }
        ctx, wait = table.get(self.harness, (c.delivery.batch_max_chars, 50))
        # never more than the hook script prints for this harness, so its cut
        # is only a safety net (it prints nothing rather than cut, §7.3)
        ctx = min(ctx, HOOK_CONTEXT_MAX.get(self.harness, ctx))
        return Caps(ctx_max_chars=ctx, wait_cap_s=wait)

    def tier(self, p: Participant | None) -> tuple[str, str | None]:
        return "mcp-only", None

    def conn_tier(self, ident: Any, existing: Participant | None) -> tuple[str, str | None]:
        """The tier a join from this verified MCP connection gets (Claude: inbox if attached)."""
        return self.tier(existing)

    def on_joined(self, p: Participant, nonce: str, fresh: bool) -> None:
        """A join (``fresh``: a new membership or another process) was persisted."""
        return None

    def defer_end(self, p: Participant) -> bool:
        """The liveness check found ``p``'s agent process gone: True keeps the
        session for now (Codex: a daemon restart's grace window), False ends it."""
        return False

    def on_mcp_hello(self, ident: Any, mine: list[Participant]) -> None:
        """A verified ``mcp.hello`` (``mine``: the sessions of that same MCP process)."""
        return None

    def status_summary(self) -> str | None:
        """A one-line state for ``switchboard status`` (Codex: the link), or None."""
        return None

    def join_guidance(self, p: Participant, room: str) -> str:
        return (
            "switchboard can't wake this session on its own. Call"
            f' read("{room}") to check for messages, or wait("{room}", {self.caps(p).wait_cap_s})'
            " to block until one arrives."
        )

    def context_events(self, p: Participant) -> frozenset[str]:
        """Hook events whose reply may carry priority context for this participant."""
        return frozenset()

    # -- routing (pure) -----------------------------------------------------
    def route(self, p: Participant, rel: Release, sink: Any, now: float) -> Route:
        raise NotImplementedError

    def on_hook(self, p: Participant, ev: HookEvent) -> dict[str, Any] | None:
        """Harness-specific side effects of a hook event (Codex: holds, thread proof)."""
        return None

    # -- stop-time delivery (M5: Cursor stop park, Devin Stop block) ----------
    def park_s(self, p: Participant, ev: HookEvent) -> float | None:
        """Seconds a stop hook may park waiting for a follow-up (Cursor), else None."""
        return None

    def stop_continues(self, p: Participant) -> bool:
        """True if this harness's Stop hook may return a continue at once (Devin)."""
        return False

    def redeliver_on_stop(self, p: Participant, ev: HookEvent) -> bool:
        """Stop re-delivers unanswered priority items once (§8.5) unless this says no."""
        return True

    def closes_waits(self, p: Participant, ev: HookEvent) -> bool:
        """True if this hook means an open wait() of the session was orphaned (Devin)."""
        return False

    def is_wait_pre(self, ev: HookEvent) -> bool:
        """A PreToolUse for switchboard's wait() whose tool_use_id pins its answer (Devin)."""
        return False

    def pull_confirms(self, path: str, want_tool_use: str | None, ev: HookEvent) -> bool:
        """Harness rule on top of the batch token for a pull answer (Devin: only
        the wait() call's own successful PostToolUse)."""
        return True

    def continue_verdict(
        self, p: Participant, loop_count: int | None, acked: bool, ev: HookEvent
    ) -> str | None:
        """For an unconfirmed stop continuation: 'confirm', an expiry reason, or None."""
        return None

    def confirm_window_s(self) -> float:
        """How long an acked stop continuation may wait for the session's next hook."""
        return 180.0

    def expire_due(self, p: Participant, b: Batch, now: float) -> str | None:
        """Event-based expiry of an offered push batch (§8.7): a reason, or None to keep it."""
        return None

    def push_expired(self, p: Participant, b: Batch, reason: str, now: float) -> None:
        """A push batch of this participant expired (``p.push_expiries`` already counts it)."""
        return None

    # -- transport ------------------------------------------------------------
    async def send(self, p: Participant, batch: Batch, text: str, **meta: Any) -> float | None:
        """Carry out a push route. Returns when the frame was posted (epoch s), if known."""
        raise NotImplementedError(f"{self.harness} has no push transport")

    async def start(self, runner: Any) -> None:
        return None

    async def stop(self) -> None:
        return None


class PullAdapter(Adapter):
    """Routing for sessions with no harness adapter (``unknown``): an open
    wait() sink gets wakes; mid-task priority items are pulled by the agent's
    next read()/say(); otherwise the member is parked. (Claude, Codex, Cursor
    and Devin have their own adapters since M3-M5.)"""

    def __init__(self, harness: str, cfg: Config):
        super().__init__(cfg)
        self.harness = harness

    def tier(self, p: Participant | None) -> tuple[str, str | None]:
        if self.harness == "codex":
            # _meta.threadId is taken on trust until the M4 thread proof (§9.3)
            return "mcp-only", "unverified thread"
        return "mcp-only", None

    def context_events(self, p: Participant) -> frozenset[str]:
        # Codex, Cursor and Devin hook outputs are enabled with their adapters (M4, M5).
        return frozenset()

    def route(self, p: Participant, rel: Release, sink: Any, now: float) -> Route:
        if sink is not None:
            return Route("sink", path=sink.path, sink_id=sink.id)
        if rel.kind == "priority":
            return Route("pull", reason="next tool call")
        return Route("none", reason="idle and not listening: call wait() or poke it")
