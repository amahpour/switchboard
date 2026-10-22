"""One real trial of Claude Code agents, graded (DESIGN.md §12.7). Opt-in: ``SWITCHBOARD_EVAL=claude uv run
pytest -m eval tests/evals -s -q``.

A smoke test of the whole path with real CLIs, like ``tests/live``: three Claude Code sessions in a room of a
throwaway broker, the person asks a question that names nobody, and the trial must be graded (a timeout or a
crash is a failure here). What the agents *did* is not asserted: that is the numbers' job, not a test's
(``run_evals.py``). A few cents.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

_wanted = {x.strip() for x in os.environ.get("SWITCHBOARD_EVAL", "").split(",")}
if not _wanted & {"claude", "all"}:
    pytest.skip("set SWITCHBOARD_EVAL=claude to run the evals against Claude Code", allow_module_level=True)

from evalkit import claude_world, scenario  # noqa: E402
from evalkit.trial import Limits, run_trial  # noqa: E402

_why = claude_world.skip_reason()
if _why:
    pytest.skip(_why, allow_module_level=True)


def test_one_real_trial_is_graded() -> None:
    world = claude_world.ClaudeWorld()
    trial = run_trial(world, scenario.load_all()["nobody-named"], "", Limits())
    print("\nEVAL RESULTS", json.dumps(trial.as_dict(), indent=1))
    assert world.abort_reason is None, world.abort_reason
    assert world.notes["leftover_processes"] == [], "processes of the trial outlived it"
    assert trial.status == "graded", trial.error
