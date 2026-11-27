"""The ready line's rules (``reviews.ready``, DESIGN.md §37.21): pure, no broker. What counts as CI, a settled
card and a verdict at the latest commit, and what does not. The broker tests are in
``tests/integration/test_review_ready.py``."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from switchboard import reviews
from switchboard.reviews import Item

SHA = "abcdef1234567890abcdef1234567890abcdef12"
SHORT = SHA[:12]
LEAD = (7, "lead-1")  # the coordinator's membership id and screen name


def card(state: str = "fixed", *, raised_by: str = "codex-1", owner: str = "claude-1", n: int = 1) -> Item:
    return Item(
        id=n, n=n, kind="finding", state=state, title="off by one", detail="", file="a.py", lines="3-4",
        raised_by=raised_by, owner=owner, commit=SHORT, reason="", options=(), recommend=None, answer="",
        answered_by="", confirmed_by="codex-1" if state == "fixed" else "",
    )  # fmt: skip


def ev(**data: Any) -> dict[str, Any]:
    return {"ts": 0.0, "data": data}


def board(head: str = SHORT) -> dict[str, Any]:
    return {"url": "https://example.com/shop/pull/7", "head": head, "closed_at": None, "opened_at": 0.0}


def passing(head: str = SHA, membership: int = 7) -> dict[str, Any]:
    return ev(report={"pr_ci": "passing"}, by="lead-1", membership=membership, head=head)


def verdict(by: str = "codex-1", head: str = SHORT) -> dict[str, Any]:
    return ev(verdict=head, by=by)


def line(events: list, items: list | None = None, agents: tuple = ("lead-1", "claude-1", "codex-1")) -> str:
    got = reviews.ready(board(), items if items is not None else [card()], events, LEAD, agents)
    assert got is not None
    return got.line


def test_all_three_at_the_latest_commit_is_ready_and_a_short_hash_matches_a_long_one() -> None:
    """A report at 40 characters and a verdict at 7 are one commit. Fails if they are compared as strings."""
    assert line([passing(), verdict()]) == f"Ready at {SHA[:12]}"
    assert (
        reviews.same_commit(SHA[:7], SHA)
        and reviews.same_commit(SHA, SHORT)
        and reviews.same_commit(SHA, SHA)
    )
    assert not reviews.same_commit(SHORT, "abcdef2") and not reviews.same_commit("", SHA)


def test_no_coordinator_or_a_closed_board_has_no_line() -> None:
    """The line is hidden, not "not ready", without a coordinator and once the board is closed."""
    assert reviews.ready(board(), [card()], [passing(), verdict()], None, ("codex-1",)) is None
    closed = {**board(), "closed_at": 5.0}
    assert reviews.ready(closed, [card()], [passing(), verdict()], LEAD, ("codex-1",)) is None


def test_each_part_missing_alone() -> None:
    """The line names exactly the part that is missing. Fails if a part is not required or is blamed for
    another."""
    assert line([verdict()]) == f"Not ready: CI not reported passing at {SHORT} by lead-1"
    assert line([passing()]) == f"Not ready: codex-1 has no verdict at {SHORT}"
    assert (
        line([passing(), verdict()], [replace(card(), confirmed_by="")]) == "Not ready: F1 awaits its check"
    )
    for state in ("raised", "contested", "conceded"):
        assert line([passing(), verdict()], [card(state)]) == f"Not ready: F1 is {state}"
    assert line([passing(), verdict()], [replace(card(), kind="question", state="open")]) == (
        "Not ready: Q1 is open"
    )


def test_only_the_coordinators_own_report_counts() -> None:
    """Another agent's report, the right name with another membership, and a report from before the event
    recorded one are not the coordinator's. Fails if a name is enough."""
    missing = f"Not ready: CI not reported passing at {SHORT} by lead-1"
    other = ev(report={"pr_ci": "passing"}, by="claude-1", membership=3, head=SHA)
    assert line([other, verdict()]) == missing
    assert line([passing(membership=99), verdict()]) == missing
    old = ev(report={"pr_ci": "passing"}, by="lead-1", head=SHA)
    assert line([old, verdict()]) == missing


def test_the_coordinators_latest_ci_report_wins_and_needs_a_head_at_the_commit() -> None:
    """A later "failing" replaces "passing"; a passing report with no head, or at another commit, is not at
    this one. A report that carries no ci (threads only) does not replace the ci one."""
    missing = f"Not ready: CI not reported passing at {SHORT} by lead-1"
    failing = ev(report={"pr_ci": "failing"}, by="lead-1", membership=7, head=SHA)
    threads = ev(report={"pr_threads": 0}, by="lead-1", membership=7)
    assert line([passing(), failing, verdict()]) == missing
    assert line([passing(), threads, verdict()]) == f"Ready at {SHA[:12]}"
    assert line([ev(report={"pr_ci": "passing"}, by="lead-1", membership=7), verdict()]) == missing
    assert line([passing(head="1234567"), verdict(), ev(open="u", head=SHORT, by="x", how="moved")])


def test_a_verdict_at_an_older_commit_does_not_count_and_a_newer_one_replaces_an_older() -> None:
    """Only a verdict at the latest commit counts, and the agent's last one is the one read. Fails if an older
    verdict carries over a new commit."""
    newer = "1234567" + "8" * 33
    events = [
        passing(),
        verdict(head=SHORT),
        ev(report={"pr_ci": "passing"}, by="lead-1", membership=7, head=newer),
    ]
    assert "codex-1 has no verdict at 1234567" in line(events)
    assert [v["head"] for v in reviews.verdicts([verdict(head="1111111"), verdict(head=SHORT)])] == [SHORT]


def test_the_authors_side_verdict_does_not_count_and_is_not_asked_for() -> None:
    """The owner of a finding and the agent that posted the link are the author's side: a verdict from one is
    ignored, and one that raised a finding it owns is not waited on. Fails if the author can vouch."""
    assert line([passing(), verdict("claude-1")], [card(owner="claude-1")]) == (
        f"Not ready: codex-1 has no verdict at {SHORT}"
    )
    by_link = ev(open="u", head=SHORT, by="x", how="opened", author="claude-1")
    assert reviews.sure_side([], [by_link]) == frozenset({"claude-1"})
    # nobody but the author's side said anything: still no verdict
    assert line(
        [passing(), by_link, verdict("claude-1")], [card(raised_by="claude-1", owner="claude-1")]
    ) == (f"Not ready: no verdict at {SHORT}")


def test_a_reviewer_who_left_does_not_block_but_is_named_and_one_verdict_is_still_needed() -> None:
    """A raiser no longer in the room is a note. Without any verdict the board is not ready."""
    items = [card(raised_by="codex-2", n=2), card()]
    assert line([passing(), verdict()], items) == f"Ready at {SHA[:12]} (codex-2 left without a verdict)"
    gone = ("lead-1", "claude-1")
    assert line([passing()], items, gone) == (
        f"Not ready: no verdict at {SHORT} (codex-2 left without a verdict; codex-1 left without a verdict)"
    )


def test_a_long_list_is_cut_to_a_few_names() -> None:
    """Many unsettled cards read "and N more", so the line stays one line."""
    items = [card("raised", n=n) for n in range(1, 8)]
    got = line([passing(), verdict()], items)
    assert got.count(" is raised") == reviews.MAX_NAMED and got.endswith("and 3 more")


def test_a_board_told_no_commit_says_so() -> None:
    """With no commit there is nothing to be ready at."""
    got = reviews.ready({**board(""), "head": ""}, [], [], LEAD, ("codex-1",))
    assert got is not None and not got.ok and "no commit" in got.line
