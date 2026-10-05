""".github/scripts/release.py: the release a release PR carries (issues #33, #46)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "release.py"
spec = importlib.util.spec_from_file_location("release_script", SCRIPT)
assert spec and spec.loader
rel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rel)


@pytest.mark.parametrize(
    "title",
    [
        "feat: tab icon (#31)",
        "fix(cli): colour only on a terminal",
        "ci!: drop py3.12",
        "docs: typo",
        "chore(deps): bump uv",
        "release: v0.7.0",
    ],
)
def test_good_titles(title: str) -> None:
    assert rel.check_title(title) is None
    assert rel.main(["--check-title", title]) == 0


@pytest.mark.parametrize(
    "title",
    [
        "Add a tab icon",
        "feat:no space",
        "feature: x",
        "Fix: capital",
        "feat(CLI): x",
        "",
        "release: 0.7.0",
        "release: v0.7",
        "release: next",
    ],
)
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


CHANGELOG = (
    "# Changelog\n\n## Unreleased\n\nIntro.\n\n### Added\n\n- A thing.\n\n## 0.4.0 (2026-09-28)\n\nOld.\n"
)


def test_cut_changelog_gathers_the_notes_above_the_last_release() -> None:
    # an old-style Unreleased section is gathered, and goes
    text, notes = rel.cut_changelog(CHANGELOG, "0.4.1", "2026-09-29", [], ["fix: x"])
    assert notes == "Intro.\n\n### Added\n\n- A thing.\n"
    assert text == (
        "# Changelog\n\n## 0.4.1 (2026-09-29)\n\nIntro.\n\n### Added\n\n- A thing.\n\n"
        "## 0.4.0 (2026-09-28)\n\nOld.\n"
    )
    # notes files (changes/*.md) go in above the last release
    text2, notes2 = rel.cut_changelog(text, "0.4.2", "2026-09-30", ["### Fixed\n\n- B.\n"], ["fix: b (#34)"])
    assert notes2 == "### Fixed\n\n- B.\n"
    assert text2.startswith(
        "# Changelog\n\n## 0.4.2 (2026-09-30)\n\n### Fixed\n\n- B.\n\n## 0.4.1 (2026-09-29)"
    )
    # no notes at all: the commits' titles are the notes
    text3, notes3 = rel.cut_changelog(text2, "0.4.3", "2026-09-30", [], ["docs: typo (#33)"])
    assert notes3 == "- docs: typo (#33)\n"
    assert text3.startswith("# Changelog\n\n## 0.4.3 (2026-09-30)\n\n- docs: typo (#33)\n\n## 0.4.2")
    # a first release goes at the end of the preamble
    first, _ = rel.cut_changelog("# Changelog\n\nIntro.\n", "1.0.0", "d", ["- One.\n"], [])
    assert first == "# Changelog\n\nIntro.\n\n## 1.0.0 (d)\n\n- One.\n"
    with pytest.raises(SystemExit):  # never a second section for the same version
        rel.cut_changelog(text3, "0.4.3", "2026-10-01", [], [])


def test_gather_notes_puts_each_heading_once_in_order() -> None:
    a = "The big one.\n\n### Fixed\n\n- A fix.\n\n### Added\n\n- A feature.\n"
    b = "### Added\n\n- Another feature.\n- And a third.\n\n### Deprecated\n\n- Old flag.\n"
    c = "### Fixed\n\nA paragraph, not a bullet.\n"
    assert rel.gather_notes(["", a, b, "\n", c]) == (
        "The big one.\n\n"
        "### Added\n\n- A feature.\n- Another feature.\n- And a third.\n\n"  # one list across files
        "### Fixed\n\n- A fix.\n\nA paragraph, not a bullet.\n\n"  # HEADING_ORDER first...
        "### Deprecated\n\n- Old flag."
    )  # ...then the rest, as seen
    assert rel.gather_notes(["### Fixed\n\n", "  \n"]) == ""  # empty files and headings add nothing
    assert rel.is_release_file("changes/a.md") and rel.is_release_file("uv.lock")
    assert not rel.is_release_file("changes/a.txt") and not rel.is_release_file("src/switchboard/cli.py")


def test_update_pins() -> None:
    text = "uv tool install git+https://github.com/amahpour/switchboard@v0.4.0   # comment\n"
    assert rel.update_pins(text, "0.4.1") == text.replace("@v0.4.0", "@v0.4.1")
    assert rel.update_pins("git+https://github.com/other/switchboard@v0.4.0", "9.9.9").endswith("@v0.4.0")
    # the image (issue #34): the examples and docs/DEPLOY.md name a release, never :latest
    image = "    image: ghcr.io/amahpour/switchboard:0.4.0   # a release\n"
    assert rel.update_pins(image, "0.5.0") == image.replace(":0.4.0", ":0.5.0")
    for other in (
        "ghcr.io/amahpour/switchboard:latest",
        "ghcr.io/other/switchboard:0.4.0",
        "ghcr.io/amahpour/switchboard:0.4.0-rc1",
        "python:3.13-slim",
    ):
        assert rel.update_pins(other, "0.5.0") == other  # not a release pin of this image


# ------------------------------------------------------------------ a real repo
def git(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(repo),
    }
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env
    ).stdout


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src" / "switchboard").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "switchboard"\nversion = "0.4.0"\n')
    (repo / "src" / "switchboard" / "__init__.py").write_text('"""x"""\n\n__version__ = "0.4.0"\n')
    (repo / "uv.lock").write_text(
        '[[package]]\nname = "other"\nversion = "1.0"\n\n'
        '[[package]]\nname = "switchboard"\nversion = "0.4.0"\nsource = { editable = "." }\n'
    )
    (repo / "CHANGELOG.md").write_text(CHANGELOG)
    pin = "uv tool install git+https://github.com/amahpour/switchboard@v0.4.0\n"
    (repo / "README.md").write_text(pin)
    (repo / "docs" / "INSTALL.md").write_text(pin)
    (repo / "deploy" / "compose").mkdir(parents=True)
    (repo / "deploy" / "compose" / "compose.yaml").write_text(
        "    image: ghcr.io/amahpour/switchboard:0.4.0\n"
    )
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
    assert (
        "@v0.5.0" in (repo / "README.md").read_text() and "@v0.5.0" in (repo / "docs/INSTALL.md").read_text()
    )
    assert ":0.5.0\n" in (repo / "deploy/compose/compose.yaml").read_text()  # the image pin
    assert notes.read_text() == "Intro.\n\n### Added\n\n- A thing.\n"
    assert (repo / "CHANGELOG.md").read_text().startswith("# Changelog\n\n## 0.5.0 (2026-09-29)\n\nIntro.")

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
    git(repo, "checkout", "-q", first)  # ... while this run is for a
    assert rel.release(repo, tmp_path / "n.md", "2026-09-29") is None
    assert "release from origin/main" in capsys.readouterr().out
    assert 'version = "0.4.0"' in (repo / "pyproject.toml").read_text()


def test_a_tag_off_main_is_not_the_last_release(tmp_path: Path) -> None:
    """A release commit whose push to main was refused, but whose tag got out (before the push
    was atomic), must not be the base of the next release."""
    repo = make_repo(tmp_path)
    git(repo, "commit", "-q", "--allow-empty", "-m", "feat: x (#39)")
    base = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "commit", "-q", "--allow-empty", "-m", "release: v0.5.0")
    git(repo, "tag", "v0.5.0")  # the stray tag...
    git(repo, "checkout", "-q", "-B", "main", base)  # ...off main, which moved on without it
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


# ------------------------------------------------------------------ release PRs (#46)
def test_notes_for_reads_a_release_back_out_of_the_changelog() -> None:
    text, notes = rel.cut_changelog(CHANGELOG, "0.4.1", "2026-09-29", [], [])
    assert rel.notes_for(text, "0.4.1") == notes == "Intro.\n\n### Added\n\n- A thing.\n"
    assert rel.notes_for(text, "0.4.0") == "Old.\n"  # the last section runs to the end
    with pytest.raises(SystemExit):
        rel.notes_for(text, "0.4.2")


def test_merged_names_the_release_its_pyproject_carries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path)  # pyproject.toml says 0.4.0
    assert rel.merged_release("release: v0.4.0 (#47)", repo) == "0.4.0"  # the squash commit's title
    assert rel.merged_release("release: v0.4.0", repo) == "0.4.0"
    for other in ("fix: x (#3)", "release: v0.4.0 and more", "chore(release): v0.4.0"):
        assert rel.merged_release(other, repo) is None
    with pytest.raises(SystemExit, match="pyproject.toml says 0.4.0"):
        rel.merged_release("release: v0.4.1 (#47)", repo)  # never tag a version the build won't report
    assert rel.main(["--merged", "release: v0.4.0 (#47)", "--root", str(repo)]) == 0
    assert rel.main(["--merged", "docs: y (#48)", "--root", str(repo)]) == 0
    assert rel.main(["--notes-for", "0.4.0", "--root", str(repo)]) == 0
    assert capsys.readouterr().out == "0.4.0\n\nOld.\n"


FAKE_GH = """#!/bin/sh
echo "$*" >> "$GH_LOG"
case "$1 $2" in
  "pr list") printf '%s\\n' "$GH_OPEN_PRS" ;;
  "pr create") echo "https://github.com/amahpour/switchboard/pull/99" ;;
