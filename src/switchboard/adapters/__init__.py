"""Per-harness adapters (DESIGN.md §9): routing (pure) and transport (async)."""

from __future__ import annotations

from switchboard.adapters.base import Adapter, Caps, PullAdapter
from switchboard.adapters.claude import ClaudeAdapter
from switchboard.adapters.codex import CodexAdapter
from switchboard.adapters.cursor import CursorAdapter
from switchboard.adapters.devin import DevinAdapter
from switchboard.adapters.https import (
    HttpsClaudeAdapter,
    HttpsCodexAdapter,
    HttpsCursorAdapter,
    HttpsDevinAdapter,
)
from switchboard.adapters.remote_codex import RemoteCodexAdapter
from switchboard.adapters.testagent import TestAgentAdapter
from switchboard.config import Config

# The adapter of a Codex session on another host (DESIGN.md §27.7); never a harness name.
REMOTE_CODEX = "codex@remote"
# The adapters of Claude Code and Codex sessions over HTTPS (§49.3); never harness names.
HTTPS_CLAUDE = "claude@https"
HTTPS_CODEX = "codex@https"
HTTPS_CURSOR = "cursor@https"
HTTPS_DEVIN = "devin@https"


def build_adapters(cfg: Config) -> dict[str, Adapter]:
    """Claude (M3) and Codex (M4) have push tiers; Cursor (stop park, M5) and
    Devin (wait loop plus Stop re-arm, M5) are reached through their hooks. A
    Codex session on a remote host is pull only (``REMOTE_CODEX``, M8c). A session over
    HTTPS is woken by its own Stop hook (``HTTPS_CLAUDE``, ``HTTPS_CODEX``, §49.4)."""
    return {
        "claude": ClaudeAdapter(cfg),
        "codex": CodexAdapter(cfg),
        REMOTE_CODEX: RemoteCodexAdapter(cfg),
        HTTPS_CLAUDE: HttpsClaudeAdapter(cfg),
        HTTPS_CODEX: HttpsCodexAdapter(cfg),
        HTTPS_CURSOR: HttpsCursorAdapter(cfg),
        HTTPS_DEVIN: HttpsDevinAdapter(cfg),
        "cursor": CursorAdapter(cfg),
        "devin": DevinAdapter(cfg),
        "unknown": PullAdapter("unknown", cfg),
        "test": TestAgentAdapter(cfg),
    }


__all__ = [
    "HTTPS_CLAUDE",
    "HTTPS_CODEX",
    "HTTPS_CURSOR",
    "HTTPS_DEVIN",
    "REMOTE_CODEX",
    "Adapter",
    "Caps",
    "ClaudeAdapter",
    "CodexAdapter",
    "CursorAdapter",
    "DevinAdapter",
    "HttpsClaudeAdapter",
    "HttpsCodexAdapter",
    "HttpsCursorAdapter",
    "HttpsDevinAdapter",
    "PullAdapter",
    "RemoteCodexAdapter",
    "TestAgentAdapter",
    "build_adapters",
]
