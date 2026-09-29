""".github/scripts/release.py (issue #33): every merge to main cuts a release."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "release.py"
spec = importlib.util.spec_from_file_location("release_script", SCRIPT)
assert spec and spec.loader
rel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rel)


@pytest.mark.parametrize("title", ["feat: tab icon (#31)", "fix(cli): colour only on a terminal", "ci!: drop py3.12",
                                   "docs: typo", "chore(deps): bump uv"])
def test_good_titles(title: str) -> None:
    assert rel.check_title(title) is None
    assert rel.main(["--check-title", title]) == 0


@pytest.mark.parametrize("title", ["Add a tab icon", "feat:no space", "feature: x", "Fix: capital", "feat(CLI): x", ""])
def test_bad_titles(title: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert rel.check_title(title)
    assert rel.main(["--check-title", title]) == 1
    assert "PR title" in capsys.readouterr().err


def test_every_merge_is_at_least_a_patch() -> None:
    assert rel.bump_kind([("docs: typo", "")]) == "patch"
    assert rel.bump_kind([("not conventional at all", "")]) == "patch"
    assert rel.bump_kind([("fix: x", ""), ("feat(web): y", "")]) == "minor"
    assert rel.bump_kind([("fix!: drop an option", "")]) == "minor"
    assert rel.bump_kind([("refactor: z", "BREAKING CHANGE: the old flag is gone")]) == "minor"
    assert rel.bump_kind([("release: v0.4.1", "")]) is None  # the release commit itself
    assert rel.bump_kind([]) is None


def test_next_version() -> None:
    assert rel.next_version("0.4.0", "patch") == "0.4.1"
    assert rel.next_version("0.4.9", "minor") == "0.5.0"
    assert rel.next_version("0.4.0", "minor", breaking_major=True) == "0.5.0"  # below 1.0: a minor
    assert rel.next_version("1.2.3", "minor", breaking_major=True) == "2.0.0"


CHANGELOG = "# Changelog\n\n## Unreleased\n\nIntro.\n\n### Added\n\n- A thing.\n\n## 0.4.0 (2026-09-28)\n\nOld.\n"


def test_cut_changelog_moves_unreleased_under_the_version() -> None:
    text, notes = rel.cut_changelog(CHANGELOG, "0.4.1", "2026-09-29", ["fix: x"])
    assert notes == "Intro.\n\n### Added\n\n- A thing.\n"
    assert text == ("# Changelog\n\n## Unreleased\n\n## 0.4.1 (2026-09-29)\n\nIntro.\n\n### Added\n\n- A thing.\n\n"
                    "## 0.4.0 (2026-09-28)\n\nOld.\n")
    # nothing written under Unreleased: the commits' titles are the notes
    text2, notes2 = rel.cut_changelog(text, "0.4.2", "2026-09-30", ["docs: typo (#33)"])
    assert notes2 == "- docs: typo (#33)\n"
    assert text2.startswith("# Changelog\n\n## Unreleased\n\n## 0.4.2 (2026-09-30)\n\n- docs: typo (#33)\n\n## 0.4.1")
    with pytest.raises(SystemExit):
        rel.cut_changelog("# Changelog\n", "1.0.0", "d", [])
    with pytest.raises(SystemExit):  # never a second section for the same version
        rel.cut_changelog(text2, "0.4.2", "2026-10-01", [])


def test_update_pins() -> None:
    text = "uv tool install git+https://github.com/amahpour/switchboard@v0.4.0   # comment\n"
    assert rel.update_pins(text, "0.4.1") == text.replace("@v0.4.0", "@v0.4.1")
    assert rel.update_pins("git+https://github.com/other/switchboard@v0.4.0", "9.9.9").endswith("@v0.4.0")
    # the image (issue #34): the examples and docs/DEPLOY.md name a release, never :latest
    image = "    image: ghcr.io/amahpour/switchboard:0.4.0   # a release\n"
    assert rel.update_pins(image, "0.5.0") == image.replace(":0.4.0", ":0.5.0")
    for other in ("ghcr.io/amahpour/switchboard:latest", "ghcr.io/other/switchboard:0.4.0",
                  "ghcr.io/amahpour/switchboard:0.4.0-rc1", "python:3.13-slim"):
        assert rel.update_pins(other, "0.5.0") == other  # not a release pin of this image


# ------------------------------------------------------------------ a real repo
def git(repo: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
           "HOME": str(repo)}
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env).stdout


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src" / "switchboard").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "switchboard"\nversion = "0.4.0"\n')
    (repo / "src" / "switchboard" / "__init__.py").write_text('"""x"""\n\n__version__ = "0.4.0"\n')
    (repo / "uv.lock").write_text('[[package]]\nname = "other"\nversion = "1.0"\n\n'
                                  '[[package]]\nname = "switchboard"\nversion = "0.4.0"\nsource = { editable = "." }\n')
    (repo / "CHANGELOG.md").write_text(CHANGELOG)
    pin = "uv tool install git+https://github.com/amahpour/switchboard@v0.4.0\n"
    (repo / "README.md").write_text(pin)
    (repo / "docs" / "INSTALL.md").write_text(pin)
    (repo / "deploy" / "compose").mkdir(parents=True)
    (repo / "deploy" / "compose" / "compose.yaml").write_text("    image: ghcr.io/amahpour/switchboard:0.4.0\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "chore: start")
    git(repo, "tag", "v0.4.0")
    return repo


def test_a_release_on_a_real_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    out = tmp_path / "gh-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    notes = tmp_path / "notes.md"
    git(repo, "commit", "-q", "--allow-empty", "-m", "feat(cli): colour the framing (#30)")

    assert rel.main(["--notes", str(notes), "--root", str(repo), "--today", "2026-09-29"]) == 0
    assert out.read_text() == "released=true\nversion=0.5.0\n"
    assert 'version = "0.5.0"' in (repo / "pyproject.toml").read_text()
    assert '__version__ = "0.5.0"' in (repo / "src/switchboard/__init__.py").read_text()
    lock = (repo / "uv.lock").read_text()
    assert 'name = "switchboard"\nversion = "0.5.0"' in lock and 'name = "other"\nversion = "1.0"' in lock
    assert "@v0.5.0" in (repo / "README.md").read_text() and "@v0.5.0" in (repo / "docs/INSTALL.md").read_text()
    assert ":0.5.0\n" in (repo / "deploy/compose/compose.yaml").read_text()  # the image pin
    assert notes.read_text() == "Intro.\n\n### Added\n\n- A thing.\n"
    assert "## Unreleased\n\n## 0.5.0 (2026-09-29)\n" in (repo / "CHANGELOG.md").read_text()

    # what CI does next: commit and tag; then a docs-only merge with nothing under Unreleased
    git(repo, "commit", "-qam", "release: v0.5.0")
    git(repo, "tag", "v0.5.0")
    out.write_text("")
    assert rel.main(["--notes", str(notes), "--root", str(repo), "--today", "2026-09-30"]) == 0
    assert out.read_text() == "released=false\nversion=\n"  # only the release commit since v0.5.0
    git(repo, "commit", "-q", "--allow-empty", "-m", "docs: fix a typo (#31)")
    out.write_text("")
    assert rel.main(["--notes", str(notes), "--root", str(repo), "--today", "2026-09-30"]) == 0
    assert out.read_text() == "released=true\nversion=0.5.1\n"
    assert notes.read_text() == "- docs: fix a typo (#31)\n"


def test_nothing_is_released_when_main_moved_on(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = make_repo(tmp_path)
    git(repo, "commit", "-q", "--allow-empty", "-m", "fix: a (#1)")
    first = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "commit", "-q", "--allow-empty", "-m", "fix: b (#2)")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")  # main is at b ...
    git(repo, "checkout", "-q", first)                           # ... while this run is for a
    assert rel.release(repo, tmp_path / "n.md", "2026-09-29") is None
    assert "its own run releases both" in capsys.readouterr().out
    assert 'version = "0.4.0"' in (repo / "pyproject.toml").read_text()


def test_a_tag_off_main_is_not_the_last_release(tmp_path: Path) -> None:
    """A release commit whose push to main was refused, but whose tag got out (before the push
    was atomic), must not be the base of the next release."""
    repo = make_repo(tmp_path)
    git(repo, "commit", "-q", "--allow-empty", "-m", "feat: x (#39)")
    base = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "commit", "-q", "--allow-empty", "-m", "release: v0.5.0")
    git(repo, "tag", "v0.5.0")                           # the stray tag...
    git(repo, "checkout", "-q", "-B", "main", base)      # ...off main, which moved on without it
    git(repo, "commit", "-q", "--allow-empty", "-m", "docs: y (#38)")
    assert rel.last_version(repo) == "0.4.0"
    assert [s for s, _ in rel.commits_since("0.4.0", repo)] == ["docs: y (#38)", "feat: x (#39)"]


def test_a_missing_tag_or_version_stops_the_release(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    git(repo, "tag", "-d", "v0.4.0")
    with pytest.raises(SystemExit):
        rel.last_version(repo)
    (repo / "pyproject.toml").write_text("[project]\n")
    with pytest.raises(SystemExit):
        rel.set_version(repo, "1.0.0")


def test_the_script_needs_an_action() -> None:
    with pytest.raises(SystemExit):
        rel.main([])
