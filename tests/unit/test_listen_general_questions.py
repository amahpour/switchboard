"""Who may answer a question addressed to nobody in particular on a call (#761, DESIGN.md §52.1).

One listener in the room answers it, as well as anything about its own work; with several, each
speaks only about its own work, so they do not all answer at once. A spoken name still wins.
"""

from __future__ import annotations

from pathlib import Path

from conftest import FakeClock
from engine_agents import result, waiting, woke
from test_listen_mode import call_world, line

from switchboard.delivery.rules import LISTEN_ALONE, LISTEN_NAMED, LISTEN_SHARED, listen_guidance

QUESTION = "alice: What is the capital of California?"


def test_guidance_depends_on_how_many_are_listening() -> None:
    """The pure rule: one listener may answer a general question, several may not, a name always wins."""
    assert listen_guidance(1).startswith(LISTEN_ALONE) and listen_guidance(1).endswith(LISTEN_NAMED)
    assert listen_guidance(2).startswith(LISTEN_SHARED) and listen_guidance(2).endswith(LISTEN_NAMED)
    assert listen_guidance(0) == listen_guidance(1)
    assert "nobody in particular" not in listen_guidance(2)


def test_the_only_listener_may_answer_a_general_question(tmp_path: Path, clock: FakeClock) -> None:
    """With one listener, its call lines come with the guidance that lets it answer anyone's question.

    Without it the agent stays quiet on a question that is not about its own work."""
    w, call = call_world(tmp_path, clock)
    _, m, sink = waiting(w, "alice-bot")
    _, _, other = waiting(w, "bob-bot")  # in the room, not listening
    w.store.set_listen(m.id, True, call)
    line(w, call, QUESTION)
    clock.advance(3)
    w.actions += w.engine.tick()
    assert woke(w, sink) and not woke(w, other)
    text = result(w, sink)["text"]
    assert LISTEN_ALONE in text and LISTEN_SHARED not in text


def test_with_several_listeners_each_speaks_only_about_its_own_work(tmp_path: Path, clock: FakeClock) -> None:
    """Two listeners both hear the question, and neither is told it may answer it."""
    w, call = call_world(tmp_path, clock)
    _, a, sa = waiting(w, "alice-bot")
    _, b, sb = waiting(w, "bob-bot")
    for m in (a, b):
        w.store.set_listen(m.id, True, call)
    line(w, call, QUESTION)
    clock.advance(3)
    w.actions += w.engine.tick()
    for sink in (sa, sb):
        text = result(w, sink)["text"]
        assert LISTEN_SHARED in text and LISTEN_ALONE not in text


def test_a_listener_that_switches_off_leaves_the_other_alone(tmp_path: Path, clock: FakeClock) -> None:
    """The count is of agents listening now: when one stops, the other is the only listener again."""
    w, call = call_world(tmp_path, clock)
    _, a, sa = waiting(w, "alice-bot")
    _, b, _ = waiting(w, "bob-bot")
    for m in (a, b):
        w.store.set_listen(m.id, True, call)
    w.store.set_listen(b.id, False, call)
    line(w, call, QUESTION)
    clock.advance(3)
    w.actions += w.engine.tick()
    assert LISTEN_ALONE in result(w, sa)["text"]


def test_a_named_listener_is_woken_at_once_and_told_the_name_wins(tmp_path: Path, clock: FakeClock) -> None:
    """A line that names one of two listeners wakes it without the quiet wait, and not the other."""
    w, call = call_world(tmp_path, clock)
    _, a, sa = waiting(w, "alice-bot")
    _, b, sb = waiting(w, "bob-bot")
    for m in (a, b):
        w.store.set_listen(m.id, True, call)
    msg = w.store.insert_message(
        w.room.id,
        sender_name="call",
        sender_kind="meeting",
        via="meeting",
        text="alice: bob-bot, what is the capital of California?",
        meeting_id=call,
        mentions=("bob-bot",),
    )
    w.actions += w.engine.on_message(msg.id)
    assert woke(w, sb) and not woke(w, sa)
    text = result(w, sb)["text"]
    assert LISTEN_SHARED in text and LISTEN_NAMED.strip() in text


def test_an_agent_that_is_not_listening_gets_no_listening_guidance(tmp_path: Path, clock: FakeClock) -> None:
    """A named agent that is not a listener hears its name and nothing about listening."""
    w, call = call_world(tmp_path, clock)
    _, b, sb = waiting(w, "bob-bot")
    msg = w.store.insert_message(
        w.room.id,
        sender_name="call",
        sender_kind="meeting",
        via="meeting",
        text="alice: bob-bot, hello",
        meeting_id=call,
        mentions=("bob-bot",),
    )
    w.actions += w.engine.on_message(msg.id)
    assert woke(w, sb)
    text = result(w, sb)["text"]
    assert LISTEN_ALONE not in text and LISTEN_SHARED not in text
