"""Per-harness adapters (DESIGN.md §9): routing (pure) and transport (async)."""

from __future__ import annotations

from switchboard.adapters.base import Adapter, Caps, PullAdapter
from switchboard.adapters.claude import ClaudeAdapter
from switchboard.adapters.codex import CodexAdapter
from switchboard.adapters.cursor import CursorAdapter
from switchboard.adapters.devin import DevinAdapter
from switchboard.adapters.testagent import TestAgentAdapter
from switchboard.config import Config


def build_adapters(cfg: Config) -> dict[str, Adapter]:
    """Claude (M3) and Codex (M4) have push tiers; Cursor (stop park, M5) and
    Devin (wait loop plus Stop re-arm, M5) are reached through their hooks."""
    return {
        "claude": ClaudeAdapter(cfg),
        "codex": CodexAdapter(cfg),
        "cursor": CursorAdapter(cfg),
        "devin": DevinAdapter(cfg),
        "unknown": PullAdapter("unknown", cfg),
        "test": TestAgentAdapter(cfg),
    }


__all__ = ["Adapter", "Caps", "ClaudeAdapter", "CodexAdapter", "CursorAdapter", "DevinAdapter", "PullAdapter",
           "TestAgentAdapter", "build_adapters"]
