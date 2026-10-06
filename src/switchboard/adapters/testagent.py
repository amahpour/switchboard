"""The scripted test agent's harness (DESIGN.md §9.6), tier ``mcp-only``.

``switchboard mcp --harness test --test-session KEY --ack next_call|immediate|never``
works only against a test-mode broker. Status is inferred from sinks (an
open wait() is idle, anything else busy); synthetic hook events from a
subprocess of the fake agent exercise hook confirmation and hook context.
"""

from __future__ import annotations

from typing import Any

from switchboard.adapters.base import HOOK_CONTEXT_EVENTS, Adapter
from switchboard.config import Config
from switchboard.models import Participant, Release, Route

ACK_MODES = ("next_call", "immediate", "never")


class TestAgentAdapter(Adapter):
    harness = "test"
    __test__ = False  # not a pytest class

    def tier(self, p: Participant | None) -> tuple[str, str | None]:
        return "mcp-only", None

    def context_events(self, p: Participant) -> frozenset[str]:
        return HOOK_CONTEXT_EVENTS["test"]

    def history_session_id(self, p: Participant, cfg: Config) -> tuple[str | None, str]:
        return None, "a test session"

    def join_guidance(self, p: Participant, room: str) -> str:
        return (
            f'scripted test agent: call read("{room}") or wait("{room}", {self.caps(p).wait_cap_s});'
            " mid-task items can also arrive as synthetic hook context."
        )

    def route(self, p: Participant, rel: Release, sink: Any, now: float) -> Route:
        if sink is not None:
            return Route("sink", path=sink.path, sink_id=sink.id)
        if rel.kind == "priority":
            return Route("pull", reason="next tool call")
        return Route("none", reason="not waiting")
