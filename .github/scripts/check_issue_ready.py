"""Whether an issue may be handed to an agent that has nobody to ask (CLAUDE.md, "Labels say
whose turn it is"; the `groom-issues` and `overnight-run` skills).

    python3 .github/scripts/check_issue_ready.py 73 60
    python3 .github/scripts/check_issue_ready.py --allow-stale 73
    python3 .github/scripts/check_issue_ready.py --json 73

Exit 0: every issue may be queued. Exit 1: at least one may not, with the issue, the gate it
failed and what to do about it.

Readiness is a token that is absent by default. An open issue, a clear title and the right
labels look like a ready queue and aren't one: nothing in them says the description survives
an agent that builds exactly what it reads at 2am. So an issue passes only when a grooming
pass has earned it:

- it is open, carries `build-ready`, and carries none of `needs-grooming`, `needs-decision`,
  `blocked`, `human-gated` or `in-progress`;
- its body has no unsettled marker (`(assumed)`, `TBD`, an "Open question" heading) and no
  hedged decision ("works either way");
- every issue it says it depends on is closed;
- its body carries `Build-ready: verified against origin/main @ <sha> on <YYYY-MM-DD>.`, the
  commit is on main, the body names the repo paths the work touches, each of them exists
  (one the work creates says `new` on its line), and none has changed since that commit. A
  groomed description is a photograph, not a contract.

A pass says where the description came from and that main hasn't moved under it. It doesn't
say the description is true: this never reads the code the description describes. Checking
each claim against the tree is the grooming pass's job, before it writes the stamp.

The pure parts (parsing, the gates) are apart from the two impure ones (`gh` and `git`), which
tests replace. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

READY_LABEL = "build-ready"
# Each of these says it isn't an unattended agent's turn, whatever else is true.
DISQUALIFYING_LABELS = {
    "needs-grooming": "nobody has written the spec yet",
    "needs-decision": "the maintainer has a decision to make",
    "blocked": "it waits on another issue",
    "human-gated": "its deliverable is an action only the maintainer takes",
    "in-progress": "an agent is already on it",
}

# Literal marks of a spec still being argued about. Short on purpose: a description may say
# "still open, doesn't block" about something it then leaves alone.
UNSETTLED_MARKERS = ("(assumed)", "NOT-A-BUILD", "NEEDS-DECISION")
TBD_RE = re.compile(r"\bTBD\b")
OPEN_QUESTION_RE = re.compile(r"^#{1,6}\s*open\s+questions?\b", re.I | re.M)

# An open decision in polite words is as blocking as "(assumed)".
HEDGES = (
    (re.compile(r"works?\s+either\s+way", re.I), "'works either way': the choice was never made"),
    (re.compile(r"\beither\s+is\s+fine\b", re.I), "'either is fine': no decision recorded"),
    (re.compile(r"\bwould\s+rather\b", re.I), "'would rather': leaves the decision to someone"),
    (re.compile(r"\bdrop\s+it\s+if\b", re.I), "'drop it if': scope that depends on an answer"),
    (re.compile(r"\bif\s+(?:you|the\s+maintainer)\s+(?:would\s+|'d\s+)?prefers?\b", re.I),
     "'if you prefer': unresolved"),
)

STAMP_RE = re.compile(
    r"Build-ready:\s*verified\s+against\s+`?origin/main`?\s*@\s*`?([0-9a-f]{7,40})`?\s*on\s*(\d{4}-\d{2}-\d{2})",
    re.I)

# Repo paths a description points at: src/switchboard/db.py, tests/unit/, .github/workflows/test.yml.
# One with a placeholder or a glob in it (changes/<branch>.md, tests/unit/test_*.py) names no file.
PATH_RE = re.compile(r"(?<![\w/.<-])((?:src|tests|docs|deploy|sandbox|changes|\.github|\.claude)/[\w.*<>/-]*[\w*>])")
TOP_FILE_RE = re.compile(
    r"(?<![\w/.<-])((?:README|CLAUDE|AGENTS|CONTRIBUTING|SECURITY|CHANGELOG)\.md|pyproject\.toml|uv\.lock|Dockerfile)\b")

# A dependency is a line that says so and names an issue. A line ending in a colon carries its
# phrase to the bullets under it ("These have to merge first:" and then the list).
DEPENDENCY_RE = re.compile(
    r"\bblocked\s+by\b|\bdepends\s+on\b|\bgated\s+on\b|\bwait(?:s|ing)\s+(?:on|for)\b|\bprerequisite\b"
    r"|\b(?:must|has\s+to|have\s+to|needs\s+to)\s+(?:ship|merge|land)\b|\b(?:ships?|merges?|lands?)\s+first\b",
    re.I)
ISSUE_REF_RE = re.compile(r"(?<![\w&])#(\d+)\b")
BULLET_RE = re.compile(r"^\s*(?:[*+-]|\d+[.)])\s+")
NEW_RE = re.compile(r"\bnew\b", re.I)


# ------------------------------------------------------------------ gh and git
def gh_json(*args: str, root: Path = ROOT) -> object:
    out = subprocess.run(["gh", *args], cwd=root, check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def fetch_issue(number: int) -> dict:
    data = gh_json("issue", "view", str(number), "--json", "number,title,state,body,labels")
    assert isinstance(data, dict)
    return data


def open_issues(numbers: Iterable[int]) -> set[int]:
    """Which of these issues (or pull requests) are still open."""
    still = set()
    for n in sorted(set(numbers)):
        data = gh_json("api", f"repos/{{owner}}/{{repo}}/issues/{n}")
        if isinstance(data, dict) and data.get("state") == "open":
            still.add(n)
    return still


def git(*args: str, root: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


class Tree:
    """What git knows about origin/main. Tests pass a stand-in with the same three methods."""

    def missing(self, paths: list[str]) -> list[str]:
        return [p for p in paths if git("cat-file", "-e", f"origin/main:{p.rstrip('/')}").returncode != 0]

    def changed_since(self, sha: str, paths: list[str]) -> list[str] | None:
        """The named paths that changed between sha and origin/main, or None when sha isn't on main."""
        if git("merge-base", "--is-ancestor", sha, "origin/main").returncode != 0:
            return None
        if not paths:
            return []
        out = git("diff", "--name-only", sha, "origin/main", "--", *paths).stdout
        return [line for line in out.splitlines() if line]

    def commits_since(self, sha: str) -> int:
        out = git("rev-list", "--count", f"{sha}..origin/main").stdout.strip()
        return int(out) if out.isdigit() else 0


