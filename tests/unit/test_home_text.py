"""The one line a Home card shows for an agent's message (#626, DESIGN.md §55): ``broker/home.py``'s
``_first_line`` and ``_question_text``. A card is one line, so the line has to say something."""

from __future__ import annotations

import pytest

from switchboard.broker.home import _first_line, _question_text


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        # a heading says nothing alone: it is joined to the first words under it
        (
            "## Open questions\n\n1. Keep the v1 endpoint?\n2. Drop the flag?",
            "Open questions: Keep the v1 endpoint?",
        ),
        # a lead-in that ends with a colon is joined to what it introduces
        (
            "Two issues:\n- the upload limit is too low\n- the old path is dead",
            "Two issues: the upload limit is too low",
        ),
        (
            "Done in `.worktrees/claude-1`:\n\nraised the limit to 25 MB",
            "Done in .worktrees/claude-1: raised the limit to 25 MB",
        ),
        # a plain first line is left alone, and so is a lead-in with nothing after it
        ("raised the upload limit\nsecond line", "raised the upload limit"),
        ("Two issues:", "Two issues:"),
    ],
)
def test_a_heading_or_a_lead_in_is_joined_to_the_line_under_it(raw: str, shown: str) -> None:
    """Fails if a card says only "Open questions", "Two issues:" or "Done in ...:", which tells the
    reader nothing."""
    assert _first_line(raw) == shown


def test_the_joined_line_is_still_cut_to_the_limit() -> None:
    """The join happens before the cut, so a long second line cannot make the card longer than its limit."""
    out = _first_line("Two issues:\n" + "x" * 500, 60)
    assert len(out) == 60 and out.startswith("Two issues: xxx") and out.endswith("…")


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("@alice Keep the old upload endpoint, or remove it?", "Keep the old upload endpoint, or remove it?"),
        ("@alice, @humans: which limit?", "which limit?"),
        ("@humans ship it?", "ship it?"),
        # only the viewer's and everyone's mention goes, and only at the start
        ("@bob @alice take a look", "@bob @alice take a look"),
        ("keep it, @alice?", "keep it, @alice?"),
        # a message that is only the mention keeps it: the card would be empty otherwise
        ("@alice", "@alice"),
    ],
)
def test_the_card_does_not_repeat_the_viewers_own_mention(raw: str, shown: str) -> None:
    """The card already says it is for you. Fails if it opens with your own @name or @humans, or if it
    takes out
    another person's mention or one in the middle of the line."""
    assert _question_text(raw, "alice") == shown
