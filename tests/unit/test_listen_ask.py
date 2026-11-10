"""A person's question on the call wakes a listener within seconds (DESIGN.md §52.1, #697 phase 4).

The fast path is pure policy (``rules.asks``, ``chatter_due``, ``chatter_wakes_at``) driven by the
synchronous engine with a ``FakeClock``: nothing sleeps and nothing does I/O.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock
from engine_agents import result, waiting, woke
from test_listen_mode import call_world, line

from switchboard.delivery.rules import asks


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Can you list the tests?", True),
        ("Can you list the tests?   ", True),
        ('He asked, "is it admins only?"', True),
        ("Is it done (roughly)?)", True),
        ("Is it done [or not?]", True),
        ("Is it done?”", True),
        ("Is it done? I think so.", False),
        ("That is the plan.", False),
        ("What? Never mind", False),
        ("", False),
        ("?", True),
    ],
)
def test_asks_looks_only_at_the_end_of_the_line(text: str, expected: bool) -> None:
    """The mark has to end the line (after trailing spaces, closing quotes and brackets); a question
    mark in the middle of a statement must not make the agent answer in three seconds."""
    assert asks(text) is expected


def test_a_question_wakes_after_listen_ask_s_and_a_statement_waits_for_listen_quiet_s(
    tmp_path: Path, clock: FakeClock
) -> None:
    """Without the fast path both lines would wait 15 s; with it the question goes at 3 s."""
    w, call = call_world(tmp_path, clock, quiet_s=0, max_hold_s=1)
    _, m, sink = waiting(w)
    w.store.set_listen(m.id, True, call)
    line(w, call, "Where does the export code live?")
    assert w.engine.wakes_at(m.id) == clock.now() + 3
    clock.advance(2)
    w.actions += w.engine.tick()
    assert not woke(w, sink)
    clock.advance(1)
    w.actions += w.engine.tick()
    assert woke(w, sink) and "export code" in result(w, sink)["text"]

    (tmp_path / "b").mkdir()
    w2, call2 = call_world(tmp_path / "b", clock, quiet_s=0, max_hold_s=1)
    _, m2, sink2 = waiting(w2)
    w2.store.set_listen(m2.id, True, call2)
    line(w2, call2, "The export code lives in the handler.")
    assert w2.engine.wakes_at(m2.id) == clock.now() + 15
    clock.advance(14)
    w2.actions += w2.engine.tick()
    assert not woke(w2, sink2)


def test_the_ask_clock_is_room_quiet_since_the_latest_line(tmp_path: Path, clock: FakeClock) -> None:
    """More talk after the question restarts the 3 s: the person is still speaking."""
    w, call = call_world(tmp_path, clock)
    _, m, sink = waiting(w)
    w.store.set_listen(m.id, True, call)
    line(w, call, "Is this ready?")
    clock.advance(2)
    line(w, call, "Well, I mean the second half.")
    clock.advance(2)
    w.actions += w.engine.tick()
    assert not woke(w, sink)
    # the question is still pending, so the short clock now runs from the later line
    assert w.engine.wakes_at(m.id) == clock.now() + 1
    clock.advance(1)
    w.actions += w.engine.tick()
    assert woke(w, sink)


def test_only_a_call_line_from_the_listening_call_takes_the_fast_path(
    tmp_path: Path, clock: FakeClock
) -> None:
    """A peer agent's chat message that ends in "?" keeps its ordinary timing, and so does a question
    from an earlier call: only the listened-to call's lines are fast."""
    w, call = call_world(tmp_path, clock, quiet_s=60, max_hold_s=60)
    _, m, sink = waiting(w)
    line(w, call, "Did anyone merge it?")
    w.store.end_meeting(call, None)
    new = w.store.insert_meeting(w.room.id, started_by="alice", platform="zoom")
    w.store.set_meeting_live(new.id)
    w.store.set_listen(m.id, True, new.id)
    msg = w.store.insert_message(
        w.room.id, sender_name="beta", sender_kind="agent", via="mcp", text="Is the build green?"
    )
    w.actions += w.engine.on_message(msg.id)
    clock.advance(5)
    w.actions += w.engine.tick()
    assert not woke(w, sink)


def test_a_question_spends_the_listening_cap_and_the_room_budget(tmp_path: Path, clock: FakeClock) -> None:
    """Fast does not mean free: the wake counts against the per-call cap, whose last one switches
    listening off, and against the room's hourly budget."""
    w, call = call_world(tmp_path, clock, listen_max_wakes=1)
    _, m, sink = waiting(w)
    w.store.set_listen(m.id, True, call)
    before = w.store.room_by_id(w.room.id).budget_remaining
    line(w, call, "Can you hear the question?")
    clock.advance(3)
    w.actions += w.engine.tick()
    assert woke(w, sink)
    assert w.m(m).listen_wakes == 1 and not w.m(m).listen
    assert w.store.room_by_id(w.room.id).budget_remaining == before - 1


def test_a_question_does_not_pass_the_room_budget(tmp_path: Path, clock: FakeClock) -> None:
    """With no wakes left in the hour, a question waits like any listening wake."""
    w, call = call_world(tmp_path, clock, budget_per_hour=0)
    _, m, sink = waiting(w)
    w.store.set_listen(m.id, True, call)
    line(w, call, "Is anyone there?")
    clock.advance(30)
    w.actions += w.engine.tick()
    assert not woke(w, sink)


def test_a_one_to_one_conversation_gets_its_answer_in_seconds(tmp_path: Path) -> None:
    """Replay alice and one agent (tests/fixtures/calls/one_to_one.json): each of her questions wakes
    the agent within about 4 s with the default, and with the fast path set equal to the quiet time (the
    behavior before it existed) each waits 15 s or more. The second half is what makes the first count."""
    from listen_replay import CONVERSATION, replay, script

    questions = [u for u in script(fixture=CONVERSATION) if u.tag.startswith("q")]
    assert len(questions) >= 8
    (tmp_path / "fast").mkdir()
    (tmp_path / "slow").mkdir()
    fast = replay(15, 40, fixture=CONVERSATION, tmp=tmp_path / "fast")
    slow = replay(15, 40, fixture=CONVERSATION, ask_s=15, tmp=tmp_path / "slow")
    for u in questions:
        assert fast.delay_for(u.tag) is not None and fast.delay_for(u.tag) <= 4.5
        assert slow.delay_for(u.tag) is not None and slow.delay_for(u.tag) >= 15
    assert fast.n_wakes <= 40