# ------------------------------------------------------------------ pure parts
def parse_stamp(body: str) -> tuple[str, str] | None:
    m = STAMP_RE.search(body)
    return (m[1].lower(), m[2]) if m else None


def referenced_paths(body: str) -> list[str]:
    """The existing repo paths the body points at. A path on a line that says `new` is one the
    work creates, so it isn't expected on main."""
    found: set[str] = set()
    for line in body.splitlines():
        if NEW_RE.search(line):
            continue
        found |= {m[1] for m in PATH_RE.finditer(line)} | {m[1] for m in TOP_FILE_RE.finditer(line)}
    return sorted(p for p in found if not set(p) & set("*<>"))


def unsettled(body: str) -> list[str]:
    found = [m for m in UNSETTLED_MARKERS if m in body]
    if TBD_RE.search(body):
        found.append("TBD")
    if OPEN_QUESTION_RE.search(body):
        found.append('an "Open question" heading')
    return found


def hedged(body: str) -> list[str]:
    return [why for rx, why in HEDGES if rx.search(body)]


def dependencies(body: str) -> list[int]:
    """Issue numbers the body names as something this one waits on, in order, once each."""
    found: list[int] = []
    carry = False
    for line in body.splitlines():
        says = bool(DEPENDENCY_RE.search(line))
        if says or (carry and BULLET_RE.match(line)):
            found += [int(n) for n in ISSUE_REF_RE.findall(line)]
        if says:
            carry = line.rstrip().endswith(":")
        elif not (carry and (BULLET_RE.match(line) or not line.strip())):
            carry = False
    return list(dict.fromkeys(found))


