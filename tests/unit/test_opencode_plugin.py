"""Run the OpenCode V2 plugin against a scripted context (#513, DESIGN.md §49.12)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import child_env, node_for_tests

ROOT = Path(__file__).resolve().parents[2]
NODE, pytestmark = node_for_tests()


def test_opencode_plugin_listener_and_session_isolation() -> None:
    """Node drives real plugin code through response codes, races, ownership, approval holds and cleanup."""
    r = subprocess.run(
        [NODE, "--test", str(ROOT / "tests/opencode_plugin_harness.mjs")],
        env=child_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stdout + r.stderr
