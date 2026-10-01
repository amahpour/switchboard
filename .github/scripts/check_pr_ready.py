"""Whether a pull request is ready for the maintainer to review (CLAUDE.md, "Work goes through
the gates"; the `work-on-an-issue` skill's last gate).

    python3 .github/scripts/check_pr_ready.py 74
    python3 .github/scripts/check_pr_ready.py --ci-only 74     # only the checks, to wait on them
    python3 .github/scripts/check_pr_ready.py --json 74

Exit 0: ready. Exit 1: refused, with each reason. Exit 2: nothing is wrong, but the checks on
the head commit haven't finished (or haven't started): wait and run it again.

"Done" from the agent that did the work isn't evidence, and neither is a green check on some
earlier commit. So this reads the pull request itself:

- it is open, not a draft, targets main, has no conflict, and its title is a Conventional
  Commits one;
- `CI` and `conventional PR title` passed on the head commit, and the head commit is the one
  checked out here (a commit that was never pushed has no checks);
- the description has a Verification section with a command's output or a picture in it;
- a change to something a person sees (the web UI, CLI output) embeds previews from this
  pull request's folder on `design-assets`, linked by commit SHA so they outlive the branch;
- nothing under "Pre-merge checklist" is left unticked (an item that doesn't apply is deleted);
- a change under src/ brings its notes in changes/<name>.md, and nothing edits CHANGELOG.md.

A release PR (`release: vX.Y.Z`) is only asked for its checks. Unticked "Post-merge ops" and a
missing `Closes #N` are notes: the first are the `close-out` skill's business.

The pure part (`evaluate`) is apart from `gh` and `git`, which tests replace. Standard library
only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import release  # noqa: E402  (release.py, next to this file)

ROOT = Path(__file__).resolve().parents[2]

REQUIRED_CHECKS = ("CI", "conventional PR title")   # the ruleset on main requires these two
# Changes a person sees, which need previews. A floor, not the whole rule: CLAUDE.md asks for
# previews of anything visible, and the agent decides that for paths this list doesn't know.
VISIBLE = (("src/switchboard/web/static/", "the web UI"), ("src/switchboard/cli.py", "CLI output"))
MEDIA = r"(?:png|jpe?g|gif|webp|mp4|webm)"
FIELDS = ("number,title,body,state,isDraft,baseRefName,headRefName,headRefOid,mergeable,url,files,"
          "statusCheckRollup,closingIssuesReferences")

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
UNTICKED_RE = re.compile(r"^\s*[-*]\s+\[ \]\s+(.*)$", re.M)
IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)\s]+\)|<(?:img|video)\b[^>]*\bsrc=", re.I)


# ------------------------------------------------------------------ gh and git
def fetch_pr(number: int, root: Path = ROOT) -> dict:
    out = subprocess.run(["gh", "pr", "view", str(number), "--json", FIELDS], cwd=root, check=True,
                         capture_output=True, text=True).stdout
    data = json.loads(out)
    assert isinstance(data, dict)
    return data


def local_head(branch: str, root: Path = ROOT) -> str | None:
    """The commit checked out here, when this checkout is on the pull request's branch."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True).stdout.strip()
    return git("rev-parse", "HEAD") if git("rev-parse", "--abbrev-ref", "HEAD") == branch else None


# ------------------------------------------------------------------ pure parts
def section(body: str, title: str) -> str | None:
    """The text under the heading that starts with `title` (any level), up to the next heading of
    that level or higher. None when there is no such heading."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if m and m[2].lower().startswith(title.lower()):
            level = len(m[1])
            rest = []
            for nxt in lines[i + 1:]:
                n = HEADING_RE.match(nxt)
                if n and len(n[1]) <= level:
                    break
                rest.append(nxt)
            return "\n".join(rest)
    return None


def has_transcript(text: str) -> bool:
    """Whether the text has a closed fenced block with something in it. Line by line: the body
    is text someone else may have written, and a regex over all of it could be made to crawl."""
    fence, filled = None, False
    for line in text.splitlines():
        mark = line.strip()[:3]
        if fence is None:
            if mark in ("```", "~~~"):
                fence, filled = mark, False
        elif mark == fence:
            if filled:
                return True
            fence = None
        elif line.strip():
            filled = True
    return False


def checks(rollup: list[dict]) -> tuple[list[str], list[str]]:
    """(failed, pending) among the required checks on the head commit. A check that ran twice
    counts as its newest run; one that isn't there yet is pending."""
    failed, pending = [], []
    for name in REQUIRED_CHECKS:
        runs = [c for c in rollup if (c.get("name") or c.get("context")) == name]
        if not runs:
            pending.append(f"`{name}` hasn't started")
            continue
        run = max(runs, key=lambda c: c.get("startedAt") or "")
        state = run.get("conclusion") or run.get("state") or ""
        if run.get("status", "COMPLETED") != "COMPLETED" or state in ("", "PENDING", "EXPECTED"):
            pending.append(f"`{name}` is still running")
        elif state != "SUCCESS":
            failed.append(f"`{name}` ended {state.lower()}")
    return failed, pending


