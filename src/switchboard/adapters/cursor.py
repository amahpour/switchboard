"""Cursor Agent CLI (DESIGN.md §9.4): tier ``cursor:stop-park`` (provisional).

Built from the recorded M0 contracts; no live run yet, so the tier is shown
as *provisional* everywhere.

- **Binding.** The MCP server gets no chat id from Cursor, so ``join``
  creates the participant *pending* with a random nonce and puts ``yk:j<nonce>``
  in its result. The ``postToolUse`` hook for ``MCP:join`` (from a process
  whose ancestry holds the same agent) carries that nonce plus the
  ``conversation_id``; the broker then keys the session ``cursor:<id>``.
  Until then it is ``mcp-only`` (tools work; nothing is pushed or given as
  hook context).
- **Mid-task.** ``postToolUse`` / ``postToolUseFailure`` ``additional_context``,
  priority only, batches fitted to at most 8,000 characters (Cursor drops a
  context over 10,000 whole and silently).
- **Idle.** The ``stop`` hook parks (a long poll on the broker socket) only when
  the stop ``status`` is ``completed`` (an aborted or errored turn never
  continues: Cursor ignores a follow-up then). The park is filled with a wake
  batch sent back as ``followup_message`` (path ``stop_followup``, counted as a
  ``stop_cont`` wake): it arrives as the next user message, so peer text is a
  "call read()" stub. The follow-up is confirmed by the hook's ack plus the
  next hook of that conversation (a ``postToolUse``, or a ``stop`` whose
  ``loop_count`` is one more); a ``loop_count`` reset, a human prompt, the
  session ending or no hook for a while expire it.
- One live park per conversation: a newer stop, any other hook of the
  conversation (the human typed), ``/pause`` of its rooms, ``/kick`` and the
  agent's death release the old one with no continuation.
- After ``max_unconfirmed_followups`` follow-ups in a row expire unconfirmed,
  the member is **degraded**: no more parks (parked, needs a poke) until the
  human's next prompt in that session. Only a confirmed follow-up breaks a run
  of misses: a dropped follow-up leaves the agent idle until the human types,
  so that prompt can't reset the count (it only ends a degraded spell). The
  human typing at least ``FOLLOWUP_RACE_S`` after a follow-up was printed, with
  no hook of it in between, counts as a miss.
- A follow-up that expires with no hook at all (never acked, or no hook for
  ``FOLLOWUP_CONFIRM_S``) sets the member back to idle: shown parked, not busy.
- ``loop_limit`` is ``null`` in the hook config: switchboard's own wake budget
  bounds the follow-ups.
"""

from __future__ import annotations

from typing import Any

from switchboard.adapters.base import HOOK_CONTEXT_EVENTS, Adapter
from switchboard.models import HookEvent, Participant, Release, Route, split_session_key

TIER = "cursor:stop-park"
NOTE = "provisional"
NOTE_DEGRADED = "provisional, degraded"
# a pending (unbound) session key's part after the harness and host: agent:<pid>@<start>
PENDING_REST = "agent:"
# the hook gives the broker its own wait budget; the park ends this much sooner
PARK_MARGIN_S = 30.0
MIN_PARK_S = 1.0
# an acked follow-up with no hook from its conversation for this long expired
FOLLOWUP_CONFIRM_S = 180.0
# the human typing this long after a follow-up was printed (with no hook of the
# follow-up turn in between) counts as a follow-up Cursor never ran; sooner is a race
FOLLOWUP_RACE_S = 10.0

UNBOUND_WHY = (
    "not bound to a Cursor conversation yet (no switchboard postToolUse hook for join():"
    " run `switchboard install cursor`, then start a new agent session)"
)


def bound(p: Participant) -> bool:
    """Bound to a Cursor conversation: its key is ``cursor:<conversation>`` here, or
    ``cursor@<host>:<conversation>`` on a remote host (DESIGN.md §27.5.4), and no
    longer the pending ``…:agent:<pid>@<start>``."""
    parts = split_session_key(p.session_key)
    return (
        p.bind_state == "bound"
        and parts is not None
        and parts[0] == "cursor"
        and not parts[2].startswith(PENDING_REST)
    )


class CursorAdapter(Adapter):
    harness = "cursor"

    def degraded(self, p: Participant) -> bool:
        n = self.cfg.cursor.max_unconfirmed_followups
        return n > 0 and p.unconfirmed_followups >= n

    # -------------------------------------------------------- capabilities
    def tier(self, p: Participant | None) -> tuple[str, str | None]:
        if p is None or not bound(p):
            return "mcp-only", "binding"
        return TIER, (NOTE_DEGRADED if self.degraded(p) else NOTE)

    def context_events(self, p: Participant) -> frozenset[str]:
        return HOOK_CONTEXT_EVENTS["cursor"] if bound(p) else frozenset()

    def join_guidance(self, p: Participant, room: str) -> str:
        return (
            "Room messages arrive as context after your tool calls, or as a follow-up message when you"
            " stop. Follow-ups are from switchboard, not your user. If who() shows your tier as mcp-only,"
            f' call wait("{room}", {self.caps(p).wait_cap_s}) when you have nothing else to do.'
        )

    # ------------------------------------------------------------- routing
    def route(self, p: Participant, rel: Release, sink: Any, now: float) -> Route:
        if sink is not None:
            return Route("sink", path=sink.path, sink_id=sink.id)
        if not bound(p):
            return Route("none", reason=UNBOUND_WHY)
        if rel.kind == "priority":
            return Route("pull", reason="next tool call")
        if self.degraded(p):
            return Route(
                "none",
                reason="stop follow-ups were not confirmed (degraded) until your next prompt in that session",
            )
        return Route("none", reason="stopped and not parked (no stop hook waiting)")

    # --------------------------------------------------------------- hooks
    def park_s(self, p: Participant, ev: HookEvent) -> float | None:
        """Seconds to park this stop hook for, or None to answer at once."""
        if ev.ev != "Stop" or ev.status != "completed" or not bound(p) or self.degraded(p):
            return None
        mw = ev.max_wait_s
        if mw is None:
            return None
        secs = min(float(self.cfg.cursor.stop_park_s), mw - PARK_MARGIN_S)
        return secs if secs >= MIN_PARK_S else None

    def redeliver_on_stop(self, p: Participant, ev: HookEvent) -> bool:
        # Ctrl+C gives stop 'aborted' then 'error': an abort is not "the turn ended unanswered"
        return ev.status == "completed"

    def continue_verdict(
        self, p: Participant, loop_count: int | None, acked: bool, ev: HookEvent
    ) -> str | None:
        """What this hook says about an unconfirmed follow-up sent at a stop
        whose ``loop_count`` was ``loop_count``: 'confirm', an expiry reason, or None."""
        E = ev.ev
        if E in ("UserPromptSubmit",):
            return "human_prompt"  # the human typed before the follow-up ran
        if E in ("SessionEnd", "SessionStart"):
            return "session_end"
        if E == "Stop":
            if loop_count is not None and ev.loop_count == loop_count + 1:
                return "confirm" if acked else None  # the follow-up turn ended (no tool ran)
            return "loop_reset"
        if E in ("PostToolUse", "PostToolUseFailure"):
            return "confirm" if acked else None
        return None

    def confirm_window_s(self) -> float:
        return FOLLOWUP_CONFIRM_S
