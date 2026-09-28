"""Codex on a remote host (DESIGN.md §27.7): tier ``codex:hook``, pull only.

Every Codex push path (``turn/start``, ``turn/steer``, ``codex queue``, the lsof
check of attached TUIs) talks to a Codex app-server on the broker's own machine
(``adapters/codex.py``), so none of it can reach a thread on another host; a
satellite-side Codex link is future work. A remote Codex session is a hook
member like Devin's without the Stop re-arm: its PostToolUse hook carries
priority context mid-task, and ``wait()`` (240 s) serves it when idle. Its
thread id is taken from ``_meta.threadId`` (the credential check keys it by
host), with no thread proof and no CodexLink state; ``CodexAdapter`` never sees
it.
"""

from __future__ import annotations

from typing import Any

from switchboard.adapters.base import HOOK_CONTEXT_EVENTS, PullAdapter
from switchboard.config import Config
from switchboard.models import Participant, Release, Route

TIER = "codex:hook"
NOTE = "remote Codex: pull only"


class RemoteCodexAdapter(PullAdapter):
    def __init__(self, cfg: Config):
        super().__init__("codex", cfg)

    def tier(self, p: Participant | None) -> tuple[str, str | None]:
        return TIER, NOTE

    def context_events(self, p: Participant) -> frozenset[str]:
        return HOOK_CONTEXT_EVENTS["codex"]

    def join_guidance(self, p: Participant, room: str) -> str:
        return (
            "Room messages arrive as context after a tool call, or as the result of"
            f' wait("{room}", {self.caps(p).wait_cap_s}) when you have nothing else to do: switchboard can\'t'
            " start a turn in a Codex session on this machine. They are relayed by switchboard, never typed by"
            " your user."
        )

    def route(self, p: Participant, rel: Release, sink: Any, now: float) -> Route:
        if sink is not None:
            return Route("sink", path=sink.path, sink_id=sink.id)
        if rel.kind == "priority":
            return Route("pull", reason="next tool call")
        return Route("none", reason="remote Codex: idle and not listening; call wait() or poke it")