def evaluate(issue: dict, *, tree: Tree, still_open: Callable[[Iterable[int]], set[int]],
             allow_stale: bool = False) -> dict:
    """One issue's verdict: its problems (any refuses it) and notes."""
    number = issue.get("number")
    labels = {lb["name"] for lb in issue.get("labels") or []}
    body = issue.get("body") or ""
    problems: list[str] = []
    notes: list[str] = []

    if issue.get("state") != "OPEN":
        problems.append(f"it is {str(issue.get('state')).lower()}, not open")
    if READY_LABEL not in labels:
        problems.append(f"no `{READY_LABEL}` label. Readiness is absent by default: the `groom-issues` skill"
                        " applies it once every claim in the description is checked against the code")
    for label, why in DISQUALIFYING_LABELS.items():
        if label in labels:
            problems.append(f"labelled `{label}`: {why}")
    if found := unsettled(body):
        problems.append(f"the description still has {', '.join(found)}. Settle it, or write down the decision made")
    if found := hedged(body):
        problems.append("the description leaves a decision open: " + "; ".join(found))

    deps = [n for n in dependencies(body) if n != number]
    if waiting := sorted(still_open(deps)) if deps else []:
        problems.append("it waits on " + ", ".join(f"#{n}" for n in waiting) + ", still open. Label it `blocked`"
                        " and take it out of the queue until that closes")

    stamp = parse_stamp(body)
    paths = referenced_paths(body)
    if stamp is None:
        problems.append("no `Build-ready: verified against origin/main @ <sha> on <YYYY-MM-DD>.` line, so nothing"
                        " says which tree the description was checked against")
    else:
        sha, day = stamp
        notes.append(f"verified against {sha} on {day}")
        if not paths:
            problems.append("stamped, but the description names no repo paths, so the stamp can never go stale"
                            " however far main moves. Name the files the work touches")
        moved = tree.changed_since(sha, paths)
        if moved is None:
            problems.append(f"the stamped commit {sha} isn't on origin/main. Fetch, or groom against one that is")
        elif moved:
            detail = (f"{len(moved)} path(s) the description points at changed in the {tree.commits_since(sha)}"
                      f" commit(s) since {sha}: {', '.join(moved)}")
            if allow_stale:
                notes.append(f"stale, allowed: {detail}")
            else:
                problems.append(f"stale: {detail}. Check the description against main again and re-stamp it"
                                " (or --allow-stale, for one that names symbols and not lines)")
    if gone := tree.missing(paths):
        problems.append(f"it names path(s) that aren't on origin/main: {', '.join(gone)}. If the work creates one,"
                        " say `new` on that line")
    return {"number": number, "title": issue.get("title") or "", "problems": problems, "notes": notes}


def report(verdicts: list[dict]) -> str:
    lines: list[str] = []
    for v in verdicts:
        lines.append(f"\n{'REFUSED' if v['problems'] else 'PASS':7}  #{v['number']}  {v['title'][:70]}")
        lines += [f"         note: {n}" for n in v["notes"]]
        lines += [f"         x {p}" for p in v["problems"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("issues", nargs="+", type=lambda s: int(s.lstrip("#")), help="issue numbers, such as 73 60")
    ap.add_argument("--allow-stale", action="store_true",
                    help="a path that changed since the stamp is a note, not a refusal")
    ap.add_argument("--no-fetch", action="store_true", help="don't fetch origin/main first")
    ap.add_argument("--json", action="store_true", help="print the verdicts as JSON")
    args = ap.parse_args(argv)

    if not args.no_fetch and git("fetch", "--quiet", "origin", "main").returncode != 0:
        print("warning: couldn't fetch origin/main; checking against the copy here", file=sys.stderr)
    tree = Tree()
    verdicts = [evaluate(fetch_issue(n), tree=tree, still_open=open_issues, allow_stale=args.allow_stale)
                for n in args.issues]
    print(json.dumps(verdicts, indent=2) if args.json else report(verdicts))
    refused = [f"#{v['number']}" for v in verdicts if v["problems"]]
    if refused:
        print(f"\n{len(refused)} of {len(verdicts)} refused: {', '.join(refused)}. Don't queue these: groom them"
              " first (the `groom-issues` skill). A question the code can answer is never a reason to refuse:"
              " answer it and write the answer into the issue.", file=sys.stderr)
        return 1
    if not args.json:
        print(f"\nAll {len(verdicts)} may be queued.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
