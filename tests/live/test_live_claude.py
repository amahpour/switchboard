"""Live Claude Code over HTTPS (DESIGN.md §12.4). Opt-in: ``SWITCHBOARD_LIVE=claude uv run pytest -m live``.

One real ``claude --model haiku`` session in a private tmux server with the plugin from this checkout
(``--plugin-dir plugins/claude``), a throwaway broker in a temp home and a token made through the web API.
The scenarios are ``harness/scenarios.py``'s, in order; the first leaves the session joined, and each of
the others starts where the one before left it. The real ``~/.claude/settings.json`` must not change.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from harness import scenarios
from harness.claude import ClaudeRun
from harness.world import skip_reason

pytestmark = pytest.mark.live

_why = skip_reason("claude", "claude")
if _why:
    pytest.skip(_why, allow_module_level=True)


@pytest.fixture(scope="module")
def run() -> Iterator[ClaudeRun]:
    r = ClaudeRun("claude-1")
    try:
        r.start()
        yield r
    finally:
        r.stop()
    assert r.results["drift_fail"] == [], f"the user's own Claude config changed: {r.results['drift_fail']}"
    assert r.results["leftover_processes"] == [], "processes of the run outlived it"


def test_1_join_and_say(run: ClaudeRun) -> None:
    scenarios.join_and_say(run)


def test_2_woken_while_idle_by_a_mention(run: ClaudeRun) -> None:
    scenarios.idle_wake(run)


def test_3_a_mention_mid_task_arrives_after_the_next_tool_call(run: ClaudeRun) -> None:
    scenarios.mid_task(run)


def test_4_a_subagent_does_not_take_the_sessions_message(run: ClaudeRun) -> None:
    scenarios.subagent(run)


def test_5_quit_shows_offline(run: ClaudeRun) -> None:
    scenarios.quit_offline(run)


def test_6_resume_comes_back_and_is_woken_with_nothing_typed(run: ClaudeRun) -> None:
    scenarios.resume(run)


def test_7_a_revoked_token_ends_it(run: ClaudeRun) -> None:
    scenarios.revoked_token(run)
