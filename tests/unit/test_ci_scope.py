""".github/scripts/ci_scope.py (issue #46): which pull requests skip the test suite."""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / ".github" / "scripts"
spec = importlib.util.spec_from_file_location("ci_scope", SCRIPTS / "ci_scope.py")
assert spec and spec.loader
scope_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope_mod)


def runs_tests(files: list[str], title: str = "docs: x") -> bool:
    return scope_mod.scope(files, title)[0]


def test_a_docs_only_pr_skips_the_tests() -> None:
    assert not runs_tests(
        ["README.md", "docs/USAGE.md", "CLAUDE.md", "docs/media/ui/light.png", "docs/media/demo.mp4"]
    )


@pytest.mark.parametrize(
    "files",
    [
        ["README.md", "src/switchboard/cli.py"],  # docs and code
        ["src/switchboard/web/static/help.md"],  # Markdown the code ships
        ["tests/fixtures/db/README.md"],  # Markdown beside the tests
        ["docs/media/ui_shots.py"],  # a script under docs/ that CI runs
        ["docs/media/cast.json"],  # data under docs/, not an image
        [".github/workflows/test.yml"],
        [],  # nothing listed: run everything
    ],
)
def test_anything_else_runs_them(files: list[str]) -> None:
    assert runs_tests(files)


def test_a_release_pr_skips_the_tests() -> None:
    release_files = [
        "pyproject.toml",
        "src/switchboard/__init__.py",
        "uv.lock",
        "CHANGELOG.md",
        "README.md",
        "docs/INSTALL.md",
        "docs/DEPLOY.md",
        "deploy/compose/compose.yaml",
    ]
    assert not runs_tests(release_files, "release: v0.7.0")
    assert runs_tests(release_files, "chore: bump the version")  # the title makes it a release PR
    assert runs_tests([*release_files, "src/switchboard/cli.py"], "release: v0.7.0")  # and nothing else in it
    assert runs_tests(release_files, "release: v0.7.0 and a fix")
    # the notes files it gathers (and deletes) belong to a release PR too
    assert not runs_tests([*release_files, "changes/sidebar-names.md", "changes/ci.md"], "release: v0.7.0")
    assert not runs_tests(["changes/sidebar-names.md"], "fix(web): x")  # a note alone is docs


@pytest.mark.parametrize(
    ("title", "ok"),
    [
        ("fix(web): keep names visible", False),  # a PR's notes go in changes/<name>.md
        ("release: v0.7.0", True),  # a release writes it
        ("docs(changelog): fix 0.6.2's notes", True),  # fixing notes that are out, on purpose
        ("docs: typo", False),
    ],
)
def test_only_a_release_edits_the_changelog(
    title: str, ok: bool, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    files = ["CHANGELOG.md", "src/switchboard/cli.py"]
    assert (scope_mod.changelog_problem(files, title) is None) is ok
    assert scope_mod.changelog_problem(["README.md"], title) is None
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(files) + "\n"))
    assert scope_mod.main(["--title", title]) == (0 if ok else 1)
    if not ok:
        assert "::error file=CHANGELOG.md::" in capsys.readouterr().out


def test_the_cli_writes_the_decision_for_the_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "gh-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(sys, "stdin", io.StringIO("README.md\ndocs/USAGE.md\n\n"))
    assert scope_mod.main(["--title", "docs: typo"]) == 0
    assert out.read_text() == "tests=false\n"
    assert "docs only: skipping the tests" in capsys.readouterr().out
    monkeypatch.setattr(sys, "stdin", io.StringIO("src/switchboard/cli.py\n"))
    assert scope_mod.main([]) == 0
    assert out.read_text() == "tests=false\ntests=true\n"
