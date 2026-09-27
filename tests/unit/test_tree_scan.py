"""The repo carries no local machine details (public-release guard).

Every text file in the repo is scanned: tracked plus untracked-but-not-ignored
files when git is available, else a directory walk (the Docker test image has
no .git). It fails on:

- Claude Code scratch dirs (/tmp/claude-<uid>) and tmux socket dirs (tmux-<uid>);
- home paths (/Users/<name>, /home/<name>) and Claude transcript dir names
  (-Users-<name>-...) whose name is not one of the fakes in FAKE_NAMES;
- a session UUID in front of /scratchpad.

A second test checks names from a local denylist, which never lives in the repo.
Set SWITCHBOARD_PRIVATE_DENYLIST to a file outside the repo with one entry per line:
a name, or the SHA-256 hex digest of its lowercased form; # starts a comment.
Unset, that test is skipped (CI and the Docker image never have it). Tokens are
the lowercased runs of [a-z0-9_-], plus their runs of up to MAX_WORDS words
split on - and _ (so a name inside mcp__<name>__tool is still caught).

Write placeholders instead: ~ or $HOME, <uid>, <scratch>, <session-id>,
/Users/someone. Hit messages give file:line and, for a denylisted name, only a
digest prefix, never the name.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = frozenset(
    {".git", ".venv", ".worktrees", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
     "node_modules", "dist", "build"}
)
MAX_BYTES = 5 * 1024 * 1024
SNIFF_BYTES = 8192  # a NUL byte in here means binary

# Example or placeholder user names allowed after /Users/, /home/ and -Users- (compared lowercased).
FAKE_NAMES = frozenset(
    {"someone", "x", "me", "you", "runner", "user", "username", "name", "shared", "example", "alice", "bob",
     "dev", "linuxbrew"}
)

PATTERNS = (
    ("Claude scratch dir", re.compile(r"/tmp/claude-\d+")),
    ("tmux socket dir", re.compile(r"\btmux-\d+")),
    ("session UUID before /scratchpad",
     re.compile(r"(?i)\b(?!0{8}-0{4}-0{4}-0{4}-0{12})[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}[/\\]+scratchpad")),
)
NAMED_PATTERNS = (
    ("home path /Users/<name>", re.compile(r"/Users/([A-Za-z0-9_][A-Za-z0-9._-]*)")),
    # absolute /home/<name> only, not ".../yk/home/hooks" or "~/home"
    ("home path /home/<name>", re.compile(r"(?<![\w.~$}-])/home/([A-Za-z0-9_][A-Za-z0-9._-]*)")),
    ("Claude transcript dir -Users-<name>-", re.compile(r"-Users-([A-Za-z0-9_][A-Za-z0-9._]*)")),
)

DENYLIST_ENV = "SWITCHBOARD_PRIVATE_DENYLIST"
# Read and ~-expanded at import, before the autouse clean_env fixture points HOME at a temp dir.
DENYLIST_VALUE = os.path.expanduser(os.environ.get(DENYLIST_ENV, ""))
# Digest of a harmless canary that only the self-tests below plant (they build the
# string at run time, so this file never contains it whole). The repo scan uses it too.
CANARY_DIGEST = "217bd21028b1dfbaf265b4161b449df3eb0f33a419554a8575912dbb2f025373"
CANARY_ONLY = frozenset({CANARY_DIGEST})
MAX_WORDS = 4
TOKEN = re.compile(r"[a-z0-9_-]+")
SEPARATORS = re.compile(r"([_-]+)")
HEX_DIGEST = re.compile(r"[0-9a-f]{64}")


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def load_denylist(path: Path) -> frozenset[str]:
    """SHA-256 digests from a denylist file: a digest or a name per line (names are hashed)."""
    out = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        entry = raw.split("#", 1)[0].strip().lower()
        if entry:
            out.add(entry if HEX_DIGEST.fullmatch(entry) else _sha256(entry))
    return frozenset(out)


def denylist_from_env(value: str, root: Path = ROOT) -> frozenset[str]:
    """The local denylist named by DENYLIST_ENV's value; empty when unset. A missing file raises."""
    if not value:
        return frozenset()
    path = Path(value).resolve()
    if path.is_relative_to(root.resolve()):
        raise ValueError(f"{DENYLIST_ENV} must name a file outside the repo, or the list gets published")
    return load_denylist(path)


