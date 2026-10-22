"""Live Codex over HTTPS (DESIGN.md §12.4). Opt-in: ``SWITCHBOARD_LIVE=codex uv run pytest -m live``.

One real ``codex`` session (the cheapest model, low effort) in a private tmux server, on a **temp
CODEX_HOME** that holds a copy of the login and the plugin from this checkout, against a throwaway broker
in a temp home. The scenarios are ``harness/scenarios.py``'s, in order. The copy of the login is deleted
when the run ends, and the real ``~/.codex`` must not change.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from harness import scenarios
from harness.codex import CodexRun
from harness.world import skip_reason

pytestmark = pytest.mark.live

_why = skip_reason("codex", "codex")
if _why:
    pytest.skip(_why, allow_module_level=True)


@pytest.fixture(scope="module")
def run() -> Iterator[CodexRun]:
    r = CodexRun("codex-1")
    try:
        r.start()
        yield r
    finally:
        r.stop()
    assert r.results["drift_fail"] == [], f"the user's own Codex config changed: {r.results['drift_fail']}"
    assert r.results["leftover_processes"] == [], "processes of the run outlived it"
    assert not (r.codex_home / "auth.json").exists(), "the copy of the login was not deleted"


def test_1_join_and_say(run: CodexRun) -> None:
    scenarios.join_and_say(run)


def test_2_woken_while_idle_by_a_mention(run: CodexRun) -> None:
    scenarios.idle_wake(run)


def test_3_a_mention_mid_task_arrives_after_the_next_tool_call(run: CodexRun) -> None:
    scenarios.mid_task(run)


def test_4_a_subagent_does_not_take_the_sessions_message(run: CodexRun) -> None:
    scenarios.subagent(run)


def test_5_quit_shows_offline(run: CodexRun) -> None:
    scenarios.quit_offline(run)


def test_6_resume_comes_back_and_is_woken(run: CodexRun) -> None:
    scenarios.resume(run)


def test_7_a_revoked_token_ends_it(run: CodexRun) -> None:
    scenarios.revoked_token(run)
