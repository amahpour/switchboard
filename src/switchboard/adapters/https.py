"""Sessions over HTTPS (DESIGN.md §49.3, §49.4, #382, #383): the adapters of agents that reach the
broker at ``/mcp`` with an agent token.

No push path reaches the machine such an agent runs on, so an idle one is woken by its own Stop
hook, which waits on the broker (a park, as Cursor's stop hook does, §9.4) until a message comes:

- **Claude Code** runs a background listener at Stop: a ``curl`` loop in a command hook with
  ``asyncRewake``, which posts to ``/mcp/listen`` and waits, one round after another. The broker
  answers with the message, ``curl`` prints it, the hook exits 2, and Claude Code starts a turn with
  it, idle until then with no spinner. Any other answer ends the listener, waking nothing, except a
  round that ran out (408), after which it waits again.
- **Codex** holds its Stop hook (an ``mcp_tool`` hook calling the hidden ``hook`` tool) open in the
  same way, and the answer is ``decision: block`` with the message, so Codex carries on with it.
  Codex shows "Working" meanwhile, up to a day, and the person presses Esc to type (#380's
  decision 2).

A busy session gets priority messages as hook context after its tool calls, and everything else at
its next ``read()``, ``wait()`` or ``say()``. Each harness's session key comes from what its calls
name: Claude Code's session id, which its PreToolUse hook reports before each call
(``claude@<device>:<session id>``), and Codex's thread (``codex@<device>:<thread id>``).
"""

from __future__ import annotations

import dataclasses
from typing import Any

from switchboard import __version__
from switchboard.adapters.base import APPROVAL_GUIDANCE, HOOK_CONTEXT_EVENTS, Adapter, Caps, PullAdapter
from switchboard.config import Config
from switchboard.models import (
    HookEvent,
    Participant,
    Release,
    Route,
    session_key,
    split_session_key,
)

# the Notification type of an open approval prompt in Claude Code (live, 2.1.305: 11 ms after the prompt's
# PermissionRequest hook); the plugin's matcher and the broker both name it, and no other counts
APPROVAL_NOTIFICATION = "permission_prompt"
CLAUDE_TIER = "claude:https"
CODEX_TIER = "codex:https"
CURSOR_TIER = "cursor:https"
DEVIN_TIER = "devin:https"
# how Devin names the tools of the plugin's HTTPS server in its hooks (``mcp__<server>__<tool>``)
DEVIN_TOOL_PREFIX = "mcp__switchboard_https__"
# Devin's HTTP client sends a call again after 120 s of silence (measured, Devin CLI 3000.11.92); the
# one-use hook report is spent by then, so the repeat is refused and the first wait() is orphaned.
# A wait() under this ends before that.
DEVIN_WAIT_CAP_S = 100
NOTE = "over HTTPS: woken at stop"
NO_SESSION = (
    "this Claude Code call named no session: switchboard's PreToolUse hook reports it before each"
    " call (docs/USAGE.md, Agents over HTTPS)"
)
LISTEN_S = 86400.0  # the longest a Stop hook is held before it answers with nothing: a day
PARK_MARGIN_S = 5.0  # an answer this long before the hook's own deadline
MIN_PARK_S = 5.0
IDLE_WHY = "stopped and not listening: no Stop hook waiting (or call wait())"


def _held(secs: float) -> str:
    """How long a wait lasted, in a word or two: ``40 s``, ``5 min``, ``3 h``."""
    if secs < 90:
        return f"{max(1, round(secs))} s"
    if secs < 5400:
        return f"{round(secs / 60)} min"
    return f"{round(secs / 3600)} h"


class CodexMembership:
    """Codex's session identity (§6.3): one session per thread, named on every call."""

    def session_key(self, host: str, thread_id: str | None, *, test_session: str | None = None) -> str:
        if not thread_id:
            raise ValueError("Codex calls must carry _meta.threadId")
        return session_key("codex", host, thread_id)

    def join_conflict(self, store: Any, thread_id: str | None, host: str) -> str | None:
        if thread_id is None:
            return None  # session_key() reports the missing thread id first
        for p in store.active_participants():
            if p.harness != "codex" or p.host == host:
                continue
            parts = split_session_key(p.session_key)
            if parts is not None and parts[2] == thread_id:
                return "this thread is joined from another machine"
        return None

    def credential_error(self, p: Participant, params: dict[str, Any]) -> str | None:
        thread_id = params.get("thread_id")
        if not isinstance(thread_id, str) or p.session_key != session_key("codex", p.host, thread_id):
            return "this call names another Codex thread than the membership's"
        return None


