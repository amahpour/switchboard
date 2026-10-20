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

from typing import Any

from switchboard import __version__
from switchboard.adapters.base import HOOK_CONTEXT_EVENTS, PullAdapter
from switchboard.adapters.codex import CodexMembership, steered
from switchboard.config import Config
from switchboard.models import HookEvent, Participant, Release, Route, session_key

CLAUDE_TIER = "claude:https"
CODEX_TIER = "codex:https"
CURSOR_TIER = "cursor:https"
NOTE = "over HTTPS: woken at stop"
NO_SESSION = (
    "this Claude Code call named no session: switchboard's PreToolUse hook reports it before each"
    " call (docs/USAGE.md, Agents over HTTPS)"
)
LISTEN_S = 86400.0  # the longest a Stop hook is held before it answers with nothing: a day
PARK_MARGIN_S = 5.0  # an answer this long before the hook's own deadline
MIN_PARK_S = 5.0
IDLE_WHY = "stopped and not listening: no Stop hook waiting (or call wait())"


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
        )

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

    def continue_verdict(
        self, p: Participant, loop_count: int | None, acked: bool, ev: HookEvent
    ) -> str | None:
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

    def session_key(self, ident: Any, thread_id: str | None, *, test_session: str | None = None) -> str:
        if not thread_id:
            raise ValueError(NO_SESSION)
        return session_key("claude", ident.host, thread_id)

    def ends_session(self, ev: HookEvent) -> bool:
        # /clear keeps the session: the same id goes on (as Claude Code locally, §7.4)
        return ev.reason != "clear"


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

    def prompt_verdict(self, acked: bool) -> str | None:
        return "human_prompt"


class HttpsCursorAdapter(Listening):
    """Cursor over HTTPS, keyed by the conversation ID its preToolUse hook puts on each call."""

    reports_approval_mode = False

    def __init__(self, cfg: Config):
        super().__init__("cursor", cfg)

    def tier(self, p: Participant | None) -> tuple[str, str | None]:
        return CURSOR_TIER, NOTE

    def session_key(self, ident: Any, thread_id: str | None, *, test_session: str | None = None) -> str:
        if not thread_id:
            raise ValueError("this Cursor call has no verified conversation ID")
        return session_key("cursor", ident.host, thread_id)
