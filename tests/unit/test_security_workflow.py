"""Security scans report on PRs and main without changing the existing CI gate."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_security_workflow_reports_every_layer_without_upload_or_gate() -> None:
    """Findings must be downloadable while code scanning and CI requirements stay untouched."""
    workflow = (ROOT / ".github/workflows/security.yml").read_text()
    assert "pull_request:" in workflow
    assert "push:" in workflow and "main" in workflow
    assert "security-events: write" not in workflow
    assert "upload-sarif" not in workflow
    assert "upload: never" in workflow
    assert "security-extended" in workflow
    for layer in ("codeql", "bandit", "dependencies", "image", "zizmor", "gitleaks"):
        assert f"  {layer}:" in workflow
    assert workflow.count("actions/upload-artifact@") >= 6
    assert "continue-on-error: true" in workflow
    assert "ci_scope.py" in workflow
    codeql_config = (ROOT / ".github/codeql/codeql-config.yml").read_text()
    assert "web/static/vendor/mermaid" in codeql_config
    aggregate = (ROOT / ".github/workflows/test.yml").read_text().split("\n  ci:\n", 1)[1]
    assert "security" not in aggregate


def test_local_fast_scan_is_named_in_the_workflow_docs() -> None:
    """An agent can run the same quick checks before opening a PR."""
    command = "python scripts/security_scan.py"
    assert (ROOT / "scripts/security_scan.py").is_file()
    for name in ("CLAUDE.md", ".claude/skills/work-on-an-issue/SKILL.md", "CONTRIBUTING.md"):
        assert command in (ROOT / name).read_text()
