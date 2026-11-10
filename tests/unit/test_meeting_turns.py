"""One spoken turn per call, held for a gap and never spoken stale (DESIGN.md §52.2)."""

from __future__ import annotations

import pytest


@pytest.fixture
def turns():
    from switchboard.meeting.turns import Turns

    return Turns()


def test_recent_speech_holds_a_line_for_one_and_a_half_seconds(turns) -> None:
    """Starting speech during a person's last segment would interrupt them."""
    turns.hear(10)
    assert turns.offer("answer", 10, 7)
    assert turns.take(11.49) is None
    said = turns.take(11.5)
    assert said is not None and said.text == "answer" and said.membership_id == 7


def test_new_speech_moves_the_gap_but_not_the_lines_expiry(turns) -> None:
    """Continued speech can delay an answer, but cannot keep an old answer alive forever."""
    turns.hear(0)
    turns.offer("answer", 0)
    for now in range(1, 60):
        turns.hear(now)
        assert turns.take(now) is None
    assert turns.take(60) is None
    assert turns.expire(60).text == "answer"
    assert turns.pending is None


def test_a_second_line_is_refused_while_queued_and_while_spoken(turns) -> None:
    """Two agents may answer the same batch; the call has one slot, including speech in progress."""
    assert turns.offer("first", 0)
    assert not turns.offer("second", 0)
    first = turns.take(0)
    assert first is not None
    assert not turns.offer("third", 100)
    assert not turns.finish(first.id + 1)
    assert not turns.offer("fourth", 100)
    assert turns.finish(first.id)
    assert turns.offer("next", 100)


def test_the_next_line_waits_thirty_seconds_from_the_previous_start(turns) -> None:
    """Finishing early never buys another start within the per-call interval."""
    turns.offer("first", 0)
    first = turns.take(0)
    turns.finish(first.id)
    assert turns.offer("next", 1)
    assert turns.take(29.99) is None
    assert turns.take(30).text == "next"


def test_switching_voice_off_drops_the_wait_but_preserves_the_interval(turns) -> None:
    turns.offer("first", 0)
    first = turns.take(0)
    turns.finish(first.id)
    turns.offer("drop", 1)
    turns.clear_pending()
    turns.offer("next", 2)
    assert turns.take(29) is None
    assert turns.take(30).text == "next"


def test_a_line_expires_once_and_a_new_call_has_no_old_interval(turns) -> None:
    from switchboard.meeting.turns import Turns

    turns.offer("too late", 0)
    assert turns.expire(59.99) is None
    assert turns.expire(60).text == "too late"
    assert turns.expire(61) is None
    fresh = Turns()
    fresh.offer("new call", 61)
    assert fresh.take(61).text == "new call"


def test_voice_off_never_frees_a_line_that_is_still_being_spoken(turns) -> None:
    """Clearing a wait is not evidence that the current speaker has finished."""
    turns.offer("still speaking", 0)
    first = turns.take(0)
    turns.clear_pending()
    assert not turns.offer("second", 40)
    turns.finish(first.id)
    assert turns.offer("next", 40)
