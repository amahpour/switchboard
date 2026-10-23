"""Per-harness adapters (DESIGN.md §9): routing (pure) of sessions over HTTPS (§49)."""

from __future__ import annotations

from switchboard.adapters.base import Adapter, Caps, PullAdapter
from switchboard.adapters.https import (
    HttpsClaudeAdapter,
    HttpsCodexAdapter,
    HttpsCursorAdapter,
    HttpsDevinAdapter,
    HttpsOpenCodeAdapter,
)
from switchboard.adapters.testagent import TestAgentAdapter
from switchboard.config import Config

# The adapters of sessions over HTTPS (§49.3); never harness names.
HTTPS_CLAUDE = "claude@https"
HTTPS_CODEX = "codex@https"
HTTPS_CURSOR = "cursor@https"
HTTPS_DEVIN = "devin@https"
HTTPS_OPENCODE = "opencode@https"


def build_adapters(cfg: Config) -> dict[str, Adapter]:
    """A session over HTTPS is woken by its own Stop hook (Claude Code, Codex, Cursor: §49.4) or its
    wait loop (Devin, §9.5). ``unknown`` serves a session of no harness, ``test`` the scripted test
    agent of a test-mode broker."""
    return {
        HTTPS_CLAUDE: HttpsClaudeAdapter(cfg),
        HTTPS_CODEX: HttpsCodexAdapter(cfg),
        HTTPS_CURSOR: HttpsCursorAdapter(cfg),
        HTTPS_DEVIN: HttpsDevinAdapter(cfg),
        HTTPS_OPENCODE: HttpsOpenCodeAdapter(cfg),
        "unknown": PullAdapter("unknown", cfg),
        "test": TestAgentAdapter(cfg),
    }


def adapter_for(adapters: dict[str, Adapter], harness: str) -> Adapter:
    """The adapter of a ``harness`` session (every one is over HTTPS, §49.3): its harness's ``@https``
    adapter where there is one; the scripted test agent, which has none, its own; any other harness the
    ``unknown`` one."""
    return adapters.get(f"{harness}@https") or adapters.get(harness) or adapters["unknown"]


__all__ = [
    "HTTPS_CLAUDE",
    "HTTPS_CODEX",
    "HTTPS_CURSOR",
    "HTTPS_DEVIN",
    "HTTPS_OPENCODE",
    "Adapter",
    "Caps",
    "HttpsClaudeAdapter",
    "HttpsCodexAdapter",
    "HttpsCursorAdapter",
    "HttpsDevinAdapter",
    "HttpsOpenCodeAdapter",
    "PullAdapter",
    "TestAgentAdapter",
    "adapter_for",
    "build_adapters",
]
