"""The review board's rules (switchboard.reviews, DESIGN.md §37): pure, no broker."""

from __future__ import annotations

from dataclasses import replace

import pytest

from switchboard import reviews
from switchboard.reviews import Item, ReviewError


def finding(state: str = "raised", raised_by: str = "codex-1", owner: str = "", n: int = 1) -> Item:
    return Item(
        id=n, n=n, kind="finding", state=state, title="off by one", detail="", file="a.py", lines="3-4",
        raised_by=raised_by, owner=owner, commit="", reason="", options=(), recommend=None, answer="",
        answered_by="",
    )  # fmt: skip


def question(state: str = "open") -> Item:
    return replace(finding(), kind="question", state=state, options=("before tax", "after tax"), recommend=0)


def test_the_author_side_concedes_or_contests_and_the_raiser_only_drops() -> None:
    """Whoever raised a finding can't settle it in their own favour: conceding or contesting is
    the other side's call. The raiser may withdraw it."""
    f = finding()
    assert reviews.agent_move(f, "concede", "claude-1") == "conceded"
    assert reviews.agent_move(f, "contest", "claude-1") == "contested"
    for move in ("concede", "contest"):
        with pytest.raises(ReviewError, match="is yours"):
            reviews.agent_move(f, move, "codex-1")
    assert reviews.agent_move(f, "drop", "codex-1") == "dropped"
    with pytest.raises(ReviewError, match="who raised F1"):
        reviews.agent_move(f, "drop", "claude-1")


def test_only_the_owner_marks_a_conceded_finding_fixed() -> None:
    f = finding("conceded", owner="claude-1")
    assert reviews.agent_move(f, "fix", "claude-1") == "fixed"
    with pytest.raises(ReviewError, match="claude-1's to fix"):
        reviews.agent_move(f, "fix", "codex-1")
    with pytest.raises(ReviewError, match="can't be fixed from there"):
        reviews.agent_move(finding("raised"), "fix", "codex-1")


def test_a_contested_finding_can_still_be_conceded_but_not_contested_again() -> None:
    f = finding("contested")
    assert reviews.agent_move(f, "concede", "claude-1") == "conceded"
    with pytest.raises(ReviewError, match="contested: it can't be contested"):
        reviews.agent_move(f, "contest", "claude-1")


def test_a_question_is_a_persons_to_answer() -> None:
    """Agents ask; only a person answers or drops a question (the decisions agents can't make)."""
    q = question()
    for move in ("concede", "contest", "fix", "drop"):
        with pytest.raises(ReviewError, match="only a person"):
            reviews.agent_move(q, move, "claude-1")
    assert reviews.person_move(q, "answer") == "answered"
    assert reviews.person_move(q, "drop") == "dropped"
    with pytest.raises(ReviewError, match="can't be conceded"):
        reviews.person_move(q, "concede")
    with pytest.raises(ReviewError, match="can't be answered"):
        reviews.person_move(finding(), "answer")


def test_settled_means_nothing_left_to_do() -> None:
    assert reviews.settled([])
    assert reviews.settled([finding("fixed", owner="a"), finding("dropped"), question("answered")])
    for open_one in (finding("raised"), finding("contested"), finding("conceded"), question("open")):
        assert not reviews.settled([finding("fixed"), open_one]), open_one.state
    c = reviews.counts([finding("contested"), question(), finding("fixed"), finding("raised")])
    assert c == {"needs_person": 2, "open": 3, "fixed": 1, "dropped": 0, "answered": 0}


@pytest.mark.parametrize(
    "fn,value,msg",
    [
        (reviews.url, "javascript:alert(1)", "http"),
        (reviews.url, "https://example.com/pr/1 x", "http"),
        (reviews.url, "https://example.com/" + "a" * 500, "too long"),
        (reviews.head, "not-a-sha", "commit hash"),
        (reviews.lines, "40-12", "lower number"),
        (reviews.lines, "0", "a line or a range"),
        (reviews.options, ["only one"], "2 to 4"),
        (reviews.options, ["a", "a"], "differ"),
        (reviews.parse_label, "X1", "board label"),
    ],
)
def test_input_from_an_agent_is_checked(fn, value, msg) -> None:
    with pytest.raises(ReviewError, match=msg):
        fn(value)


def test_recommend_must_point_at_an_option() -> None:
    assert reviews.recommend(None, ("a", "b")) is None
    assert reviews.recommend(1, ("a", "b")) == 1
    for bad in (2, -1, True, "0"):
        with pytest.raises(ReviewError):
            reviews.recommend(bad, ("a", "b"))


def test_the_board_text_puts_open_work_first_and_stays_short() -> None:
    items = [
        finding("fixed", owner="claude-1", n=1),
        replace(finding("contested", n=2), title="rounding"),
        replace(question(), n=1, title="before or after tax?"),
    ]
    text = reviews.render({"url": "https://example.com/pr/7", "head": "a" * 40}, items)
    lines = text.splitlines()
    assert lines[0] == f"Review of https://example.com/pr/7 at {'a' * 12}: open"
    assert lines[1].startswith("F2 [contested] rounding") and "a person decides" in lines[1]
    assert lines[2].startswith("Q1 [open] before or after tax?") and "0. before tax (recommended)" in lines[2]
    assert lines[3].startswith("F1 [fixed]") and len(lines) == 4