esac
"""


@pytest.fixture
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """(a checkout, its origin): a release-ready repo whose origin is a local bare repo, and a
    stand-in `gh` on PATH that logs its arguments."""
    repo = make_repo(tmp_path)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "commit", "-q", "--allow-empty", "-m", "feat(cli): colour the framing (#30)")
    git(repo, "push", "-q", "origin", "main", "--tags")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "gh").write_text(FAKE_GH)
    (tmp_path / "bin" / "gh").chmod(0o755)
    for k, v in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GH_LOG": str(tmp_path / "gh.log"),
        "GH_OPEN_PRS": "[]",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin")
    return repo, origin


def test_open_pr_cuts_the_release_on_a_branch_and_leaves_the_checkout_alone(
    remote: tuple[Path, Path],
) -> None:
    repo, origin = remote
    (repo / "changes").mkdir()
    (repo / "changes" / "colour.md").write_text("### Added\n\n- Colour.\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "docs: a note for the colour (#30)")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "switch", "-q", "-c", "my-work")  # the caller's own branch, mid-edit
    (repo / "README.md").write_text("an edit in progress\n")

    assert rel.open_pr(repo, "2026-09-30") == "https://github.com/amahpour/switchboard/pull/99"

    shown = git(origin, "show", "release/v0.5.0:pyproject.toml")
    assert 'version = "0.5.0"' in shown
    assert git(origin, "log", "-1", "--format=%s", "release/v0.5.0").strip() == "release: v0.5.0"
    assert "## 0.5.0 (2026-09-30)" in git(origin, "show", "release/v0.5.0:CHANGELOG.md")
    assert "- Colour." in git(origin, "show", "release/v0.5.0:CHANGELOG.md")
    assert "changes/colour.md" not in git(
        origin, "ls-tree", "-r", "--name-only", "release/v0.5.0"
    )  # gathered
    assert git(origin, "rev-parse", "release/v0.5.0^") == git(origin, "rev-parse", "main")  # from origin/main
    log = (repo.parent / "gh.log").read_text().splitlines()
    assert log[0].startswith("pr list --state open")
    assert log[1].startswith(
        "pr create --base main --head release/v0.5.0 --title release: v0.5.0 --body-file "
    )
    # the caller's checkout: same branch, same edit, no release in it, no worktree left behind
    assert git(repo, "branch", "--show-current").strip() == "my-work"
    assert (repo / "README.md").read_text() == "an edit in progress\n"
    assert 'version = "0.4.0"' in (repo / "pyproject.toml").read_text()
    assert len(git(repo, "worktree", "list").splitlines()) == 1


def test_open_pr_refuses_while_a_release_pr_is_open(
    remote: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, origin = remote
    monkeypatch.setenv(
        "GH_OPEN_PRS",
        json.dumps([{"number": 7, "headRefName": "release/v0.4.1"}, {"number": 8, "headRefName": "fix/x"}]),
    )
    with pytest.raises(SystemExit, match="release PR #7 is still open"):
        rel.open_pr(repo, "2026-09-30")
    assert git(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").split() == ["main"]


def test_open_pr_with_nothing_to_release(
    remote: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, origin = remote
    git(repo, "tag", "v0.5.0")  # the feat is released already
    git(repo, "push", "-q", "origin", "v0.5.0")
    assert rel.main(["--open-pr", "--root", str(repo), "--today", "2026-09-30"]) == 0
    assert capsys.readouterr().out.endswith("nothing to release: no PR opened\n")
    assert git(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").split() == ["main"]
    assert len(git(repo, "worktree", "list").splitlines()) == 1


def test_a_release_gathers_the_notes_files_and_deletes_them(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "changes").mkdir()
    (repo / "changes" / "README.md").write_text("# How to write these\n\n### Fixed\n\n- Not a note.\n")
    (repo / "changes" / "zeta.md").write_text("### Fixed\n\n- Zeta, added first.\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "fix: zeta (#40)")
    (repo / "changes" / "alpha.md").write_text("### Fixed\n\n- Alpha, added second.\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "fix: alpha (#41)")

    assert rel.release(repo, tmp_path / "notes.md", "2026-09-30") == "0.4.1"
    notes = (tmp_path / "notes.md").read_text()
    # the old Unreleased text first, then the files in the order they were added (not by name)
    assert (
        notes
        == "Intro.\n\n### Added\n\n- A thing.\n\n### Fixed\n\n- Zeta, added first.\n- Alpha, added second.\n"
    )
    assert "## 0.4.1 (2026-09-30)\n\n" + notes in (repo / "CHANGELOG.md").read_text()
    assert sorted(p.name for p in (repo / "changes").iterdir()) == ["README.md"]  # gathered, the README stays
