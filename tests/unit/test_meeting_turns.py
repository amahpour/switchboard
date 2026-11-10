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


def test_a_second_line_is_refused_while_one_waits_but_may_wait_while_one_is_spoken(turns) -> None:
    """The slot holds one waiting line. A line being spoken no longer shuts the next one out (#713): two
    agents answering the same question would otherwise lose the second. Fails if a spoken line still
    refuses a new offer, or if two lines may wait at once."""
    assert turns.offer("first", 0, 1)
    assert not turns.offer("second", 0, 2)
    assert turns.refusal == "busy"
    first = turns.take(0)
    assert first is not None
    assert turns.offer("third", 1, 2)
    assert not turns.offer("fourth", 1, 3)
    assert turns.take(59) is None  # the first is still being spoken: no overlap
    assert not turns.finish(first.id + 5)
    assert turns.take(59) is None
    assert turns.finish(first.id)
    assert turns.take(59).text == "third"


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
    assert turns.offer("second", 40)
    assert turns.take(40) is None  # the first line has not ended
    turns.finish(first.id)
    assert turns.take(40).text == "second"


def test_a_line_whose_end_is_never_reported_frees_the_slot_after_ninety_seconds(turns) -> None:
    """A relay that crashed mid-line, or lost its receipt, must not silence the call for good. A 600-character
    line is under a minute, so 90 s from its start is past any real line. Fails if the slot stays taken."""
    turns.offer("first", 0)
    assert turns.take(0) is not None
    assert turns.offer("second", 60, 2)
    assert turns.take(89.9) is None
    assert turns.ready_at() == 90
    assert turns.take(90).text == "second"


def test_an_answer_to_a_question_may_start_two_seconds_after_the_last_start(turns) -> None:
    """A line the broker marks as answering a question put to its speaker waits 2 s from the last start,
    not 30 s (the maintainer's exception, #713). Fails if it still waits 30 s, or starts earlier than 2 s."""
    turns.offer("first", 0, 1)
    first = turns.take(0)
    turns.finish(first.id)
    assert turns.offer("reply", 0.5, 2, answering=True)
    assert turns.take(1.99) is None
    assert turns.take(2).text == "reply"


def test_an_answer_still_waits_for_a_gap_in_the_speech_on_the_call(turns) -> None:
    """The 2 s rule shortens the interval only. A person talking still holds a line for 1.5 s."""
    turns.hear(10)
    turns.offer("reply", 10, 2, answering=True)
    assert turns.take(11.49) is None
    assert turns.take(11.5).text == "reply"


def test_a_line_that_answers_nothing_waits_thirty_seconds_from_its_own_agents_last_start(turns) -> None:
    """The 30 s is each agent's own (#713): another agent's start does not hold this one's first line, and
    the same agent's second line still waits. Fails if the interval is still the whole call's."""
    turns.offer("one", 0, 1)
    one = turns.take(0)
    turns.finish(one.id)
    turns.offer("other agent", 3, 2)
    assert turns.take(3).text == "other agent"  # a different agent, no start of its own yet
    turns.finish(2)
    turns.offer("one again", 4, 1)
    assert turns.take(29.99) is None
    assert turns.take(30).text == "one again"


def test_the_two_second_door_opens_only_for_a_line_the_broker_marked(turns) -> None:
    """Only the broker's mark opens the 2 s door. The same line without it waits as any other."""
    turns.offer("one", 0, 1)
    turns.finish(turns.take(0).id)
    turns.offer("again", 1, 1)
    assert turns.take(5) is None


def test_four_agent_lines_in_a_row_are_the_most_until_a_person_speaks_on_the_call(turns) -> None:
    """A voice loop of agents answering each other must stop (#713). The fifth offer is refused with the
    chain reason; a participant segment ends the run. Fails if the run is not bounded, or never resets."""
    for i in range(4):
        assert turns.offer(f"line {i}", i * 40, i % 2 + 1)
        turns.finish(turns.take(i * 40).id)
    assert not turns.offer("fifth", 200, 1)
    assert turns.refusal == "chain"
    turns.hear(201)
    assert turns.offer("after a person", 201, 1)


def test_a_person_writing_in_the_room_also_ends_the_run_of_agent_lines(turns) -> None:
    """A person types instead of speaking: the run still ends. Fails if only call speech resets it."""
    for i in range(4):
        turns.offer(f"line {i}", i * 40, 1)
        turns.finish(turns.take(i * 40).id)
    assert not turns.offer("blocked", 200, 1)
    turns.person_posted()
    assert turns.offer("allowed", 200, 1)


def test_the_run_counts_a_refusal_for_a_waiting_line_as_busy_not_as_a_line(turns) -> None:
    """A line refused because one is waiting is not a line in the run: only accepted lines count."""
    for _ in range(10):
        turns.offer("x", 0, 1)
    assert turns.chain == 1
    assert turns.refusal == "busy"


def test_voice_off_drops_the_line_waiting_behind_a_spoken_one(turns) -> None:
    """Turning Voice off clears the waiting line even while another is being spoken (#713). Fails if the
    queued line survives and is spoken after the person switched voice off."""
    turns.offer("speaking", 0, 1)
    first = turns.take(0)
    turns.offer("waiting", 1, 2)
    turns.clear_pending()
    turns.finish(first.id)
    assert turns.take(100) is None
    assert turns.pending is None
