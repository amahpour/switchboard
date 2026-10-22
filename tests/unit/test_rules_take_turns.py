"""Taking turns on a person's message that names nobody (DESIGN.md §8.8, #507).

Measured in #501: a question that names nobody reaches every agent as ``to_you=yes``, and all three
real agents answered it in 10 of 10 trials. The first agent to say() an answer takes the reply; a later
say() that is only a second answer is held, with the first answer shown, and the agent decides.

The rule is pure (``rules.questions_answered``, ``rules.reply_taken``) and the engine gathers its inputs
(``Engine.check_reply_taken``); both are tested here without a broker, the second on a ``FakeClock``,
where "seen before it posted" is a matter of the order things happened in.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock
from engine_world import World

from switchboard.config import Config
from switchboard.delivery import rules
from switchboard.models import Answer, Item

NOW = 1_790_000_000.0


def item(
    mid: int,
    *,
    kind: str = "human",
    mentions: tuple[str, ...] = (),
    mentioned: bool = False,
    reply_to_me: bool = False,
) -> Item:
    return Item(
        membership_id=1,
        message_id=mid,
        prio=2 if kind == "human" else (1 if mentioned else 0),
        mentioned=mentioned,
        state="in_context",
        batch_id=None,
        attempts=0,
        notified_at=None,
        ts=NOW,
        sender_name="alice" if kind == "human" else "peer",
        sender_kind=kind,
        sender_harness=None,
        text="x",
        reply_to=None,
        mentions=mentions,
        reply_to_me=reply_to_me,
    )


def answer(
    mid: int,
    *,
    sender: str = "alpha",
    reply_to: int | None = None,
    mentions: tuple[str, ...] = (),
    ts: float = NOW + 5,
    seen_at: float | None = NOW + 1,
) -> Answer:
    return Answer(mid, sender, ts, reply_to, mentions, seen_at)


# ---------------------------------------------- which says are only a second answer (pure)
def test_a_person_message_that_names_nobody_is_a_question_and_a_named_one_is_not() -> None:
    """The bug: ``to_you`` is yes for every agent when nobody is named, so each thinks it is the one asked.
    A message that names someone (an @mention, ``@here``/``@everyone`` which name all) is a question
    for that someone and is never held; an agent's message is chatter, not a question."""
    assert rules.is_unaddressed_question(item(7))
    assert not rules.is_unaddressed_question(item(7, mentions=("bravo",)))
    assert not rules.is_unaddressed_question(item(7, mentions=("everyone",)))
    assert not rules.is_unaddressed_question(item(7, kind="agent"))


def test_a_reply_to_the_question_answers_it_and_a_reply_to_anything_else_does_not() -> None:
    """A say with ``reply_to`` is held only when it replies to the question: a reply to a peer's message,
    or to a question that named someone else, is a different conversation."""
    ctx = [item(7), item(8, mentions=("bravo",)), item(9, kind="agent")]
    assert [q.message_id for q in rules.questions_answered(ctx, 7)] == [7]
    assert rules.questions_answered(ctx, 8) == []  # it named someone
    assert rules.questions_answered(ctx, 9) == []  # a peer's message
    assert rules.questions_answered(ctx, 99) == []  # not in its context at all


def test_a_say_that_replies_to_nothing_answers_every_open_question() -> None:
    ctx = [item(7), item(9), item(10, kind="agent")]
    assert [q.message_id for q in rules.questions_answered(ctx, None)] == [7, 9]


def test_a_say_is_not_held_while_something_addressed_to_the_sayer_waits() -> None:
    """Fail toward posting: an @mention of the sayer, or a reply to its own message, is more likely what
    an unthreaded say answers than the question is. Holding it would block a reply to a peer."""
    ctx = [item(7), item(8, kind="agent", mentioned=True)]
    assert rules.questions_answered(ctx, None) == []
    ctx = [item(7), item(8, kind="agent", reply_to_me=True)]
    assert rules.questions_answered(ctx, None) == []
    assert [q.message_id for q in rules.questions_answered(ctx, 7)] == [7]  # but a reply to the question is


