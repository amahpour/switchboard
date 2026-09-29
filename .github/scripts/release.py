"""Cut a release after a merge to main (issue #33; CONTRIBUTING.md, "Releases").

    python3 .github/scripts/release.py --notes release-notes.md    # CI's `release` job
    python3 .github/scripts/release.py --check-title "feat: …"     # CI's `pr-title` check

Every squash merge to main is one Conventional Commits message (the PR's title). After the
tests pass, CI runs this script on that commit. It:
- reads the commits since the last `vX.Y.Z` tag, skipping earlier `release:` commits;
- picks the bump: a minor for `feat:` or a breaking change (`!` or `BREAKING CHANGE`), a patch
  for anything else, so every merge releases. Below 1.0 a breaking change is a minor too;
- writes the new version into pyproject.toml, switchboard/__init__.py and uv.lock;
- turns CHANGELOG.md's "## Unreleased" section into "## X.Y.Z (date)" under a fresh, empty
  "## Unreleased", and writes that section to the notes file. With nothing under Unreleased,
  the notes are the commits' titles;
- points the README's and docs/INSTALL.md's `uv tool install …@vX.Y.Z` lines at the new tag.

The job then commits "release: vX.Y.Z", tags it and publishes the GitHub Release. If main has
moved on since this commit (another merge landed), it releases nothing: that merge's own run
releases both. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TYPES = ("feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert")
TITLE_RE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[a-z0-9._/-]+)\))?(?P<bang>!)?: (?P<subject>\S.*)$")
RELEASE_PREFIX = "release: "
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+)$")
PIN_RE = re.compile(r"(git\+https://github\.com/amahpour/switchboard@)v\d+\.\d+\.\d+")
PIN_FILES = ("README.md", "docs/INSTALL.md")


# ------------------------------------------------------------------ pure parts
def check_title(title: str) -> str | None:
    """None for a good Conventional Commits title, else why it isn't one."""
    m = TITLE_RE.match(title)
    if not m:
        return "use `<type>: <subject>` or `<type>(<scope>): <subject>`, for example `fix(cli): …`"
    if m["type"] not in TYPES:
        return f"`{m['type']}` isn't a known type: {', '.join(TYPES)}"
    return None


def is_breaking(subject: str, body: str) -> bool:
    m = TITLE_RE.match(subject)
    return bool(m and m["bang"]) or "BREAKING CHANGE" in body


def bump_kind(commits: list[tuple[str, str]]) -> str | None:
    """"minor", "patch" or None (nothing to release), from (subject, body) pairs."""
    kind = None
    for subject, body in commits:
        if subject.startswith(RELEASE_PREFIX):
            continue
        m = TITLE_RE.match(subject)
        if is_breaking(subject, body) or (m and m["type"] == "feat"):
            return "minor"
        kind = "patch"
    return kind


def next_version(current: str, kind: str, *, breaking_major: bool = False) -> str:
    major, minor, patch = (int(x) for x in VERSION_RE.match(current).groups())  # type: ignore[union-attr]
    if breaking_major and major >= 1:
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def cut_changelog(text: str, version: str, date: str, fallback: list[str]) -> tuple[str, str]:
    """(the new CHANGELOG, this release's notes). The Unreleased section becomes the release's
    section; a fresh, empty Unreleased goes above it."""
    head = "## Unreleased\n"
    if f"\n## {version} (" in text:
        raise SystemExit(f"CHANGELOG.md already has a {version} section")
    start = text.find(head)
    if start < 0:
        raise SystemExit("CHANGELOG.md has no '## Unreleased' section")
    body_start = start + len(head)
    nxt = text.find("\n## ", body_start)
    end = len(text) if nxt < 0 else nxt + 1
    body = text[body_start:end].strip("\n")
    notes = body if body.strip() else "\n".join(f"- {line}" for line in fallback)
    section = f"## Unreleased\n\n## {version} ({date})\n\n{notes}\n\n"
    return text[:start] + section + text[end:].lstrip("\n"), notes + "\n"


