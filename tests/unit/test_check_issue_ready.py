""".github/scripts/check_issue_ready.py: which issues an unattended agent may be handed.

Every gate is tried both ways. A checker that has only ever said PASS proves nothing: it would
say the same with an empty body.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Iterable
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "check_issue_ready.py"
spec = importlib.util.spec_from_file_location("check_issue_ready", SCRIPT)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

SHA = "5a7f301"
GOOD_BODY = f"""Build-ready: verified against origin/main @ {SHA} on 2026-09-30.

## What happens
`approval_mode_of` in `src/switchboard/delivery/engine.py` maps `auto` to unknown.

## Fix
- Add the two modes to `PROMPTING_MODES`, with tests in `tests/unit/test_engine_core.py`.
- A recorded payload, new: `tests/fixtures/payloads/claude/PostToolUse_auto.json`.
"""


class FakeTree:
    """origin/main as the test says it is: what's missing, what moved, whether the sha is on it."""

    def __init__(self, *, missing: Iterable[str] = (), moved: Iterable[str] = (), on_main: bool = True,
                 commits: int = 12) -> None:
        self._missing, self._moved, self._on_main, self._commits = set(missing), list(moved), on_main, commits
        self.asked: list[str] = []

    def missing(self, paths: list[str]) -> list[str]:
        self.asked = list(paths)
        return [p for p in paths if p in self._missing]

    def changed_since(self, sha: str, paths: list[str]) -> list[str] | None:
        if not self._on_main:
            return None
        return [p for p in self._moved if p in paths]

    def commits_since(self, sha: str) -> int:
        return self._commits


def issue(body: str = GOOD_BODY, labels: Iterable[str] = ("build-ready", "bug"), state: str = "OPEN",
          number: int = 73) -> dict:
    return {"number": number, "title": "Auto mode shows as unknown", "state": state, "body": body,
            "labels": [{"name": n} for n in labels]}


def verdict(i: dict | None = None, *, tree: FakeTree | None = None, still_open: Iterable[int] = (),
            allow_stale: bool = False) -> dict:
    return gate.evaluate(i or issue(), tree=tree or FakeTree(),
                         still_open=lambda ns: set(ns) & set(still_open), allow_stale=allow_stale)


def problems(*args, **kw) -> str:
    return "\n".join(verdict(*args, **kw)["problems"])


def test_a_groomed_issue_passes() -> None:
    v = verdict()
    assert v["problems"] == []
    assert v["notes"] == [f"verified against {SHA} on 2026-09-30"]


def test_the_label_is_required_because_readiness_is_absent_by_default() -> None:
    assert "no `build-ready` label" in problems(issue(labels=["bug"]))
    assert "no `build-ready` label" in problems(issue(labels=[]))


@pytest.mark.parametrize("label", ["needs-grooming", "needs-decision", "blocked", "human-gated", "in-progress"])
def test_a_label_that_says_it_is_someone_elses_turn_refuses_even_with_build_ready(label: str) -> None:
    assert f"labelled `{label}`" in problems(issue(labels=["build-ready", label]))


def test_a_closed_issue_is_refused() -> None:
    assert "it is closed, not open" in problems(issue(state="CLOSED"))


@pytest.mark.parametrize("text,found", [
    ("The mode maps to prompting (assumed).", "(assumed)"),
    ("Label colour: TBD", "TBD"),
    ("## Open question\n\nShould Auto mode get its own label?", '"Open question" heading'),
    ("### Open questions\n- one", '"Open question" heading'),
])
def test_an_unsettled_marker_refuses(text: str, found: str) -> None:
    assert found in problems(issue(body=GOOD_BODY + "\n" + text))


@pytest.mark.parametrize("text", [
    "Still open, and it doesn't block this: whether to rename the chip.",   # says so and leaves it alone
    "The TBDs of the old design are gone.",                                  # not the word TBD
    "It reads `## Open question` from the body.",                            # not a heading
])
def test_a_mention_of_something_open_is_not_a_marker(text: str) -> None:
    assert verdict(issue(body=GOOD_BODY + "\n" + text))["problems"] == []


@pytest.mark.parametrize("text", ["The chip works either way.", "Either is fine for the colour.",
                                  "Drop it if the Inspector gets crowded.", "A toggle, if you prefer."])
def test_a_decision_left_open_in_polite_words_refuses(text: str) -> None:
    assert "leaves a decision open" in problems(issue(body=GOOD_BODY + "\n" + text))


def test_no_stamp_refuses() -> None:
    assert "no `Build-ready: verified against origin/main" in problems(issue(body="Fix `src/switchboard/db.py`."))