@functools.lru_cache(maxsize=None)
def denied_digest(token: str, deny: frozenset[str]) -> str | None:
    """The digest in deny that this lowercased token is or contains as whole words, if any."""
    candidates = {token}
    if "-" in token or "_" in token:
        parts = SEPARATORS.split(token)  # word, separator, word, ...
        n = len(parts) // 2 + 1
        for i in range(n):
            for j in range(i, min(i + MAX_WORDS, n)):
                candidates.add("".join(parts[2 * i : 2 * j + 1]))
    for c in candidates:
        d = _sha256(c)
        if d in deny:
            return d
    return None


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def structural_hits(text: str) -> list[tuple[int, str]]:
    """(line, what) for every local path or id in text; line numbers start at 1."""
    hits = [(_line_of(text, m.start()), what) for what, rx in PATTERNS for m in rx.finditer(text)]
    for what, rx in NAMED_PATTERNS:
        for m in rx.finditer(text):
            if m.group(1).rstrip(".").lower() not in FAKE_NAMES:
                hits.append((_line_of(text, m.start()), what))
    return hits


def denylist_hits(text: str, deny: frozenset[str]) -> list[tuple[int, str]]:
    """(line, what) for every token of text whose digest (or a word run's) is in deny."""
    lowered = text.lower()
    if not deny or not any(denied_digest(t, deny) for t in set(TOKEN.findall(lowered))):
        return []
    hits = []
    for n, line in enumerate(lowered.splitlines(), 1):
        for t in TOKEN.findall(line):
            d = denied_digest(t, deny)
            if d:
                hits.append((n, f"denylisted name (sha256 {d[:12]}...)"))
    return hits


def scan_text(text: str, deny: frozenset[str] = CANARY_ONLY) -> list[tuple[int, str]]:
    return sorted(set(structural_hits(text) + denylist_hits(text, deny)))


def read_text(path: Path) -> str | None:
    """The file's text, or None for a binary, oversized or unreadable file."""
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:SNIFF_BYTES]:
        return None
    return data.decode("utf-8", "replace")


def scan_file(path: Path, root: Path, deny: frozenset[str] = CANARY_ONLY) -> list[str]:
    text = read_text(path)
    if text is None:
        return []
    rel = path.relative_to(root).as_posix()
    return [f"{rel}:{line}: {what}" for line, what in scan_text(text, deny)]


def _git_names(root: Path) -> list[str] | None:
    if not (root / ".git").exists() or shutil.which("git") is None:
        return None
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True, timeout=30, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    names = [n for n in r.stdout.decode("utf-8", "surrogateescape").split("\0") if n]
    return names or None


def _walk_names(root: Path) -> Iterator[str]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if f not in SKIP_DIRS:  # a worktree's .git is a file naming the main checkout
                yield os.path.relpath(os.path.join(dirpath, f), root)


def repo_files(root: Path = ROOT) -> list[Path]:
    """Regular files to scan: git's tracked and unignored files, else a walk that skips SKIP_DIRS."""
    names = _git_names(root)
    out = set()
    for name in names if names is not None else _walk_names(root):
        p = root / name
        if not p.is_symlink() and p.is_file():
            out.add(p)
    return sorted(out)


@functools.cache
def repo_texts() -> tuple[tuple[str, str], ...]:
    """(path relative to ROOT, text) for every scanned file, read once per run."""
    texts = ((p.relative_to(ROOT).as_posix(), read_text(p)) for p in repo_files())
    return tuple((rel, text) for rel, text in texts if text is not None)


def test_repo_has_no_local_details() -> None:
    texts = repo_texts()
    assert Path(__file__).resolve().relative_to(ROOT).as_posix() in dict(texts)  # the scan covers this file too
    hits = [f"{rel}:{n}: {what}" for rel, text in texts for n, what in scan_text(text)]
    assert not hits, (
        "local machine details in the repo; use ~, $HOME, <uid>, <scratch>, "
        "<session-id> or /Users/someone instead:\n" + "\n".join(hits)
    )


def test_repo_has_no_locally_denylisted_names() -> None:
    deny = denylist_from_env(DENYLIST_VALUE)
    if not deny:
        pytest.skip(f"{DENYLIST_ENV} is not set")
    hits = [f"{rel}:{n}: {what}" for rel, text in repo_texts() for n, what in denylist_hits(text, deny)]
    assert not hits, "names from the local denylist in the repo:\n" + "\n".join(hits)


# --- self-tests: the same helpers catch planted strings (built at run time) ---