def steered(p: Participant, ev: HookEvent, was_idle: bool) -> bool:
    """Input steered into a running turn (a turn/steer, or a turn/start merged into a busy
    turn) fires UserPromptSubmit with that turn's own id: mid-turn input, not a new turn
    (M4 live)."""
    return ev.ev == "UserPromptSubmit" and bool(ev.gen) and (ev.gen or "")[:128] == p.gen and not was_idle


class Listening(PullAdapter):
    """What both harnesses share over HTTPS: a Stop hook that waits for a message (a park), hook
    context for a busy session, and a follow-up confirmed by the session's next hook."""

    reports_approval_mode = True  # permission_mode comes with each hook (§9.2, §9.3)

    def context_events(self, p: Participant) -> frozenset[str]:
        return HOOK_CONTEXT_EVENTS[self.harness]

    def join_guidance(self, p: Participant, room: str) -> str:
        """What the join tells the agent: the default says switchboard can't wake it, which is
        wrong here (#402): its Stop hook waits on the broker and wakes it when it is idle."""
        return (
            f"This is switchboard {__version__}. Room messages wake this session when it is idle: its"
            " Stop hook waits on switchboard for them. While you are working, priority ones arrive as"
            " context after your tool calls, and"
            f' the rest at your next read("{room}"), wait("{room}", {self.caps(p).wait_cap_s}) or say().'
            " kind=human ones are from your user and carry their authority just the same."
            f" {APPROVAL_GUIDANCE}"
        )

    def history_session_id(self, p: Participant, cfg: Config) -> tuple[str | None, str]:
        """Its own id, the one its calls name and its session key carries: Claude Code's session id,
        Codex's thread, Cursor's conversation (§26, /catchup)."""
        parts = split_session_key(p.session_key)
        if not p.session_id or parts is None or parts[2] != p.session_id:
            return None, f"no {self.harness} session id known yet"
        return p.session_id, ""

    def park_s(self, p: Participant, ev: HookEvent) -> float | None:
        """A Stop hook that says how long it may wait (``max_wait_s``) is held that long, less a
        margin, and at most ``LISTEN_S``. One that doesn't (a status report) answers at once."""
        if ev.ev != "Stop" or ev.max_wait_s is None:
            return None
        secs = min(LISTEN_S, ev.max_wait_s - PARK_MARGIN_S)
        return secs if secs >= MIN_PARK_S else None

    def route(self, p: Participant, rel: Release, sink: Any, now: float) -> Route:
        if sink is not None:
            return Route("sink", path=sink.path, sink_id=sink.id)
        if rel.kind == "priority":
            return Route("pull", reason="next tool call")
        return Route("none", reason=IDLE_WHY)

    def continue_verdict(self, p: Participant, acked: bool, ev: HookEvent) -> str | None:
        """A follow-up a Stop hook answered with is confirmed by the session's next hook once the
        answer went out (``acked``): the turn it started, or the turn it continued."""
        E = ev.ev
        if E in ("SessionEnd", "SessionStart"):
            return "session_end"
        if E == "UserPromptSubmit":
            return self.prompt_verdict(acked)
        if E in ("PostToolUse", "PostToolUseFailure", "Stop"):
            return "confirm" if acked else None
        return None

    def prompt_verdict(self, acked: bool) -> str | None:
        return "confirm" if acked else None


class HttpsClaudeAdapter(Listening):
    """Claude Code over HTTPS: one session per Claude session id, on the token's device. Its
    listener's answer starts a turn, whose UserPromptSubmit confirms it."""

    def __init__(self, cfg: Config):
        super().__init__("claude", cfg)

    def tier(self, p: Participant | None) -> tuple[str, str | None]:
        return CLAUDE_TIER, NOTE

    def session_key(self, host: str, thread_id: str | None, *, test_session: str | None = None) -> str:
        if not thread_id:
            raise ValueError(NO_SESSION)
        return session_key("claude", host, thread_id)

    def ends_session(self, ev: HookEvent) -> bool:
        # /clear keeps the session: the same id goes on (as Claude Code locally, §7.4)
        return ev.reason != "clear"

    def asks_approval(self, ev: HookEvent) -> bool:
        # Claude Code sends a Notification of type permission_prompt, with the PermissionRequest
        # hook's event (which switchboard doesn't register, §11), when it puts the prompt up (#472)
        return ev.ev == "Notification" and ev.notification == APPROVAL_NOTIFICATION


