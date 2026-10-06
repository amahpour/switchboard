"""Cut a release through a release PR (issue #46; CONTRIBUTING.md, "Releases").

    python3 .github/scripts/release.py --open-pr                    # cut a release: open its PR
    python3 .github/scripts/release.py --check-title "feat: …"      # CI's `pr-title` check
    python3 .github/scripts/release.py --merged "release: v1.2.3 (#9)"   # release.yml: the version
    python3 .github/scripts/release.py --notes-for 1.2.3             # release.yml: its notes

`main` takes only pull requests, so a release is a PR too. `--open-pr` works on a fresh branch
from origin/main, in a temporary worktree (the checkout it runs from is never touched). It:
- reads the commits since the last `vX.Y.Z` tag, skipping earlier `release:` commits. Every
  squash merge is one Conventional Commits message (the PR's title);
- picks the bump: a minor for `feat:` or a breaking change (`!` or `BREAKING CHANGE`), a patch
  for anything else. Below 1.0 a breaking change is a minor too;
- writes the new version into pyproject.toml, switchboard/__init__.py and uv.lock;
- gathers the notes files in changes/ (one per PR, changes/README.md) into a "## X.Y.Z (date)"
  section of CHANGELOG.md, each heading once, and deletes them. An old-style "## Unreleased"
  section is gathered too. With no notes at all, the notes are the commits' titles;
- points the README's and docs/INSTALL.md's `uv tool install …@vX.Y.Z` lines at the new tag,
  and the deployment examples' and docs/DEPLOY.md's `ghcr.io/amahpour/switchboard:X.Y.Z` at
  the new image;
- commits that as "release: vX.Y.Z" on `release/vX.Y.Z`, pushes it and opens the PR (`gh`).

Merging the PR releases it: .github/workflows/release.yml tags the merge, publishes the GitHub
Release with the version's CHANGELOG section, then the image. `--notes FILE` applies the next
release to the tree it runs in, without a PR. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TYPES = ("feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert")
TITLE_RE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[a-z0-9._/-]+)\))?(?P<bang>!)?: (?P<subject>\S.*)$")
RELEASE_PREFIX = "release: "
RELEASE_TITLE_RE = re.compile(r"^release: v(?P<version>\d+\.\d+\.\d+)$")       # a release PR's title
MERGED_RELEASE_RE = re.compile(r"^release: v(?P<version>\d+\.\d+\.\d+)(?: \(#\d+\))?$")  # its squash commit
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+)$")
PIN_RE = re.compile(r"(git\+https://github\.com/amahpour/switchboard@)v\d+\.\d+\.\d+")
IMAGE_PIN_RE = re.compile(r"(ghcr\.io/amahpour/switchboard:)\d+\.\d+\.\d+(?![\w.-])")
VERSION_FILES = ("pyproject.toml", "src/switchboard/__init__.py", "uv.lock")
PIN_FILES = ("README.md", "docs/INSTALL.md", "docs/DEPLOY.md", "deploy/compose/compose.yaml",
             "deploy/kubernetes/switchboard.yaml", "deploy/render/render.yaml")
RELEASE_FILES = (*VERSION_FILES, "CHANGELOG.md", *PIN_FILES)  # with the notes files, all a release PR changes
CHANGES_DIR = "changes"   # a PR's notes: changes/<name>.md (changes/README.md)
# a release's headings in this order; any other heading follows them, in the order first seen
HEADING_ORDER = ("Upgrading", "Added", "Changed", "Removed", "Fixed", "Known limitations", "Not included")


def is_release_file(path: str) -> bool:
    """A file a release PR may change: the version, CHANGELOG.md, the pins, and the notes files
    it gathers (and deletes)."""
    return path in RELEASE_FILES or (path.startswith(f"{CHANGES_DIR}/") and path.endswith(".md"))


# ------------------------------------------------------------------ pure parts
def check_title(title: str) -> str | None:
    """None for a good Conventional Commits title (or a release PR's), else why it isn't one."""
    if RELEASE_TITLE_RE.match(title):
        return None
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


def split_notes(text: str) -> tuple[str, list[tuple[str, str]]]:
    """(the text above the first `### ` heading, [(heading, the text under it), ...])."""
    parts = re.split(r"(?m)^### +(.+?)[ \t]*\n", text if text.endswith("\n") else text + "\n")
    rest = parts[1:]
    return parts[0].strip("\n"), [(rest[i].strip(), rest[i + 1].strip("\n")) for i in range(0, len(rest), 2)]


def join_md(chunks: list[str]) -> str:
    """Markdown chunks one after another; bullets from several files make one list."""
    out = ""
    for chunk in (c.strip("\n") for c in chunks):
        if not chunk.strip():
            continue
        if out:
            one_list = chunk.startswith("- ") and out.rsplit("\n", 1)[-1].startswith(("- ", "  "))
            out += "\n" if one_list else "\n\n"
        out += chunk
    return out


def gather_notes(texts: list[str]) -> str:
    """One release's notes from its notes files: the texts above their first heading, then each
    heading once (HEADING_ORDER first) with every file's text under it, in the files' order."""
    intros: list[str] = []
    sections: dict[str, list[str]] = {}
    for text in texts:
        intro, secs = split_notes(text)
        intros.append(intro)
        for heading, body in secs:
            sections.setdefault(heading, []).append(body)
    order = [h for h in HEADING_ORDER if h in sections] + [h for h in sections if h not in HEADING_ORDER]
    parts = [join_md(intros)] + [f"### {h}\n\n{body}" for h in order if (body := join_md(sections[h]))]
    return "\n\n".join(p for p in parts if p)


def cut_changelog(text: str, version: str, date: str, notes_files: list[str],
                  fallback: list[str]) -> tuple[str, str]:
    """(the new CHANGELOG, this release's notes). The notes files' texts, and an old-style
    "## Unreleased" section's (which goes), become "## X.Y.Z (date)" above the last release."""
    if f"\n## {version} (" in text:
        raise SystemExit(f"CHANGELOG.md already has a {version} section")
    unreleased = ""
    if m := re.search(r"(?m)^## Unreleased[ \t]*\n", text):   # the one shared section, before changes/
        nxt = text.find("\n## ", m.end() - 1)
        end = len(text) if nxt < 0 else nxt + 1
        unreleased, text = text[m.end():end], text[:m.start()] + text[end:]
    notes = gather_notes([unreleased, *notes_files]) or "\n".join(f"- {line}" for line in fallback)
    section = f"## {version} ({date})\n\n{notes}\n\n"
    if first := re.search(r"(?m)^## ", text):
        return text[:first.start()] + section + text[first.start():], notes + "\n"
    return text.rstrip("\n") + "\n\n" + section.rstrip("\n") + "\n", notes + "\n"


def notes_for(text: str, version: str) -> str:
    """A release's section of the CHANGELOG: its GitHub Release's notes."""
    m = re.search(rf"(?m)^## {re.escape(version)} \([^)\n]*\)\n", text)
    if not m:
        raise SystemExit(f"CHANGELOG.md has no {version} section")
    nxt = text.find("\n## ", m.end())
    return text[m.end():len(text) if nxt < 0 else nxt].strip("\n") + "\n"


def merged_release(title: str, root: Path) -> str | None:
    """The version a merged release PR releases, from its squash commit's title
    ("release: vX.Y.Z (#N)"), or None for any other merge. The title must agree with the
    version in pyproject.toml, which is what the build reports."""
    m = MERGED_RELEASE_RE.match(title.strip())
    if not m:
        return None
    have = re.search(r'(?m)^version = "([^"]+)"', (root / "pyproject.toml").read_text())
    if not have or have[1] != m["version"]:
        raise SystemExit(f"{title!r} releases {m['version']}, "
                         f"but pyproject.toml says {have[1] if have else 'nothing'}")
    return m["version"]


def set_version(root: Path, version: str) -> None:
    """The version in pyproject.toml, switchboard/__init__.py and uv.lock's own entry."""
    patterns = (
        r'(?m)^version = "[^"]+"',
        r'(?m)^__version__ = "[^"]+"',
        r'(name = "switchboard-chat"\nversion = )"[^"]+"',
    )
    repls = (f'version = "{version}"', f'__version__ = "{version}"', rf'\g<1>"{version}"')
    for rel, pattern, repl in zip(VERSION_FILES, patterns, repls, strict=True):
        path = root / rel
        text, n = re.subn(pattern, repl, path.read_text(), count=1)
        if n != 1:
            raise SystemExit(f"{rel}: no version to set")
        path.write_text(text)


def update_pins(text: str, version: str) -> str:
    """The install pins (``…switchboard@vX.Y.Z``) and the image pins (``…/switchboard:X.Y.Z``)."""
    return IMAGE_PIN_RE.sub(rf"\g<1>{version}", PIN_RE.sub(rf"\g<1>v{version}", text))


# ------------------------------------------------------------------ git
def git(*args: str, root: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def gh(*args: str, root: Path = ROOT) -> str:
    return subprocess.run(["gh", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def last_version(root: Path = ROOT) -> str:
    """The newest vX.Y.Z tag on this commit's history. A tag elsewhere (a release commit that
    never reached main) is never the base of the next version."""
    for tag in git("tag", "--list", "v*", "--merged", "HEAD", "--sort=-v:refname", root=root).split():
        if m := TAG_RE.match(tag):
            return m[1]
    raise SystemExit("no vX.Y.Z tag to release from")


def commits_since(version: str, root: Path = ROOT) -> list[tuple[str, str]]:
    out = git("log", "--format=%s%x1f%b%x1e", f"v{version}..HEAD", root=root)
    pairs = [c.strip("\n").split("\x1f", 1) for c in out.split("\x1e") if c.strip()]
    return [(p[0], p[1] if len(p) > 1 else "") for p in pairs]


def notes_files(root: Path = ROOT) -> list[Path]:
    """changes/*.md but its README, in the order they were added (for a name used again, its
    latest addition); files git doesn't know yet come last, by name."""
    folder = root / CHANGES_DIR
    files = [p for p in folder.glob("*.md") if p.name != "README.md"] if folder.is_dir() else []
    added = git(
        "log", "--reverse", "--diff-filter=A", "--format=", "--name-only", "--", CHANGES_DIR, root=root
    )
    rank = {name: i for i, name in enumerate(line.strip() for line in added.splitlines() if line.strip())}
    return sorted(files, key=lambda p: (rank.get(f"{CHANGES_DIR}/{p.name}", len(rank)), p.name))


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
        print("main has moved on since this checkout: release from origin/main (--open-pr)")
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
    files = notes_files(root)
    changelog, notes = cut_changelog((root / "CHANGELOG.md").read_text(), version, today,
                                     [p.read_text() for p in files], fallback)
    (root / "CHANGELOG.md").write_text(changelog)
    for p in files:
        p.unlink()   # gathered into the release (`git commit -a` records the deletions)
    for rel in PIN_FILES:
        p = root / rel
        if p.exists():
            p.write_text(update_pins(p.read_text(), version))
    notes_path.write_text(notes)
    return version


def open_pr(root: Path, today: str) -> str | None:
    """Cut the next release on `release/vX.Y.Z` from origin/main and open its PR; the PR's URL,
    or None with nothing to release. Works in a temporary worktree, so the checkout it runs
    from (its branch, its changes) is never touched."""
    listed = json.loads(gh("pr", "list", "--state", "open", "--json", "number,headRefName",
                           "--limit", "200", root=root) or "[]")
    if stale := [f"#{p['number']}" for p in listed if p["headRefName"].startswith("release/")]:
        raise SystemExit(f"release PR {', '.join(stale)} is still open: merge it or close it first")
    git("fetch", "-q", "--tags", "origin", "main", root=root)
    with tempfile.TemporaryDirectory(prefix="switchboard-release-") as tmp:
        tree, notes = Path(tmp) / "tree", Path(tmp) / "notes.md"
        git("worktree", "add", "-q", "--detach", str(tree), "origin/main", root=root)
        try:
            version = release(tree, notes, today)
            if version is None:
                return None
            branch = f"release/v{version}"
            git("switch", "-q", "-c", branch, root=tree)
            git("commit", "-q", "-am", f"release: v{version}", root=tree)
            # a branch left over from a closed release PR is replaced (no release PR is open)
            git("push", "-q", "--force", "origin", f"HEAD:refs/heads/{branch}", root=tree)
            notes.write_text(notes.read_text() + "\n---\n\nOpened by `.github/scripts/release.py --open-pr`. "
                             f"Merging it tags `v{version}` and publishes the GitHub Release and the image "
                             "(`.github/workflows/release.yml`).\n")
            return gh("pr", "create", "--base", "main", "--head", branch, "--title", f"release: v{version}",
                      "--body-file", str(notes), root=tree).strip()
        finally:
            git("worktree", "remove", "--force", str(tree), root=root)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    act = ap.add_mutually_exclusive_group(required=True)
    act.add_argument("--open-pr", action="store_true", help="cut the next release and open its PR")
    act.add_argument("--check-title", metavar="TITLE", help="check a PR title and exit")
    act.add_argument(
        "--merged", metavar="TITLE", help="print the version a merged release PR releases, if any"
    )
    act.add_argument("--notes-for", metavar="VERSION", help="print a release's CHANGELOG section")
    act.add_argument("--notes", help="apply the next release to this tree and write its notes here")
    ap.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    ap.add_argument("--today", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    root = Path(args.root)
    today = args.today or dt.datetime.now(dt.timezone.utc).date().isoformat()
    if args.check_title is not None:
        why = check_title(args.check_title)
        if why:
            print(f"PR title {args.check_title!r}: {why}", file=sys.stderr)
            return 1
        print("PR title ok")
        return 0
    if args.merged is not None:
        print(merged_release(args.merged, root) or "")
        return 0
    if args.notes_for:
        print(notes_for((root / "CHANGELOG.md").read_text(), args.notes_for), end="")
        return 0
    if args.open_pr:
        url = open_pr(root, today)
        print(url or "nothing to release: no PR opened")
        return 0
    version = release(root, Path(args.notes), today)
    output(released="true" if version else "false", version=version or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
