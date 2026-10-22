"""Live Devin over HTTPS (DESIGN.md §12.4). Opt-in: ``SWITCHBOARD_LIVE=devin uv run pytest -m live``.

One real ``devin`` session in a private tmux server, with the project config from this checkout's
``plugins/devin`` and the user's own Devin config out of the way (an empty ``XDG_CONFIG_HOME``), against a
throwaway broker in a temp home. The scenarios are ``harness/scenarios.py``'s, in order; Devin has no
subagent scenario (its background ``run_subagent`` is handled by taint, §9.5, and tested without a model).
The real ``~/.config/devin`` must not change.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from harness import scenarios
from harness.devin import DevinRun
from harness.world import skip_reason

pytestmark = pytest.mark.live

_why = skip_reason("devin", "devin")
if _why:
    pytest.skip(_why, allow_module_level=True)


@pytest.fixture(scope="module")
def run() -> Iterator[DevinRun]:
    r = DevinRun("devin-1")
    try:
        r.start()
        yield r
    finally:
        r.stop()
    assert r.results["drift_fail"] == [], f"the user's own Devin config changed: {r.results['drift_fail']}"
    assert r.results["leftover_processes"] == [], "processes of the run outlived it"


def test_1_join_and_say(run: DevinRun) -> None:
    scenarios.join_and_say(run)


def test_2_woken_while_idle_by_a_mention(run: DevinRun) -> None:
    scenarios.idle_wake(run)


def test_3_a_mention_mid_task_arrives_after_the_next_tool_call(run: DevinRun) -> None:
    scenarios.mid_task(run)


def test_4_quit_shows_offline(run: DevinRun) -> None:
    scenarios.quit_offline(run)


def test_5_resume_comes_back_and_is_woken(run: DevinRun) -> None:
    scenarios.resume(run)


def test_6_a_killed_devin_shows_offline_and_a_resumed_one_comes_back(run: DevinRun) -> None:
    scenarios.killed_shows_offline(run)


def test_7_a_revoked_token_ends_it(run: DevinRun) -> None:
    scenarios.revoked_token(run)