def test_nothing_in_context_is_nothing_to_hold() -> None:
    assert rules.questions_answered([], None) == []
    assert rules.questions_answered([item(9, kind="agent")], None) == []


# ----------------------------------------------------- which post took the reply (pure)
def test_the_first_answer_after_the_question_takes_the_reply() -> None:
    q = item(7)
    early, late = answer(8, sender="alpha"), answer(9, sender="bravo")
    assert rules.reply_taken(q, "charlie", [late, early]) == early  # the oldest, whatever the order given
    assert rules.reply_taken(q, "charlie", []) is None


def test_a_post_before_the_question_or_for_another_message_is_not_an_answer() -> None:
    """An answer is after the question and replies to it or to nothing. A reply to a peer's message is
    part of another conversation, even from an agent that had seen the question."""
    q = item(7)
    assert rules.reply_taken(q, "charlie", [answer(6)]) is None
    assert rules.reply_taken(q, "charlie", [answer(8, reply_to=3)]) is None
    assert rules.reply_taken(q, "charlie", [answer(8, reply_to=7)]) is not None
    assert rules.reply_taken(q, "charlie", [answer(8, reply_to=None)]) is not None


def test_a_post_by_an_author_who_had_not_seen_the_question_is_not_an_answer() -> None:
    """The author's own delivery of the question went into its context at ``seen_at``. A post made
    before that, or by an author that never had it in context, was about something else."""
    q = item(7)
    assert rules.reply_taken(q, "charlie", [answer(8, seen_at=None)]) is None
    assert rules.reply_taken(q, "charlie", [answer(8, ts=NOW + 5, seen_at=NOW + 6)]) is None
    assert rules.reply_taken(q, "charlie", [answer(8, ts=NOW + 5, seen_at=NOW + 5)]) is not None  # same instant


def test_a_post_that_mentions_the_sayer_is_a_hand_off_not_a_reply_to_defer_to() -> None:
    q = item(7)
    handoff = answer(8, mentions=("charlie",))
    assert rules.reply_taken(q, "charlie", [handoff]) is None
    assert rules.reply_taken(q, "Charlie", [handoff]) is None  # names compare without case
    assert rules.reply_taken(q, "bravo", [handoff]) is not None  # to anyone else it is the answer
    # a hand-off does not hide a later, real answer
    assert rules.reply_taken(q, "charlie", [handoff, answer(9, sender="bravo")]).sender == "bravo"  # type: ignore[union-attr]


def test_the_rule_is_a_function_of_its_arguments() -> None:
    q, a = item(7), answer(8)
    assert rules.reply_taken(q, "bravo", [a]) == rules.reply_taken(q, "bravo", [a])
    assert dataclasses.replace(a, sender="x").sender == "x"  # frozen values, nothing shared


# ------------------------------------- the engine gathers the inputs (store + FakeClock, no broker)
@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def room_of_three(w: World) -> dict[str, tuple[Any, Any]]:
    return {n: w.agent(n, status="idle") for n in ("alpha", "bravo", "charlie")}


def reads(w: World, who: tuple[Any, Any]) -> None:
    """The agent read() the room and the answer reached its model: its deliveries are in context now."""
    p, m = who
    _text, bid, _n, _more, acts = w.engine.pull(w.p(p), w.m(m), "read", 50)
    w.actions += acts
    w.actions += w.engine.on_confirm(bid, "next_call")


def held(w: World, who: tuple[Any, Any], reply_to: int | None) -> Any:
    return w.engine.check_reply_taken(w.p(who[0]), w.m(who[1]), reply_to)