def evaluate(pr: dict, *, head: str | None, ci_only: bool = False) -> dict:
    """The verdict: problems refuse it, waiting means the checks aren't done, notes are for the reader.
    `head` is the commit checked out locally on the PR's branch, or None when that isn't known."""
    number, title, body = pr.get("number"), pr.get("title") or "", pr.get("body") or ""
    files = [f["path"] for f in pr.get("files") or []]
    problems: list[str] = []
    notes: list[str] = []

    oid = pr.get("headRefOid") or ""
    if head and head != oid:
        problems.append(f"the commit checked out here ({head[:9]}) isn't the pull request's head ({oid[:9]}):"
                        " push it, then wait for the checks on it")
    failed, waiting = checks(pr.get("statusCheckRollup") or [])
    problems += [f"{f} on {oid[:9]}: read the failing job's log" for f in failed]
    is_release = bool(release.RELEASE_TITLE_RE.match(title))
    if ci_only or is_release:
        if is_release and not ci_only:
            notes.append("a release PR: only its checks are looked at")
        return {"number": number, "title": title, "problems": problems, "waiting": waiting, "notes": notes}

    if pr.get("state") != "OPEN":
        problems.append(f"it is {str(pr.get('state')).lower()}, not open")
    if pr.get("isDraft"):
        problems.append("it is a draft")
    if pr.get("baseRefName") != "main":
        problems.append(f"it targets {pr.get('baseRefName')}, not main")
    if pr.get("mergeable") == "CONFLICTING":
        problems.append("it conflicts with main: bring the branch up to date")
    if why := release.check_title(title):
        problems.append(f"the title isn't a Conventional Commits one: {why}")

    proof = section(body, "Verification")
    if proof is None:
        problems.append("no Verification section. Show the change working: the command you ran and what it"
                        " printed, or a picture")
    elif not (has_transcript(proof) or IMAGE_RE.search(proof)):
        problems.append("the Verification section has neither a command's output (a fenced block) nor a picture."
                        " A summary in your own words isn't evidence")

    m = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/", pr.get("url") or "")
    slug = re.escape(m[1]) if m else r"[^/\s]+/[^/\s]+"
    raw = re.findall(rf"https://raw\.githubusercontent\.com/{slug}/([^/\s\"')]+)/([^\s\"')]+\.{MEDIA})", body, re.I)
    unpinned = sorted({ref for ref, _ in raw if not re.fullmatch(r"[0-9a-f]{40}", ref)})
    if unpinned:
        problems.append(f"a preview is linked by `{unpinned[0]}`, not by a commit SHA: the link breaks when that"
                        " moves or is deleted")
    seen = sorted({what for prefix, what in VISIBLE if any(f.startswith(prefix) for f in files)})
    if seen:
        mine = [p for ref, p in raw if p.startswith(f"pr-{number}/") and re.fullmatch(r"[0-9a-f]{40}", ref)]
        if not mine:
            problems.append(f"it changes {' and '.join(seen)} and embeds no preview from pr-{number}/ on"
                            " `design-assets` (CLAUDE.md, \"Every pull request shows what it changes\")")

    pre = section(body, "Pre-merge checklist")
    if pre and (left := UNTICKED_RE.findall(pre)):
        problems.append(f"{len(left)} unticked item(s) under Pre-merge checklist, the first: \"{left[0][:80]}\"."
                        " Do it and tick it, or delete an item that doesn't apply")
    post = section(body, "Post-merge ops")
    if post and (left := UNTICKED_RE.findall(post)):
        notes.append(f"{len(left)} post-merge op(s) to do after the merge, before the issue is done")

    conventional = release.TITLE_RE.match(title)
    if "CHANGELOG.md" in files and not (conventional and conventional["scope"] == "changelog"):
        problems.append("it edits CHANGELOG.md: only a release writes it. Put the notes in changes/<name>.md")
    has_notes = any(f.startswith("changes/") and f.endswith(".md") and f != "changes/README.md" for f in files)
    if any(f.startswith("src/") for f in files) and not has_notes:
        problems.append("it changes src/ and adds no notes file, changes/<name>.md (changes/README.md)")
    if not pr.get("closingIssuesReferences"):
        notes.append("it closes no issue (`Closes #N` in the description), which is fine when there isn't one")
    return {"number": number, "title": title, "problems": problems, "waiting": waiting, "notes": notes}


def report(v: dict) -> str:
    mark = "REFUSED" if v["problems"] else "WAITING" if v["waiting"] else "READY"
    lines = [f"\n{mark:7}  #{v['number']}  {v['title'][:70]}"]
    lines += [f"         note: {n}" for n in v["notes"]]
    lines += [f"         .. {w}" for w in v["waiting"]]
    lines += [f"         x {p}" for p in v["problems"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("pr", type=lambda s: int(s.lstrip("#")), help="the pull request's number")
    ap.add_argument("--ci-only", action="store_true", help="look only at the checks on the head commit")
    ap.add_argument("--json", action="store_true", help="print the verdict as JSON")
    args = ap.parse_args(argv)

    pr = fetch_pr(args.pr)
    verdict = evaluate(pr, head=local_head(pr.get("headRefName") or ""), ci_only=args.ci_only)
    print(json.dumps(verdict, indent=2) if args.json else report(verdict))
    return 1 if verdict["problems"] else 2 if verdict["waiting"] else 0


if __name__ == "__main__":
    sys.exit(main())
