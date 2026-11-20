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


def stamped_tree(root: Path, version: str) -> Path:
    """A tree whose every plugin manifest (release.py's PLUGIN_MANIFESTS) says ``version``."""
    for manifest in scope_mod.release.PLUGIN_MANIFESTS.values():
        (root / manifest).parent.mkdir(parents=True, exist_ok=True)
        (root / manifest).write_text(f'{{\n  "name": "switchboard",\n  "version": "{version}"\n}}\n')
    return root


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
        "plugins/claude/meeting/compose.yaml",  # the hosted-call relay's image (#623)
        ".claude-plugin/marketplace.json",  # the plugins' pins move with a release (#462)
        ".agents/plugins/marketplace.json",
        "deploy/helm/switchboard/Chart.yaml",  # the chart's appVersion moves too (#608)
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
    title: str,
    ok: bool,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    files = ["CHANGELOG.md", "src/switchboard/cli.py"]
    assert (scope_mod.changelog_problem(files, title) is None) is ok
    assert scope_mod.changelog_problem(["README.md"], title) is None
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(files) + "\n"))
    root = stamped_tree(tmp_path, "0.7.0")  # a release PR's plugins carry its version
    assert scope_mod.main(["--title", title, "--root", str(root)]) == (0 if ok else 1)
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


def test_a_release_pr_fails_unless_every_plugin_manifest_carries_its_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release PR skips the suite, so its stamps are checked here (#901): v1.11.0 was cut by an
    older release.py that left Devin's and OpenCode's manifests at 1.10.0, and only the next PR's
    tests noticed. Fails if a manifest left at an old version, or missing, passes a release PR."""
    root = stamped_tree(tmp_path, "0.7.0")
    assert scope_mod.release_problem("release: v0.7.0", root) is None
    assert scope_mod.release_problem("fix(web): x", root) is None  # only a release PR is checked
    (root / "plugins/devin/plugin.json").write_text('{"version": "0.6.0"}\n')
    (root / "plugins/opencode/package.json").unlink()
    problem = scope_mod.release_problem("release: v0.7.0", root)
    assert problem and "plugins/devin/plugin.json" in problem and "plugins/opencode/package.json" in problem
    assert "plugins/claude" not in problem  # a stamped one is not named
    # only the top-level version counts. Each of these holds a matching "version": "0.7.0" that a text
    # search finds first (c31bd418's regex passed all three): one nested before a stale top-level one,
    # one inside a JSON array, one in truncated JSON
    (root / "plugins/claude/.claude-plugin/plugin.json").write_text(
        '{"engines": {"version": "0.7.0"}, "version": "0.6.0"}\n'
    )
    (root / "plugins/codex/.codex-plugin/plugin.json").write_text('[{"version": "0.7.0"}]\n')
    (root / "plugins/cursor/.cursor-plugin/plugin.json").write_text('{"version": "0.7.0",\n')
    problem = scope_mod.release_problem("release: v0.7.0", root)
    assert problem and all(
        f in problem
        for f in (
            "plugins/claude/.claude-plugin/plugin.json",
            "plugins/codex/.codex-plugin/plugin.json",
            "plugins/cursor/.cursor-plugin/plugin.json",
        )
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("pyproject.toml\n"))
    assert scope_mod.main(["--title", "release: v0.7.0", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert out.startswith("::error::release v0.7.0: ") and "plugins/devin/plugin.json" in out