def test_the_second_agent_to_answer_is_told_who_took_it(w: World, clock: FakeClock) -> None:
    """The scene of #501: a question that names nobody, three agents read it, and alpha answers first.
    bravo's and charlie's says are then held, naming alpha's post; alpha's own say is not."""
    ag = room_of_three(w)
    q = w.human("Quick question for the room: what is 17 plus 25?")
    for who in ag.values():
        reads(w, who)
    assert held(w, ag["bravo"], q.id) is None  # nobody has answered yet: bravo may
    clock.advance(2.0)
    first = w.agent_says(ag["alpha"][1], "42", reply_to=q.id)
    clock.advance(1.0)
    for name in ("bravo", "charlie"):
        got = held(w, ag[name], q.id)
        assert got is not None, name
        question, taken = got
        assert question.message_id == q.id
        assert (taken.message_id, taken.sender) == (first.id, "alpha")
    assert held(w, ag["alpha"], q.id) is None  # its own post is not a reason to hold it
    evs = w.store.recent_events(kinds=("reply_taken",))
    assert sorted(e.membership_id for e in evs) == sorted(ag[n][1].id for n in ("bravo", "charlie"))
    assert all(e.data == {"question": q.id, "answer": first.id, "by": "alpha"} for e in evs)


def test_nothing_is_held_before_an_answer_or_after_the_question_named_someone(w: World) -> None:
    ag = room_of_three(w)
    named = w.human("@bravo what is 12 times 12?", mentions=("bravo",))
    for who in ag.values():
        reads(w, who)
    w.agent_says(ag["bravo"][1], "144", reply_to=named.id)
    assert held(w, ag["alpha"], named.id) is None and held(w, ag["alpha"], None) is None
    assert w.store.count_events("reply_taken") == 0


def test_a_post_made_before_the_author_read_the_question_does_not_take_it(
    w: World, clock: FakeClock
) -> None:
    """alpha posted something else while the question was still unread by it, and read it afterwards:
    its post is not an answer, so bravo's say is free."""
    ag = room_of_three(w)
    q = w.human("what is 17 plus 25?")
    reads(w, ag["bravo"])
    clock.advance(1.0)
    w.agent_says(ag["alpha"][1], "status: still building")  # before alpha had the question
    clock.advance(1.0)
    reads(w, ag["alpha"])
    assert held(w, ag["bravo"], q.id) is None and held(w, ag["bravo"], None) is None


def test_a_reply_to_a_peer_is_not_held_even_after_an_answer(w: World, clock: FakeClock) -> None:
    ag = room_of_three(w)
    q = w.human("what is 17 plus 25?")
    for who in ag.values():
        reads(w, who)
    clock.advance(1.0)
    first = w.agent_says(ag["alpha"][1], "42", reply_to=q.id)
    assert held(w, ag["bravo"], first.id) is None  # a reply to alpha's own message
    assert held(w, ag["bravo"], q.id) is not None and held(w, ag["bravo"], None) is not None


def test_a_member_that_already_answered_or_passed_has_no_open_question(w: World, clock: FakeClock) -> None:
    """Once a say or pass handled the question it is no longer in the member's context, so a later,
    unrelated say of the same member is never held on its account."""
    ag = room_of_three(w)
    q = w.human("what is 17 plus 25?")
    for who in ag.values():
        reads(w, who)
    clock.advance(1.0)
    w.agent_says(ag["alpha"][1], "42", reply_to=q.id)
    w.store.mark_handled(ag["bravo"][1].id)  # bravo passed
    assert held(w, ag["bravo"], None) is None


def test_the_claim_is_per_question(w: World, clock: FakeClock) -> None:
    """alpha answered the first question; a second one, asked later, is a new race."""
    ag = room_of_three(w)
    first_q = w.human("what is 17 plus 25?")
    for who in ag.values():
        reads(w, who)
    clock.advance(1.0)
    w.agent_says(ag["alpha"][1], "42", reply_to=first_q.id)
    for n in ("alpha", "bravo", "charlie"):
        w.store.mark_handled(ag[n][1].id)
    clock.advance(1.0)
    second_q = w.human("and 3 times 3?")
    for who in ag.values():
        reads(w, who)
    for n in ("alpha", "bravo", "charlie"):
        assert held(w, ag[n], second_q.id) is None, n