CANARY = "yk-canary-" + "private-name"
DENIED = "denylisted name (sha256 " + CANARY_DIGEST[:12] + "...)"
UUID = "12345678-1234-4abc-8def-1234567890ab"
PLANTED = [
    ("logs in /private/tmp/claude-" + "501" + "/x", "Claude scratch dir"),
    ("SCR=${SCR:-/tmp/claude-" + "1000/m0}", "Claude scratch dir"),
    ("socket /private/tmp/tmux-" + "501" + "/default", "tmux socket dir"),
    ("cd /Users/" + "Zed" + "/code/app", "home path /Users/<name>"),
    ("codex --remote unix:///Users/" + "zed.q" + "/x.sock", "home path /Users/<name>"),
    ("the checkout at /Users/" + "zed.", "home path /Users/<name>"),
    ('"cwd": "/home/' + "zed" + '/ws"', "home path /home/<name>"),
    ("~/.claude/projects/-Users-" + "zed" + "-code-app/x.jsonl", "Claude transcript dir -Users-<name>-"),
    ("runtime junk in <x>/" + UUID + "/" + "scratchpad/m0", "session UUID before /scratchpad"),
    ("mcp: " + CANARY + " on", DENIED),
    ("tool mcp__" + CANARY.upper() + "__get_x", DENIED),
    ("ENV_" + CANARY.replace("-", "_").upper() + "_URL=x", None),  # other separators: not the canary
]
PLACEHOLDERS = [
    "HOME=/Users/someone and /Users/x/.local and unix:///Users/me/.codex/x.sock and /Users/Shared/y",
    "/Users/ and /Users/$USER/.local and /Users/<user>/code and /Users/.../x",
    "--home /opt/yk/home/hooks and $SWITCHBOARD_HOME/home/x and /home/dev/switchboard and /home/linuxbrew/.linuxbrew",
    "~/.claude/projects/-Users-someone-code-app and <scratch>/m0/claude-inbox and tmux-<uid> and /tmp/claude-<uid>",
    "<session-id>/scratchpad and 00000000-0000-0000-0000-000000000000/scratchpad and " + UUID + " alone",
    "yk-canary and private-name and canary-private and " + CANARY[:-1] + " are not the canary",
]


def test_scanner_catches_planted_details(tmp_path: Path) -> None:
    f = tmp_path / "planted.md"
    f.write_text("\n".join(line for line, _ in PLANTED) + "\n")
    got = scan_file(f, tmp_path)
    want = [f"planted.md:{n}: {what}" for n, (_, what) in enumerate(PLANTED, 1) if what]
    assert got == want
    assert all(CANARY not in h.lower() for h in got)  # a hit never names the token
    assert _sha256(CANARY) == CANARY_DIGEST


def test_scanner_ignores_placeholders() -> None:
    assert scan_text("\n".join(PLACEHOLDERS)) == []


def test_local_denylist_file_is_loaded_and_used(tmp_path: Path) -> None:
    alpha, beta = "yk-demo-" + "alpha", "yk-demo-" + "beta"
    listfile = tmp_path / "denylist.txt"
    listfile.write_text(f"# local only\n\n{_sha256(alpha).upper()}  # by digest\n  {beta.upper()}\n")
    deny = denylist_from_env(str(listfile))
    assert deny == {_sha256(alpha), _sha256(beta)}
    text = f"one {alpha} two\nthree\nmcp__{beta}__get_x\n"
    assert [n for n, _ in denylist_hits(text, deny)] == [1, 3]
    assert all(alpha not in what and beta not in what for _, what in denylist_hits(text, deny))
    assert denylist_hits(text, CANARY_ONLY) == []  # not denied unless the local list names them
    assert denylist_from_env("") == frozenset()
    with pytest.raises(ValueError):  # a list inside the repo would get published
        denylist_from_env(str(ROOT / "denylist.txt"))
    with pytest.raises(FileNotFoundError):  # a typo in the path fails, not skips
        denylist_from_env(str(tmp_path / "missing.txt"))


def test_file_set_skips_junk_dirs_binaries_huge_files_and_symlinks(tmp_path: Path) -> None:
    bad = "planted " + CANARY + "\n"
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "ok.md").write_text("fine\n" + bad)
    for d in sorted(SKIP_DIRS - {".git"}):  # no .git, so repo_files walks
        (root / d / "sub").mkdir(parents=True)
        (root / d / "sub" / "x.md").write_text(bad)
    (root / "docs" / ".git").write_text(bad)  # a nested worktree's gitlink file
    (root / "blob.bin").write_bytes(b"\0" + bad.encode())
    (root / "huge.txt").write_bytes(bad.encode() + b"x" * MAX_BYTES)
    (tmp_path / "outside.md").write_text(bad)
    (root / "link.md").symlink_to(tmp_path / "outside.md")
    files = repo_files(root)
    assert {p.relative_to(root).as_posix() for p in files} == {"docs/ok.md", "blob.bin", "huge.txt"}
    hits = [h for p in files for h in scan_file(p, root)]
    assert hits == [f"docs/ok.md:2: {DENIED}"]
