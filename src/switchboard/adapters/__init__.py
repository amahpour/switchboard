"""Per-harness adapters (DESIGN.md §9): routing (pure) and transport (async)."""

from __future__ import annotations

from switchboard.adapters.base import Adapter, Caps, PullAdapter
from switchboard.adapters.claude import ClaudeAdapter
from switchboard.adapters.codex import CodexAdapter
from switchboard.adapters.cursor import CursorAdapter
from switchboard.adapters.devin import DevinAdapter
from switchboard.adapters.remote_codex import RemoteCodexAdapter
from switchboard.adapters.testagent import TestAgentAdapter
from switchboard.config import Config


# The adapter of a Codex session on another host (DESIGN.md §27.7); never a harness name.
REMOTE_CODEX = "codex@remote"


def build_adapters(cfg: Config) -> dict[str, Adapter]:
    """Claude (M3) and Codex (M4) have push tiers; Cursor (stop park, M5) and
    Devin (wait loop plus Stop re-arm, M5) are reached through their hooks. A
    Codex session on a remote host is pull only (``REMOTE_CODEX``, M8c)."""
    return {
        "claude": ClaudeAdapter(cfg),
        "codex": CodexAdapter(cfg),
        REMOTE_CODEX: RemoteCodexAdapter(cfg),
        "cursor": CursorAdapter(cfg),
        "devin": DevinAdapter(cfg),
        "unknown": PullAdapter("unknown", cfg),
        "test": TestAgentAdapter(cfg),
    }


__all__ = ["REMOTE_CODEX", "Adapter", "Caps", "ClaudeAdapter", "CodexAdapter", "CursorAdapter", "DevinAdapter",
           "PullAdapter", "RemoteCodexAdapter", "TestAgentAdapter", "build_adapters"]
