"""Lint is a failing part of the aggregate CI check, not a decorative job."""

from __future__ import annotations

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "test.yml"


def test_lint_job_is_in_the_required_ci_aggregate() -> None:
    """A red lint job must make the already-required CI check red on a code PR."""
    workflow = WORKFLOW.read_text()
    assert "\n  lint:\n" in workflow
    aggregate = workflow.split("\n  ci:\n", 1)[1]
    assert "needs: [changes, docs, lint," in aggregate
    assert 'want = ("lint",' in aggregate
