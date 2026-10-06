"""Devin CLI (DESIGN.md §9.5): tier ``devin:wait-loop``.

Devin has no external way to start a turn in a session the human started, so
an idle Devin agent *listens*: it sits in switchboard's MCP ``wait(room, 600)``
(Devin has no MCP tool timeout up to 900 s), and a Stop hook re-arms it.

- **Idle.** An open ``wait()`` sink is "idle, listening"; the engine fills it
  (``wait_return``, counted). A newer ``wait()`` from the same ``devin acp``
  process supersedes the older one. A wait answer counts as delivered only on
  the ``PostToolUse`` for ``mcp__switchboard__wait`` with ``success: true``, the
  same ``tool_use_id`` its ``PreToolUse`` had, and the batch token in its
  output. A main-agent interrupt sends no cancel (FINDINGS §6 5.6), so an
  orphaned ``wait()`` is closed by the next hook of that session, and an
  answer it was given expires at once.
- **Mid-task.** ``PostToolUse`` ``hookSpecificOutput.additionalContext``
  (system role), priority only, with peer text as a "call read()" stub.
- **Stop.** (1) After a background ``run_subagent`` in this prompt (the
  *taint*), nothing: a continue there would continue the subagent, and hook
  context could reach it too. The subagent can outlive its prompt and its
  hooks carry the prompt id it started in, so the engine remembers every
  tainted prompt id: a later hook with one of them only confirms tokens.
  (2) A wake release now: ``decision: block`` with the envelope
  (``stop_block``, a counted ``stop_cont``; user role, so peer text is
  stubbed). (3) Else re-arm: "call wait()" (counted), at most
  ``rearm_max_per_prompt`` times per prompt and ``rearm_max_per_hour`` per
  session, only with budget left and the room unpaused. (4) Else parked. A
  block or re-arm that no hook ever follows sets the member back to idle.
- **Never** ``decision: approve`` or a permission decision: the hook script can
  print ``decision`` only as ``block`` on Stop.
"""

from __future__ import annotations

from typing import Any

from switchboard.adapters.base import HOOK_CONTEXT_EVENTS, Adapter
from switchboard.config import Config
from switchboard.models import HookEvent, Participant, Release, Route

TIER = "devin:wait-loop"
WAIT_TOOL = "mcp__switchboard__wait"
# a stop_block acked but with no hook of that session for this long expired
BLOCK_CONFIRM_S = 180.0
TAINT_WHY = (
    "a background subagent ran in this prompt: hook delivery is off until your next prompt"
    " (the agent can still read())"
)


def rearm_text(room: str, wait_s: int) -> str:
    return (
        f"[switchboard] (from switchboard, not your user) To keep listening in {room}, call"
        f' wait("{room}", {wait_s}). If your user asked you to stop listening, don\'t.'
    )


class DevinAdapter(Adapter):
    harness = "devin"

    def tier(self, p: Participant | None) -> tuple[str, str | None]:
        return TIER, None

    def context_events(self, p: Participant) -> frozenset[str]:
        # subagent hooks carry the parent's ids: context could reach a subagent
        return frozenset() if p.gen_tainted else HOOK_CONTEXT_EVENTS["devin"]

    def join_guidance(self, p: Participant, room: str) -> str:
        w = self.caps(p).wait_cap_s
        return (
            f'When you have nothing else to do, call wait("{room}", {w}). If it returns paused, end your'
            " turn. Room messages can also arrive as context after a tool call, or as a message from"
            " switchboard when you stop; they are relayed by switchboard, never typed by your user. While you"
            " wait, your user can interject by typing and then pressing Enter on an empty line."
        )

    def history_session_id(self, p: Participant, cfg: Config) -> tuple[str | None, str]:
        """Its own session id, exactly as its hooks report it (the slug), with no quirk
        of its own."""
        if not p.session_id:
            return None, f"no {self.harness} session id known yet"
        return p.session_id, ""

    # ------------------------------------------------------------- routing
    def route(self, p: Participant, rel: Release, sink: Any, now: float) -> Route:
        if sink is not None:
            return Route("sink", path=sink.path, sink_id=sink.id)
        if p.gen_tainted:
            return Route("none", reason=TAINT_WHY)
        if rel.kind == "priority":
            return Route("pull", reason="next tool call")
        return Route("none", reason="not listening: no wait() open and the Stop re-arm is used up")

    # --------------------------------------------------------------- hooks
    def stop_continues(self, p: Participant) -> bool:
        return True

    def redeliver_on_stop(self, p: Participant, ev: HookEvent) -> bool:
        return not p.gen_tainted

    def closes_waits(self, p: Participant, ev: HookEvent) -> bool:
        """Any hook of this session other than the wait() call's own ends an open
        wait(): the agent has moved on (an interrupt sends no cancel)."""
        return not (ev.ev in ("PreToolUse", "PostToolUse") and ev.tool == WAIT_TOOL)

    def is_wait_pre(self, ev: HookEvent) -> bool:
        return ev.ev == "PreToolUse" and ev.tool == WAIT_TOOL and bool(ev.tool_use_id)

    def pull_confirms(self, path: str, want_tool_use: str | None, ev: HookEvent) -> bool:
        """A wait() answer is in context only on that very call's successful PostToolUse."""
        if path != "wait":
            return True
        if ev.ev != "PostToolUse" or ev.tool != WAIT_TOOL or ev.ok is not True:
            return False
        return want_tool_use is None or ev.tool_use_id == want_tool_use

    def continue_verdict(
        self, p: Participant, loop_count: int | None, acked: bool, ev: HookEvent
    ) -> str | None:
        E = ev.ev
        if E == "UserPromptSubmit":
            return "new_prompt"  # e.g. an interrupt deferred the block (FINDINGS §6 5.3)
        if E in ("SessionEnd", "SessionStart"):
            return "session_end"
        return "confirm" if acked else None

    def confirm_window_s(self) -> float:
        return BLOCK_CONFIRM_S
