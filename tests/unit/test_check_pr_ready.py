""".github/scripts/check_pr_ready.py: whether a pull request is ready for the maintainer's review.

Every gate is tried both ways: a checker that has only ever said READY proves nothing.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "check_pr_ready.py"
spec = importlib.util.spec_from_file_location("check_pr_ready", SCRIPT)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

HEAD = "8154627952b6641bdce491585e9a92f9ad031fcf"
ASSETS = "1151ecba1d362932e8fb986cd0c64ee42bed18c4"
RAW = "https://raw.githubusercontent.com/someone/switchboard"
BODY = f"""Auto mode shows as approvals on.

## What it does
- Maps `auto` and `dontAsk` to prompting.

## Verification
```
$ uv run pytest -q tests/unit/test_engine_core.py
61 passed in 2.31s
```

## Pre-merge checklist
- [x] A recorded payload for the new mode is in tests/fixtures/payloads/.

## Post-merge ops
- [ ] Restart the running broker to pick the change up.

Closes #73.
"""


def check(name: str, conclusion: str | None = "SUCCESS", status: str = "COMPLETED", started: str = "2026-10-01T00:00:00Z") -> dict:
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion, "startedAt": started}


GREEN = [check("CI"), check("conventional PR title"), check("tree scan", "SKIPPED")]


def pr(**over: object) -> dict:
    base: dict = {
        "number": 74, "title": "fix(delivery): Auto and Don't-ask modes are approvals on", "body": BODY,
        "state": "OPEN", "isDraft": False, "baseRefName": "main", "headRefName": "fix/auto-mode", "headRefOid": HEAD,
        "mergeable": "MERGEABLE", "url": "https://github.com/someone/switchboard/pull/74",
        "files": [{"path": "src/switchboard/delivery/engine.py"}, {"path": "changes/auto-mode.md"}],
        "statusCheckRollup": GREEN, "closingIssuesReferences": [{"number": 73}],
    }
    base.update(over)
    return base


def verdict(p: dict | None = None, *, head: str | None = HEAD, ci_only: bool = False) -> dict:
    return gate.evaluate(p or pr(), head=head, ci_only=ci_only)


def problems(p: dict | None = None, **kw) -> str:
    return "\n".join(verdict(p, **kw)["problems"])


def test_a_finished_pull_request_is_ready() -> None:
    v = verdict()
    assert v["problems"] == [] and v["waiting"] == []
    assert v["notes"] == ["1 post-merge op(s) to do after the merge, before the issue is done"]


@pytest.mark.parametrize("over,found", [
    ({"state": "MERGED"}, "it is merged, not open"),
    ({"isDraft": True}, "it is a draft"),
    ({"baseRefName": "release/v0.9.0"}, "it targets release/v0.9.0, not main"),
    ({"mergeable": "CONFLICTING"}, "it conflicts with main"),
    ({"title": "Fix auto mode"}, "the title isn't a Conventional Commits one"),
])
def test_the_state_of_the_pull_request_refuses(over: dict, found: str) -> None:
    assert found in problems(pr(**over))


def test_an_unknown_mergeable_state_is_not_a_conflict() -> None:
    assert verdict(pr(mergeable="UNKNOWN"))["problems"] == []


# ------------------------------------------------------------------ the checks, on the head commit
def test_a_failed_required_check_refuses_and_names_the_commit() -> None:
    text = problems(pr(statusCheckRollup=[check("CI", "FAILURE"), check("conventional PR title")]))
    assert f"`CI` ended failure on {HEAD[:9]}" in text


@pytest.mark.parametrize("rollup,why", [
    ([check("conventional PR title")], "`CI` hasn't started"),                       # just pushed: no run yet
    ([check("CI", None, "IN_PROGRESS"), check("conventional PR title")], "`CI` is still running"),
    ([], "`CI` hasn't started"),
])
def test_checks_that_are_not_done_are_waiting_not_passed(rollup: list[dict], why: str) -> None:
    v = verdict(pr(statusCheckRollup=rollup))
    assert v["problems"] == [] and why in v["waiting"]


def test_a_check_that_ran_twice_counts_as_its_newest_run() -> None:
    old_red = check("CI", "FAILURE", started="2026-10-01T00:00:00Z")
    new_green = check("CI", "SUCCESS", started="2026-10-01T00:20:00Z")
    assert verdict(pr(statusCheckRollup=[new_green, old_red, check("conventional PR title")]))["problems"] == []
    old_green, new_red = check("CI", started="2026-10-01T00:00:00Z"), check("CI", "FAILURE", started="2026-10-01T00:20:00Z")
    assert "`CI` ended failure" in problems(pr(statusCheckRollup=[old_green, new_red, check("conventional PR title")]))


def test_only_the_required_checks_count() -> None:
    rollup = GREEN + [check("pytest (macos-latest, 2/3)", "FAILURE")]   # `CI` sums the test jobs up; it would be red too
    assert verdict(pr(statusCheckRollup=rollup))["problems"] == []


def test_a_local_commit_that_was_never_pushed_refuses_however_green_the_old_one_is() -> None:
    """A pull request with passing checks is refused when the local HEAD isn't its head commit.

    Why: the green checks belong to the commit before. The fix made locally was never pushed,
    so nothing has tested it, and "CI is green" would be a statement about other code.

    How: a pull request whose required checks all passed on its head, evaluated with a
    different local commit. The verdict must name both commits.
    """
    text = problems(head="0be7a06aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert "isn't the pull request's head" in text and "0be7a06aa" in text and HEAD[:9] in text


def test_an_unknown_local_head_is_not_compared() -> None:
    assert verdict(head=None)["problems"] == []


def test_ci_only_looks_at_nothing_but_the_checks() -> None:
    bare = pr(body="", isDraft=True, title="wip")
    assert verdict(bare, ci_only=True) == {"number": 74, "title": "wip", "problems": [], "waiting": [], "notes": []}
    assert "`CI` ended failure" in problems(pr(body="", statusCheckRollup=[check("CI", "FAILURE")]), ci_only=True)


def test_a_release_pr_is_only_asked_for_its_checks() -> None:
    v = verdict(pr(title="release: v0.9.1", body="## 0.9.1", files=[{"path": "CHANGELOG.md"}]))
    assert v["problems"] == [] and v["notes"] == ["a release PR: only its checks are looked at"]


# ------------------------------------------------------------------ evidence
def test_no_verification_section_refuses() -> None:
    assert "no Verification section" in problems(pr(body="It works now.\n\n## Tests\nAll green."))


def test_a_verification_section_in_the_agents_own_words_is_not_evidence() -> None:
    body = "## Verification\nRan the suite; everything passed and the page looks right.\n\n## Docs\n```\nnot here\n```\n"
    assert "neither a command's output" in problems(pr(body=body))


@pytest.mark.parametrize("proof", [
    "```\n$ uv run pytest -q\n2733 passed\n```",
    "~~~console\n$ switchboard status\nrunning\n~~~",
    f'<img alt="the chip" src="{RAW}/{ASSETS}/pr-74/chip-light.png" width="720">',
    "![the chip](https://github.com/user-attachments/assets/abc)",
])
def test_a_transcript_or_a_picture_is_evidence(proof: str) -> None:
    assert verdict(pr(body=f"## Verification\n{proof}\n"))["problems"] == []


@pytest.mark.parametrize("block", ["```\n\n```", "```console\n```", "```\n$ uv run pytest -q\n2733 passed",
                                   "```\n" + "x\n" * 50_000 + "~~~"])
def test_an_empty_or_unclosed_fenced_block_is_not_a_transcript(block: str) -> None:
    assert "neither a command's output" in problems(pr(body=f"## Verification\n{block}\n"))


WEB = [{"path": "src/switchboard/web/static/app.js"}, {"path": "changes/chip.md"}]


def test_a_visible_change_needs_a_preview_from_its_own_folder() -> None:
    assert "it changes the web UI and embeds no preview from pr-74/" in problems(pr(files=WEB))
    cli = [{"path": "src/switchboard/cli.py"}, {"path": "changes/status.md"}]
    assert "it changes CLI output and embeds no preview" in problems(pr(files=cli))
    before_only = BODY + f'\n<img src="{RAW}/{ASSETS}/pr-51/before.png">\n'      # another PR's folder: the "before"
    assert "embeds no preview from pr-74/" in problems(pr(files=WEB, body=before_only))
    with_preview = before_only + f'<img src="{RAW}/{ASSETS}/pr-74/chip-dark.png">\n'
    assert verdict(pr(files=WEB, body=with_preview))["problems"] == []


def test_a_preview_linked_by_a_branch_refuses_because_the_link_will_break() -> None:
    body = BODY + f'\n<img src="{RAW}/design-assets/pr-74/chip.png">\n'
    text = problems(pr(files=WEB, body=body))
    assert "linked by `design-assets`, not by a commit SHA" in text and "embeds no preview from pr-74/" in text


def test_a_change_nobody_sees_needs_no_preview() -> None:
    assert verdict(pr(files=[{"path": "src/switchboard/db.py"}, {"path": "changes/db.md"}]))["problems"] == []


# ------------------------------------------------------------------ the checklists and the notes
def test_an_unticked_pre_merge_item_refuses() -> None:
    body = BODY.replace("- [x] A recorded payload", "- [ ] A recorded payload")
    assert '1 unticked item(s) under Pre-merge checklist, the first: "A recorded payload' in problems(pr(body=body))


def test_unticked_post_merge_ops_are_a_note_for_close_out() -> None:
    assert verdict(pr(body=BODY.replace("- [ ] Restart", "- [x] Restart")))["notes"] == []


def test_a_change_under_src_needs_its_notes_file() -> None:
    assert "adds no notes file" in problems(pr(files=[{"path": "src/switchboard/delivery/engine.py"}]))
    readme_only = [{"path": "src/switchboard/db.py"}, {"path": "changes/README.md"}]
    assert "adds no notes file" in problems(pr(files=readme_only))
    assert verdict(pr(files=[{"path": "tests/unit/test_db.py"}, {"path": "docs/DESIGN.md"}]))["problems"] == []


def test_editing_the_changelog_refuses_unless_the_title_is_scoped_changelog() -> None:
    files = [{"path": "CHANGELOG.md"}]
    assert "it edits CHANGELOG.md" in problems(pr(files=files))
    assert verdict(pr(files=files, title="docs(changelog): fix 0.8.0's notes"))["problems"] == []


def test_closing_no_issue_is_a_note() -> None:
    v = verdict(pr(closingIssuesReferences=[]))
    assert v["problems"] == [] and any("closes no issue" in n for n in v["notes"])


def test_section_stops_at_the_next_heading_of_its_level() -> None:
    body = "## Verification\nabove\n### Details\ninside\n## Docs\nbelow\n"
    assert gate.section(body, "verification") == "above\n### Details\ninside"
    assert gate.section(body, "Previews") is None


@pytest.mark.parametrize("over,ci_only,code,mark", [
    ({}, False, 0, "READY"),
    ({"isDraft": True}, False, 1, "REFUSED"),
    ({"statusCheckRollup": [check("conventional PR title")]}, True, 2, "WAITING"),
    ({"isDraft": True, "statusCheckRollup": []}, False, 1, "REFUSED"),     # a problem outranks waiting
])
def test_main_exit_codes(over: dict, ci_only: bool, code: int, mark: str, monkeypatch: pytest.MonkeyPatch,
                         capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(gate, "fetch_pr", lambda n: pr(**over))
    monkeypatch.setattr(gate, "local_head", lambda branch: HEAD)
    assert gate.main((["--ci-only"] if ci_only else []) + ["#74"]) == code
    assert f"{mark:7}  #74" in capsys.readouterr().out
