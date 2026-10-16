"""Sessions over HTTPS (DESIGN.md §49.3, #382): the adapters of agents that reach the broker at
``/mcp`` with an agent token.

No push path reaches the machine such an agent runs on (#383 brings wakes), so each is pull only,
as ``PullAdapter`` is: an open ``wait()`` serves an idle one, and its ``read()`` and ``say()``
answers carry what came meanwhile. What a harness adds here is its session key, from what its
calls name. Claude Code's calls carry no session id, but its PreToolUse hook reports one for each
call (``agent_http.HookUses``), so a session is ``claude@<device>:<session id>``. Codex over HTTPS
has no adapter of its own yet: it is the remote Codex adapter (``codex@remote``, §27.7), keyed by
the thread its calls name.
"""

from __future__ import annotations

from typing import Any

from switchboard.adapters.base import PullAdapter
from switchboard.config import Config
from switchboard.models import Participant, session_key

TIER = "claude:https"
NOTE = "over HTTPS: pull only"
NO_SESSION = (
    "this Claude Code call named no session: switchboard's PreToolUse hook reports it before each"
    " call (docs/USAGE.md, Agents over HTTPS)"
)


class HttpsClaudeAdapter(PullAdapter):
    """Claude Code over HTTPS: one session per Claude session id, on the token's device."""

    def __init__(self, cfg: Config):
        super().__init__("claude", cfg)

    def tier(self, p: Participant | None) -> tuple[str, str | None]:
        return TIER, NOTE

    def session_key(self, ident: Any, thread_id: str | None, *, test_session: str | None = None) -> str:
        if not thread_id:
            raise ValueError(NO_SESSION)
        return session_key("claude", ident.host, thread_id)
