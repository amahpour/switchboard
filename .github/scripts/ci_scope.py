"""Which pull requests need the test suite (issue #46; CONTRIBUTING.md, "CI").

    git diff --name-only HEAD^1 HEAD | python3 .github/scripts/ci_scope.py --title "$TITLE"

Reads the PR's changed files on stdin, one per line, and prints (and appends to $GITHUB_OUTPUT)
`tests=true` or `tests=false`. The suite is skipped for:
- a docs-only PR: every changed file is Markdown outside src/ and tests/, or an image or a video
  under docs/;
- a release PR (`release.py --open-pr`): titled `release: vX.Y.Z`, changing only the files a
  release writes, and the notes files in changes/ it gathers (release.py's is_release_file).
Anything else runs it, and so does an empty list. test.yml's `tree scan` job still runs for the
two skipped kinds.

It fails a release PR whose plugin manifests (release.py's PLUGIN_MANIFESTS) don't all carry the
title's version: the suite that would catch it is skipped (#901). It also fails a PR that edits
CHANGELOG.md: only a release writes it. A PR's notes go in
changes/<name>.md (changes/README.md), which never conflict. A title scoped `(changelog)` may
fix the notes of a release that's out. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import release  # noqa: E402  (release.py, next to this file)

MEDIA = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".mp4", ".webm")


def is_docs(path: str) -> bool:
    p = PurePosixPath(path)
    if p.parts[:1] in (("src",), ("tests",)):
        return False  # Markdown there can be code's data, or a test's
    return p.suffix == ".md" or (p.parts[:1] == ("docs",) and p.suffix.lower() in MEDIA)


def scope(files: list[str], title: str) -> tuple[bool, str]:
    """(whether the tests run, why)."""
    if not files:
        return True, "no changed files listed"
    if all(is_docs(f) for f in files):
        return False, "docs only"
    if release.RELEASE_TITLE_RE.match(title) and all(release.is_release_file(f) for f in files):
        return False, "a release PR"
    code = [f for f in files if not is_docs(f)]
    return True, f"{len(code)} changed file(s) beyond the docs, such as {code[0]}"


def changelog_problem(files: list[str], title: str) -> str | None:
    """Why this PR mustn't change CHANGELOG.md, or None."""
    if "CHANGELOG.md" not in files or release.RELEASE_TITLE_RE.match(title):
        return None
    m = release.TITLE_RE.match(title)
    if m and m["scope"] == "changelog":
        return None  # fixing a release's notes after it's out, on purpose
    return (
        "only a release writes CHANGELOG.md: put this PR's notes in changes/<name>.md instead "
        "(changes/README.md). To fix notes that are out already, scope the title `(changelog)`."
    )


def _top_level_version(path: Path) -> str | None:
    """A manifest's own top-level ``version``, or None when the file is missing, isn't JSON, isn't an
    object, or has no string version there. A ``version`` nested deeper never counts."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    found = data.get("version") if isinstance(data, dict) else None
    return found if isinstance(found, str) else None


def release_problem(title: str, root: Path) -> str | None:
    """Why a release PR's plugin manifests don't carry its version, or None (#901). A release PR skips
    the suite, so this is the check that still runs: v1.11.0 was cut by an older release.py, which
    left Devin's and OpenCode's manifests at 1.10.0, and nothing caught it until the next PR's tests."""
    m = release.RELEASE_TITLE_RE.match(title)
    if not m:
        return None
    version = m["version"]
    stale = []
    for manifest in release.PLUGIN_MANIFESTS.values():
        if _top_level_version(root / manifest) != version:
            stale.append(manifest)
    if not stale:
        return None
    return (
        f"release v{version}: {', '.join(stale)} not stamped {version}. Cut the release again with "
        'origin/main\'s own release.py (CONTRIBUTING.md, "Cutting a release")'
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--title", default="", help="the PR's title")
    ap.add_argument("--root", default=str(release.ROOT), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    files = [line.strip() for line in sys.stdin if line.strip()]
    if problem := changelog_problem(files, args.title):
        print(f"::error file=CHANGELOG.md::{problem}")
        return 1
    if problem := release_problem(args.title, Path(args.root)):
        print(f"::error::{problem}")
        return 1
    tests, why = scope(files, args.title)
    print(f"{why}: {'running' if tests else 'skipping'} the tests")
    release.output(tests="true" if tests else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
