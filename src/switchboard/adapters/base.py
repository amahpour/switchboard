"""The common adapter interface (DESIGN.md §9.1) and the pull adapter.

An adapter is what the engine asks about one harness's sessions (§49): how a session over HTTPS is
woken and what its hooks mean. ``route`` is pure: given a participant, what the rules released and
the member's open sink (if any), it says how the batch goes out: ``sink(id)`` (an open wait() call or
a parked Stop hook returns it), ``pull`` (the next hook or tool call claims it) or ``none(reason)``
(nothing can reach the agent right now). There is no push route: nothing reaches an agent over HTTPS
that it hasn't asked for (§49.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from switchboard.broker.hook_replies import CONTEXT_MAX as HOOK_CONTEXT_MAX
from switchboard.config import Config
from switchboard.models import HookEvent, Participant, Release, Route

INLINE_PATHS = frozenset({"wait", "read", "say"})

# Hook events whose reply may carry context, per harness (DESIGN.md §7.3; the table the broker
# answers from is ``broker/hook_replies.py``).
HOOK_CONTEXT_EVENTS: dict[str, frozenset[str]] = {
    "claude": frozenset({"SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure"}),
    "codex": frozenset({"PostToolUse"}),
    "devin": frozenset({"PostToolUse"}),
    "cursor": frozenset({"PostToolUse", "PostToolUseFailure"}),
    # synthetic hook events from scripted test agents (test mode only)
    "test": frozenset({"UserPromptSubmit", "PostToolUse", "PostToolUseFailure"}),
}


# What a join tells an agent about a member that is waiting on its person (§7.4, §49.4): a fact about
# the other member, and the choice left to the agent reading it. Switchboard never decides for it.
APPROVAL_GUIDANCE = (
    "A member whose status is waiting-approval (who() shows it) is paused at its person's approval"
    " prompt and answers nobody until they act. That is information about the member, not an"
    " instruction: whether to wait for it or move on is your call."
)


@dataclass(frozen=True)
class Caps:
    ctx_max_chars: int
    wait_cap_s: int
    inline_paths: frozenset[str] = INLINE_PATHS


class Adapter:
    """Base class. Subclasses override what their harness supports."""

    harness = "unknown"
    # What this harness's hooks may set on the session (§6.3): its own session id (one the
    # history tools know it by), and its approval mode (only Claude's and Codex's hooks carry
    # a ``permission_mode``).
    reports_session_id = False
    reports_approval_mode = False
    # Its status is inferred from its sinks, not its hooks: an open wait() is idle, anything
    # else busy (the scripted test agent, §9.6).
    sink_status = False
    # A scripted stand-in: it exists only in test mode (§9.6).
    test_only = False

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
        # never more than the broker prints for this harness (``hook_replies``), so its cut is only
        # a safety net (it answers nothing rather than cut, §7.3)
        ctx = min(ctx, HOOK_CONTEXT_MAX.get(self.harness, ctx))
        return Caps(ctx_max_chars=ctx, wait_cap_s=wait)

    def tier(self, p: Participant | None) -> tuple[str, str | None]:
        return "mcp-only", None

    # -- membership: session keys (DESIGN.md §6.3) ---------------------------
    def session_key(self, host: str, thread_id: str | None, *, test_session: str | None = None) -> str:
        """This harness's session key for a call on ``host`` (the token's device): what the call
        names (Codex's thread, Claude Code's session id, ...). A harness with no key refuses with a
        ``ValueError`` whose message goes to the caller."""
        raise ValueError(f"a {self.harness} session can't be told apart: its calls name no session")

    def join_conflict(self, store: Any, thread_id: str | None, host: str) -> str | None:
        """Why this session cannot join from ``host``, or None."""
        return None

    def credential_error(self, p: Participant, params: dict[str, Any]) -> str | None:
        """Why this call's harness identity does not match its membership."""
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

    def history_session_id(self, p: Participant, cfg: Config) -> tuple[str | None, str]:
        """``(this harness's own session id for p, "")``, or ``(None, why not)``, for a
        /catchup session-history request (DESIGN.md §26). The default: a harness
        switchboard keeps no history-tool id for."""
        return None, "a session of an unknown harness"

    def pull_ack(self, p: Participant, mode: str | None) -> str:
        """How a wait/read/say answer to ``p`` is confirmed (§8.7): ``hooks`` (its hooks,
        or its next switchboard call while none were ever seen), ``next_call``,
        ``immediate`` or ``never``. ``mode`` is the ack a scripted test agent asked for (§9.6),
        which only the test agent's adapter takes."""
        return "hooks"

    # -- routing (pure) -----------------------------------------------------
    def route(self, p: Participant, rel: Release, sink: Any, now: float) -> Route:
        raise NotImplementedError

    def is_mid_turn(self, p: Participant, ev: HookEvent, was_idle: bool) -> bool:
        """True if this UserPromptSubmit is input into the running turn, not a new turn
        (Codex's steered input, §9.3): the prompt id, the taint and the pull answers stay."""
        return False

    def other_thread(self, p: Participant, ev: HookEvent) -> bool:
        """Whether this hook belongs to a subagent sharing the session ID."""
        return False

    def anchors_turn(self, p: Participant, ev: HookEvent) -> bool:
        """Whether a tool hook can identify the current turn when its prompt preceded join."""
        return False

    def starts_subagent(self, ev: HookEvent) -> bool:
        """True if this hook starts a background subagent whose hooks carry the prompt's own
        ids (Devin's ``run_subagent``, §9.5): the engine taints that prompt, so no context,
        continue or re-delivery can reach the subagent."""
        return False

    def wait_drop_means_gone(self) -> bool:
        """True if an open wait() that loses its connection, followed by no call or hook from the
        session for a round and a margin, means the agent is gone (Devin: a killed one sends no
        SessionEnd, #448). A live agent re-arms its wait at once."""
        return False

    def stop_wait_lost(self, held_s: float) -> str | None:
        """Why a session is not listening, when the call of its Stop hook (a park) ended from its own
        side before any message came, after ``held_s`` seconds: Members says so until the session's
        next hook, because nothing can wake it meanwhile. None: nothing to say (the default; a
        Claude Code listener comes back by itself, §49.4)."""
        return None

    def asks_approval(self, ev: HookEvent) -> bool:
        """True if this hook says the session has put an approval prompt in front of its person
        (§7.4): the engine shows it as ``waiting-approval`` until the session's next hook. Only
        Claude Code has such a hook that isn't a permission hook (its Notification, #472). It is
        information for the room and nothing more: the reply to it is always empty (§7.3)."""
        return False

    def ends_session(self, ev: HookEvent) -> bool:
        """True if this SessionEnd takes the session offline (§7.4). Claude's ``/clear``
        keeps the session, so its ``reason=clear`` doesn't."""
        return True

    # -- stop-time delivery (Cursor's and Claude's listener, Devin's Stop block) --
    def park_s(self, p: Participant, ev: HookEvent) -> float | None:
        """Seconds a stop hook may park waiting for a follow-up, else None."""
        return None

    def stop_continues(self, p: Participant) -> bool:
        """True if this harness's Stop hook may return a continue at once (Devin)."""
        return False

    def rearm_caps(self) -> tuple[int, int]:
        """How many "keep listening" re-arms a Stop with nothing to deliver may return:
        ``(per prompt, per hour)`` for one session, ``(0, 0)`` for none (Devin, §9.5)."""
        return 0, 0

    def rearm_text(self, room: str, wait_s: int) -> str:
        """The re-arm a Stop continues with, when ``rearm_caps`` allows one."""
        raise NotImplementedError(f"{self.harness} has no Stop re-arm")

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

    def continue_verdict(self, p: Participant, acked: bool, ev: HookEvent) -> str | None:
        """For an unconfirmed stop continuation: 'confirm', an expiry reason, or None."""
        return None

    def confirm_window_s(self) -> float:
        """How long an acked stop continuation may wait for the session's next hook."""
        return 180.0


class PullAdapter(Adapter):
    """Routing for sessions with no harness adapter (``unknown``, and the base of the sessions over
    HTTPS, §49.3): an open wait() sink gets wakes; mid-task priority items are pulled by the agent's
    next read()/say(); otherwise the member is parked."""

    def __init__(self, harness: str, cfg: Config):
        super().__init__(cfg)
        self.harness = harness

    def route(self, p: Participant, rel: Release, sink: Any, now: float) -> Route:
        if sink is not None:
            return Route("sink", path=sink.path, sink_id=sink.id)
        if rel.kind == "priority":
            return Route("pull", reason="next tool call")
        return Route("none", reason="idle and not listening: call wait() or poke it")
