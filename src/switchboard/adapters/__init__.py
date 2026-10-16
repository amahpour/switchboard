"""Per-harness adapters (DESIGN.md §9): routing (pure) and transport (async)."""

from __future__ import annotations

from switchboard.adapters.base import Adapter, Caps, PullAdapter
from switchboard.adapters.claude import ClaudeAdapter
from switchboard.adapters.codex import CodexAdapter
from switchboard.adapters.cursor import CursorAdapter
from switchboard.adapters.devin import DevinAdapter
from switchboard.adapters.https import HttpsClaudeAdapter
from switchboard.adapters.remote_codex import RemoteCodexAdapter
from switchboard.adapters.testagent import TestAgentAdapter
from switchboard.config import Config

# The adapter of a Codex session on another host (DESIGN.md §27.7); never a harness name.
REMOTE_CODEX = "codex@remote"
# The adapter of a Claude Code session over HTTPS (§49.3); never a harness name.
HTTPS_CLAUDE = "claude@https"


def build_adapters(cfg: Config) -> dict[str, Adapter]:
    """Claude (M3) and Codex (M4) have push tiers; Cursor (stop park, M5) and
    Devin (wait loop plus Stop re-arm, M5) are reached through their hooks. A
    Codex session on a remote host is pull only (``REMOTE_CODEX``, M8c), and so is a
    Claude Code session over HTTPS (``HTTPS_CLAUDE``, §49.3)."""
    return {
        "claude": ClaudeAdapter(cfg),
        "codex": CodexAdapter(cfg),
        REMOTE_CODEX: RemoteCodexAdapter(cfg),
        HTTPS_CLAUDE: HttpsClaudeAdapter(cfg),
        "cursor": CursorAdapter(cfg),
        "devin": DevinAdapter(cfg),
        "unknown": PullAdapter("unknown", cfg),
        "test": TestAgentAdapter(cfg),
    }


__all__ = [
    "HTTPS_CLAUDE",
    "REMOTE_CODEX",
    "Adapter",
    "Caps",
    "ClaudeAdapter",
    "CodexAdapter",
    "CursorAdapter",
    "DevinAdapter",
    "HttpsClaudeAdapter",
    "PullAdapter",
    "RemoteCodexAdapter",
    "TestAgentAdapter",
    "build_adapters",
]