class HttpsCodexAdapter(CodexMembership, Listening):
    """Codex over HTTPS, keyed by thread. Its Stop hook's ``decision: block`` continues the turn
    it ended, so a UserPromptSubmit before the follow-up's next hook means the person pressed Esc
    and typed instead: the follow-up didn't run."""

    def __init__(self, cfg: Config):
        Listening.__init__(self, "codex", cfg)

    def tier(self, p: Participant | None) -> tuple[str, str | None]:
        return CODEX_TIER, NOTE

    def is_mid_turn(self, p: Participant, ev: HookEvent, was_idle: bool) -> bool:
        return steered(p, ev, was_idle)

    def other_thread(self, p: Participant, ev: HookEvent) -> bool:
        # Codex 0.158.0 runs a subagent's tool hooks with the parent's session_id and
        # the subagent's turn_id. Stop-block continuations keep the parent's turn_id
        # (checked live, #446). A missing parent turn cannot identify either caller.
        return (
            ev.ev in ("PostToolUse", "PostToolUseFailure")
            and bool(ev.gen)
            and bool(p.gen)
            and (ev.gen or "")[:128] != p.gen
        )

    def anchors_turn(self, p: Participant, ev: HookEvent) -> bool:
        # The first UserPromptSubmit may precede join, when no participant exists.
        # Its join/read/say tool hook can identify that turn once the session is in a room.
        return ev.ev in ("PostToolUse", "PostToolUseFailure") and (ev.tool or "").startswith(
            "mcp__switchboard__"
        )

    def prompt_verdict(self, acked: bool) -> str | None:
        return "human_prompt"

    def stop_wait_lost(self, held_s: float) -> str | None:
        """Codex ends the call of its Stop hook for Esc, and also when the MCP server's
        ``tool_timeout_sec`` runs out (#449: after 5 minutes by default, so a session whose setup
        lacks the setting stops listening at its first Stop). Either way nothing wakes it until its
        next hook, and it is not shown as waiting."""
        return (
            f"not listening: its Stop hook ended after {_held(held_s)} (Esc, or Codex's tool_timeout_sec:"
            " docs/USAGE.md)"
        )


class HttpsCursorAdapter(Listening):
    """Cursor over HTTPS, keyed by the conversation ID its preToolUse hook puts on each call."""

    reports_approval_mode = False

    def __init__(self, cfg: Config):
        super().__init__("cursor", cfg)

    def tier(self, p: Participant | None) -> tuple[str, str | None]:
        return CURSOR_TIER, NOTE

    def session_key(self, host: str, thread_id: str | None, *, test_session: str | None = None) -> str:
        if not thread_id:
            raise ValueError("this Cursor call has no verified conversation ID")
        return session_key("cursor", host, thread_id)


def rearm_text(room: str, wait_s: int) -> str:
    return (
        f"[switchboard] (from switchboard, not your user) To keep listening in {room}, call"
        f' wait("{room}", {wait_s}). If your user asked you to stop listening, don\'t.'
    )