def set_version(root: Path, version: str) -> None:
    """The version in pyproject.toml, switchboard/__init__.py and uv.lock's own entry."""
    edits = (
        ("pyproject.toml", r'(?m)^version = "[^"]+"', f'version = "{version}"'),
        ("src/switchboard/__init__.py", r'(?m)^__version__ = "[^"]+"', f'__version__ = "{version}"'),
        ("uv.lock", r'(name = "switchboard"\nversion = )"[^"]+"', rf'\g<1>"{version}"'),
    )
    for rel, pattern, repl in edits:
        path = root / rel
        text, n = re.subn(pattern, repl, path.read_text(), count=1)
        if n != 1:
            raise SystemExit(f"{rel}: no version to set")
        path.write_text(text)


def update_pins(text: str, version: str) -> str:
    return PIN_RE.sub(rf"\g<1>v{version}", text)


# ------------------------------------------------------------------ git
def git(*args: str, root: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def last_version(root: Path = ROOT) -> str:
    for tag in git("tag", "--list", "v*", "--sort=-v:refname", root=root).split():
        if m := TAG_RE.match(tag):
            return m[1]
    raise SystemExit("no vX.Y.Z tag to release from")


def commits_since(version: str, root: Path = ROOT) -> list[tuple[str, str]]:
    out = git("log", "--format=%s%x1f%b%x1e", f"v{version}..HEAD", root=root)
    pairs = [c.strip("\n").split("\x1f", 1) for c in out.split("\x1e") if c.strip()]
    return [(p[0], p[1] if len(p) > 1 else "") for p in pairs]


def behind_main(root: Path = ROOT) -> bool:
    """True when origin/main has moved past this checkout (another merge landed)."""
    try:
        remote = git("rev-parse", "--verify", "-q", "origin/main", root=root).strip()
    except subprocess.CalledProcessError:
        return False
    return bool(remote) and remote != git("rev-parse", "HEAD", root=root).strip()


def output(**kv: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    lines = "".join(f"{k}={v}\n" for k, v in kv.items())
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(lines)
    print(lines, end="")


def release(root: Path, notes_path: Path, today: str) -> str | None:
    """Apply the next release to the working tree; the new version, or None."""
    if behind_main(root):
        print("main has moved on since this commit: its own run releases both")
        return None
    current = last_version(root)
    commits = commits_since(current, root)
    kind = bump_kind(commits)
    if kind is None:
        print(f"nothing to release since v{current}")
        return None
    breaking = any(is_breaking(s, b) for s, b in commits if not s.startswith(RELEASE_PREFIX))
    version = next_version(current, kind, breaking_major=breaking)
    set_version(root, version)
    fallback = [s for s, _ in commits if not s.startswith(RELEASE_PREFIX)]
    changelog, notes = cut_changelog((root / "CHANGELOG.md").read_text(), version, today, fallback)
    (root / "CHANGELOG.md").write_text(changelog)
    for rel in PIN_FILES:
        p = root / rel
        if p.exists():
            p.write_text(update_pins(p.read_text(), version))
    notes_path.write_text(notes)
    return version


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--notes", help="write this release's notes here and apply the release to the tree")
    ap.add_argument("--check-title", metavar="TITLE", help="check a PR title and exit")
    ap.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    ap.add_argument("--today", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.check_title is not None:
        why = check_title(args.check_title)
        if why:
            print(f"PR title {args.check_title!r}: {why}", file=sys.stderr)
            return 1
        print("PR title ok")
        return 0
    if not args.notes:
        ap.error("--notes or --check-title")
    today = args.today or dt.datetime.now(dt.timezone.utc).date().isoformat()
    version = release(Path(args.root), Path(args.notes), today)
    output(released="true" if version else "false", version=version or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
