"""One line for a burst of fixes (#984, DESIGN.md §37.20), as pure rules: when a held group is due, what the
one line says, and what a group's text round-trips to. Times come from a ``FakeClock``; nothing sleeps."""

from __future__ import annotations

from conftest import FakeClock

from switchboard import reviews

POSTER = {"sender_name": "claude-1", "sender_kind": "agent"}


def card(
    n: int, state: str = "fixed", commit: str = "abcdef0123456789", confirmed_by: str = ""
) -> reviews.Item:
    return reviews.Item(
        id=n,
        n=n,
        kind="finding",
        state=state,
        title=f"problem {n}",
        detail="",
        file="",
        lines="",
        raised_by="codex-1",
        owner="claude-1",
        commit=commit,
        reason="",
        options=(),
        recommend=None,
        answer="",
        answered_by="",
        confirmed_by=confirmed_by,
    )


def group(*labels: str, who: tuple[str, ...] = ("codex-1",)) -> dict:
    return {"who": list(who), "fixes": list(labels), "first": 0.0, "last": 0.0, "poster": POSTER}


def test_fixes_marked_close_together_are_one_group_that_goes_out_once_it_is_quiet() -> None:
    """Three fixes 10 s apart make one group for the reviewer, due 30 s after the last, not before. Fails if
    each fix is its own group, or the wait does not restart on a new fix."""
    clock = FakeClock()
    groups: list[dict] = []
    for label in ("F3", "F5", "F6"):
        groups = reviews.hold_fix(groups, ["codex-1"], label, POSTER, clock.now())
        clock.advance(10)
    [g] = groups
    assert g["fixes"] == ["F3", "F5", "F6"] and g["first"] < g["last"]
    clock.advance(reviews.FIX_QUIET_S - 10 - 1)  # 29 s after the last fix
    assert not reviews.fixes_due(g, clock.now())
    clock.advance(1)
    assert reviews.fixes_due(g, clock.now())


def test_a_single_fix_is_due_after_the_short_wait() -> None:
    """One fix waits the quiet 30 s and no longer. Fails if a lone fix is held for the long cap."""
    clock = FakeClock()
    [g] = reviews.hold_fix([], ["codex-1"], "F1", POSTER, clock.now())
    clock.advance(reviews.FIX_QUIET_S - 1)
    assert not reviews.fixes_due(g, clock.now())
    clock.advance(1)
    assert reviews.fixes_due(g, clock.now())


def test_a_steady_stream_of_fixes_is_cut_at_the_longest_wait() -> None:
    """A fix every 20 s never goes quiet, but the group is due 120 s after its first. Fails if the wait has
    no ceiling, so a reviewer could wait for a long run to end."""
    clock = FakeClock()
    groups: list[dict] = []
    for n in range(1, 7):  # at 0, 20, ... 100 s
        groups = reviews.hold_fix(groups, ["codex-1"], f"F{n}", POSTER, clock.now())
        assert not reviews.fixes_due(groups[0], clock.now())
        clock.advance(20)
    assert reviews.fixes_due(groups[0], clock.now())  # 120 s after the first, 20 after the last


def test_fixes_for_other_reviewers_are_other_groups_and_a_revised_fix_is_listed_once() -> None:
    """The group is the reviewers a line would mention: a fix for bob is not folded into one for codex-1,
    and a card fixed again is not named twice. Fails if reviewers share a line."""
    groups = reviews.hold_fix([], ["codex-1"], "F1", POSTER, 1.0)
    groups = reviews.hold_fix(groups, ["bob"], "F2", POSTER, 2.0)
    groups = reviews.hold_fix(groups, ["codex-1"], "F1", POSTER, 3.0)
    assert [(g["who"], g["fixes"]) for g in groups] == [(["codex-1"], ["F1"]), (["bob"], ["F2"])]


def test_the_line_for_one_fix_reads_as_it_always_did() -> None:
    """One fix is worded as ``told`` words it. Fails if the single line changes."""
    one = reviews.fixes_told(group("F3"), [card(3)], ["claude-1", "codex-1"])
    assert one is not None
    assert one.line == (
        "Review board: F3 fixed in abcdef012345. @codex-1: check the fix."
        ' review(action="show") shows the board.'
    )
    cards = [card(3)]
    told = reviews.told(cards[0], "claude-1", ["claude-1", "codex-1"], action="fix", side=("claude-1",))
    assert told is not None and told.line == one.line


def test_the_line_for_several_fixes_names_each_and_mentions_the_reviewer_once() -> None:
    """Three fixes are one line: every card, one commit, one mention, "fixes". Fails if a card or the
    mention is repeated or left out."""
    told = reviews.fixes_told(group("F3", "F5", "F6"), [card(3), card(5), card(6)], ["claude-1", "codex-1"])
    assert told is not None
    assert told.body == "F3, F5 and F6 fixed in abcdef012345. @codex-1: check the fixes."
    assert told.line.count("@codex-1") == 1 and told.labels == ("F3", "F5", "F6") and told.who == ("codex-1",)


def test_fixes_in_two_commits_say_which_is_which() -> None:
    """Fixes pushed as two commits keep each one's. Fails if the line names one commit for all."""
    cards = [card(3, commit="aaaaaaaa1"), card(5, commit="aaaaaaaa1"), card(6, commit="bbbbbbbb2")]
    told = reviews.fixes_told(group("F3", "F5", "F6"), cards, ["codex-1"])
    assert told is not None
    assert told.body == "F3 and F5 fixed in aaaaaaaa1, F6 in bbbbbbbb2. @codex-1: check the fixes."


def test_a_card_that_moved_on_while_held_is_left_out_and_nobody_left_is_no_line() -> None:
    """A card the reviewer confirmed, or that was reopened or dropped, is not in the line; with none left, or
    the reviewer gone from the room, there is no line. Fails if the line asks for a check nobody owes."""
    cards = [card(3), card(5, state="contested"), card(6, confirmed_by="codex-1")]
    told = reviews.fixes_told(group("F3", "F5", "F6"), cards, ["codex-1"])
    assert told is not None and told.body == "F3 fixed in abcdef012345. @codex-1: check the fix."
    assert reviews.fixes_told(group("F5", "F6"), cards, ["codex-1"]) is None
    assert reviews.fixes_told(group("F3"), cards, ["claude-1"]) is None


def test_a_board_that_names_its_url_says_so_in_the_line() -> None:
    """With several boards open the line names its board and every call it suggests carries the url."""
    told = reviews.fixes_told(
        group("F3", "F5"), [card(3), card(5)], ["codex-1"], "https://example.com/a/pull/7"
    )
    assert told is not None
    assert told.line.startswith("Review board https://example.com/a/pull/7: F3 and F5 fixed in")
    assert 'review(action="show", url="https://example.com/a/pull/7")' in told.line


def test_held_text_round_trips_and_what_is_not_a_group_is_left_out() -> None:
    """The column's text reads back to the groups; garbage reads as nothing, never an error. Fails if a bad
    row can stop a board or the tick."""
    groups = reviews.hold_fix([], ["codex-1"], "F1", POSTER, 1.0)
    assert reviews.parse_held(reviews.dump_held(groups)) == groups
    assert reviews.dump_held([]) == ""
    for bad in ("", "nope", "{}", "[1]", '[{"who":[],"fixes":["F1"],"first":1,"last":1,"poster":{}}]'):
        assert reviews.parse_held(bad) == []