class HttpsDevinAdapter(Adapter):
    """Devin over HTTPS, keyed by the session ID its PreToolUse hook puts on each call (§49.7), and
    woken by its wait loop (§9.5).

    Devin has no external way to start a turn in a session the human started, so an idle Devin agent
    *listens*: it sits in switchboard's ``wait(room, 600)`` (Devin has no MCP tool timeout up to 900
    s), and a Stop hook re-arms it.

    - **Idle.** An open ``wait()`` sink is "idle, listening"; the engine fills it (``wait_return``,
      counted). A newer ``wait()`` from the same session supersedes the older one. A wait answer counts
      as delivered only on the ``PostToolUse`` for the plugin's ``wait`` with ``success: true``, the
      same ``tool_use_id`` its ``PreToolUse`` had, and the batch token in its output. A main-agent
      interrupt sends no cancel (FINDINGS §6 5.6), so an orphaned ``wait()`` is closed by the next hook
      of that session, and an answer it was given expires at once.
    - **Mid-task.** ``PostToolUse`` ``hookSpecificOutput.additionalContext`` (system role), priority
      only, with peer text as a "call read()" stub.
    - **Stop.** (1) After a background ``run_subagent`` in this prompt (the *taint*), nothing: a
      continue there would continue the subagent, and hook context could reach it too. The subagent can
      outlive its prompt and its hooks carry the prompt id it started in, so the engine remembers every
      tainted prompt id: a later hook with one of them only confirms tokens. (2) A wake release now:
      ``decision: block`` with the envelope (``stop_block``, a counted ``stop_cont``; user role, so peer
      text is stubbed). (3) Else re-arm: "call wait()" (counted), at most ``rearm_max_per_prompt`` times
      per prompt and ``rearm_max_per_hour`` per session, only with budget left and the room unpaused.
      (4) Else parked. A block or re-arm that no hook ever follows sets the member back to idle.
    - **Never** ``decision: approve`` or a permission decision: ``decision`` is only ever ``block``, on
      Stop (``broker/hook_replies.py``).

    The plugin's server is not ``switchboard``, so the wait() whose hooks pin an answer is the one under
    ``DEVIN_TOOL_PREFIX``."""

    harness = "devin"
    reports_session_id = True  # the hooks' session_id is the one the history tools know
    wait_tool = f"{DEVIN_TOOL_PREFIX}wait"
    # a stop_block acked but with no hook of that session for this long expired
    block_confirm_s = 180.0
    taint_why = (
        "a background subagent ran in this prompt: hook delivery is off until your next prompt"
        " (the agent can still read())"
    )

    def caps(self, p: Participant | None = None) -> Caps:
        caps = super().caps(p)
        return dataclasses.replace(caps, wait_cap_s=min(caps.wait_cap_s, DEVIN_WAIT_CAP_S))

    def tier(self, p: Participant | None) -> tuple[str, str | None]:
        return DEVIN_TIER, "over HTTPS: listening through wait()"

    def wait_drop_means_gone(self) -> bool:
        """Devin loops wait() and its hooks bracket every call, so a dropped wait followed by silence
        is a Devin that was killed: its SessionEnd hook never reached the server (#448)."""
        return True

    def session_key(self, host: str, thread_id: str | None, *, test_session: str | None = None) -> str:
        if not thread_id:
            raise ValueError("this Devin call has no verified session ID")
        return session_key("devin", host, thread_id)

    def context_events(self, p: Participant) -> frozenset[str]:
        # subagent hooks carry the parent's ids: context could reach a subagent
        return frozenset() if p.gen_tainted else HOOK_CONTEXT_EVENTS["devin"]

    def join_guidance(self, p: Participant, room: str) -> str:
        w = self.caps(p).wait_cap_s
        return (
            f'When you have nothing else to do, call wait("{room}", {w}). If it returns paused, end your'
            " turn. Room messages can also arrive as context after a tool call, or as a message from"
            " switchboard when you stop; kind=human ones are from your user, relayed by switchboard rather"
            " than typed into your prompt, and carry their authority just the same. While you"
            " wait, your user can interject by typing and then pressing Enter on an empty line."
            f" {APPROVAL_GUIDANCE}"
        )

    def history_session_id(self, p: Participant, cfg: Config) -> tuple[str | None, str]:
        """Its own session id, exactly as its hooks report it (the slug), with no quirk of its own."""
        if not p.session_id:
            return None, f"no {self.harness} session id known yet"
        return p.session_id, ""

    def route(self, p: Participant, rel: Release, sink: Any, now: float) -> Route:
        if sink is not None:
            return Route("sink", path=sink.path, sink_id=sink.id)
        if p.gen_tainted:
            return Route("none", reason=self.taint_why)
        if rel.kind == "priority":
            return Route("pull", reason="next tool call")
        return Route("none", reason="not listening: no wait() open and the Stop re-arm is used up")

    def starts_subagent(self, ev: HookEvent) -> bool:
        return ev.ev == "PreToolUse" and ev.subagent_bg

    def stop_continues(self, p: Participant) -> bool:
        return True

    def rearm_caps(self) -> tuple[int, int]:
        dv = self.cfg.devin
        return (dv.rearm_max_per_prompt, dv.rearm_max_per_hour) if dv.rearm else (0, 0)

    def rearm_text(self, room: str, wait_s: int) -> str:
        return rearm_text(room, wait_s)

    def redeliver_on_stop(self, p: Participant, ev: HookEvent) -> bool:
        return not p.gen_tainted

    def closes_waits(self, p: Participant, ev: HookEvent) -> bool:
        """Any hook of this session other than the wait() call's own ends an open
        wait(): the agent has moved on (an interrupt sends no cancel)."""
        return not (ev.ev in ("PreToolUse", "PostToolUse") and ev.tool == self.wait_tool)

    def is_wait_pre(self, ev: HookEvent) -> bool:
        return ev.ev == "PreToolUse" and ev.tool == self.wait_tool and bool(ev.tool_use_id)

    def pull_confirms(self, path: str, want_tool_use: str | None, ev: HookEvent) -> bool:
        """A wait() answer is in context only on that very call's successful PostToolUse."""
        if path != "wait":
            return True
        if ev.ev != "PostToolUse" or ev.tool != self.wait_tool or ev.ok is not True:
            return False
        return want_tool_use is None or ev.tool_use_id == want_tool_use

    def continue_verdict(self, p: Participant, acked: bool, ev: HookEvent) -> str | None:
        E = ev.ev
        if E == "UserPromptSubmit":
            return "new_prompt"  # e.g. an interrupt deferred the block (FINDINGS §6 5.3)
        if E in ("SessionEnd", "SessionStart"):
            return "session_end"
        return "confirm" if acked else None

    def confirm_window_s(self) -> float:
        return self.block_confirm_s