def test_the_stamp_reads_through_backticks_and_a_long_sha() -> None:
    long = "5a7f301" + "0" * 33
    assert gate.parse_stamp(f"build-ready: verified against `origin/main` @ `{long}` on 2026-10-01") == (
        long, "2026-10-01")
    assert gate.parse_stamp("Build-ready: verified against origin/main on 2026-10-01") is None


def test_a_stamp_with_no_paths_to_check_refuses() -> None:
    body = f"Build-ready: verified against origin/main @ {SHA} on 2026-09-30.\n\nMake the chip amber."
    assert "names no repo paths" in problems(issue(body=body))


def test_a_path_that_changed_since_the_stamp_is_stale() -> None:
    tree = FakeTree(moved=["src/switchboard/delivery/engine.py"])
    text = problems(tree=tree)
    assert "stale: 1 path(s)" in text and "12 commit(s)" in text and "src/switchboard/delivery/engine.py" in text
    allowed = verdict(tree=tree, allow_stale=True)
    assert allowed["problems"] == [] and any(n.startswith("stale, allowed") for n in allowed["notes"])


def test_a_path_the_issue_does_not_name_may_change_freely() -> None:
    assert verdict(tree=FakeTree(moved=["src/switchboard/db.py"]))["problems"] == []


def test_a_stamped_commit_that_is_not_on_main_refuses() -> None:
    assert "isn't on origin/main" in problems(tree=FakeTree(on_main=False))


def test_a_named_path_that_does_not_exist_refuses_unless_the_line_says_new() -> None:
    tree = FakeTree(missing=["src/switchboard/delivery/engine.py", "tests/fixtures/payloads/claude/PostToolUse_auto.json"])
    text = problems(tree=tree)
    assert "aren't on origin/main: src/switchboard/delivery/engine.py." in text    # the existing-file claim is false
    assert "PostToolUse_auto.json" not in text                                      # declared new, so not expected
    assert "tests/fixtures/payloads/claude/PostToolUse_auto.json" not in tree.asked


def test_referenced_paths_skips_placeholders_and_globs() -> None:
    body = ("See `src/switchboard/web/static/app.js`, tests/unit/, CLAUDE.md and .github/workflows/test.yml.\n"
            "Notes go in changes/<branch>.md; tests match tests/unit/test_rules_*.py. See https://x.test/docs/a.md")
    assert gate.referenced_paths(body) == [".github/workflows/test.yml", "CLAUDE.md", "src/switchboard/web/static/app.js",
                                           "tests/unit"]


@pytest.mark.parametrize("text,deps", [
    ("Blocked by #41.", [41]),
    ("This depends on #41 and #52.", [41, 52]),
    ("#70 has to merge first.", [70]),
    ("These must land before this one:\n- #70\n- #71\n\nUnrelated: #5", [70, 71]),
    ("Follows up #41; see also #24.", []),               # a mention is not a dependency
    ("Blocked by the release.", []),
])
def test_dependencies_are_the_issues_a_line_says_it_waits_on(text: str, deps: list[int]) -> None:
    assert gate.dependencies(text) == deps


def test_an_open_dependency_refuses_and_a_closed_one_does_not() -> None:
    body = GOOD_BODY + "\nBlocked by #41 and #70.\n"
    assert "it waits on #70, still open" in problems(issue(body=body), still_open=[70])
    assert verdict(issue(body=body), still_open=[])["problems"] == []


def test_an_issue_never_waits_on_itself() -> None:
    assert verdict(issue(body=GOOD_BODY + "\nBlocked by #73.\n"), still_open=[73])["problems"] == []


def test_main_exits_1_when_any_issue_is_refused(monkeypatch: pytest.MonkeyPatch,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    issues = {73: issue(), 62: issue(number=62, labels=["enhancement"], body="An icon.")}
    monkeypatch.setattr(gate, "fetch_issue", lambda n: issues[n])
    monkeypatch.setattr(gate, "open_issues", lambda ns: set())
    monkeypatch.setattr(gate, "Tree", FakeTree)
    assert gate.main(["--no-fetch", "73"]) == 0
    assert "All 1 may be queued." in capsys.readouterr().out
    assert gate.main(["--no-fetch", "73", "#62"]) == 1
    out = capsys.readouterr()
    assert "PASS     #73" in out.out and "REFUSED  #62" in out.out
    assert "1 of 2 refused: #62" in out.err


def test_json_output_is_the_verdicts(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(gate, "fetch_issue", lambda n: issue())
    monkeypatch.setattr(gate, "open_issues", lambda ns: set())
    monkeypatch.setattr(gate, "Tree", FakeTree)
    assert gate.main(["--no-fetch", "--json", "73"]) == 0
    [v] = json.loads(capsys.readouterr().out)
    assert v["number"] == 73 and v["problems"] == []
