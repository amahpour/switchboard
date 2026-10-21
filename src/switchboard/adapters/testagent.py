"""The scripted test agent's harness (DESIGN.md §9.6), tier ``mcp-only``.

A scripted agent connects to ``/mcp`` of a **test-mode** broker as the ``test`` client, with an agent
token like any other, and names its session (``X-Switchboard-Test-Session``) and how its pull answers
are confirmed (``X-Switchboard-Test-Ack``: ``next_call``, ``immediate`` or ``never``). A broker that
isn't in test mode refuses the client. Status is inferred from sinks (an open wait() is idle, anything
else busy); synthetic hook events from the test exercise hook confirmation and hook context.
"""

from __future__ import annotations

import re
from typing import Any

from switchboard.adapters.base import HOOK_CONTEXT_EVENTS, Adapter
from switchboard.config import Config
from switchboard.models import Participant, Release, Route
from switchboard.models import session_key as make_session_key

ACK_MODES = ("next_call", "immediate", "never")
TEST_SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class TestAgentAdapter(Adapter):
    harness = "test"
    reports_session_id = True
    sink_status = True
    test_only = True
    __test__ = False  # not a pytest class

    def tier(self, p: Participant | None) -> tuple[str, str | None]:
        return "mcp-only", None

    def session_key(self, host: str, thread_id: str | None, *, test_session: str | None = None) -> str:
        # keyed by the test session, not by a process: several scripted agents may share one (§9.6)
        if not isinstance(test_session, str) or not TEST_SESSION_RE.match(test_session):
            raise ValueError("a test agent needs a session key ([A-Za-z0-9_.-]{1,64})")
        return make_session_key(self.harness, host, test_session)

    def pull_ack(self, p: Participant, mode: str | None) -> str:
        return mode if mode in ACK_MODES else "next_call"

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
