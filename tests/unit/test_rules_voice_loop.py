"""The engine stops a voice loop: on a live call, agents wake only for a person (DESIGN.md §52.2, #713).

After ``VOICE_LOOP_LINES`` (8) agent messages in a row in a room with a live call, with no person speaking on
the call or writing in the room, an agent wakes only for a person's message, and the room is told once.
This is tighter than ``hop_limit``, which a call line does not reset; ``hop_limit`` is not touched.
"""

from __future__ import annotations

from pathlib import Path

from conftest import FakeClock
from engine_agents import result, woke
from engine_world import World

from switchboard.config import Config
from switchboard.delivery import rules
from switchboard.models import Notice

N = rules.VOICE_LOOP_LINES


# ---- the pure rule
def test_eight_agent_messages_in_a_row_on_a_live_call_trip_the_rule() -> None:
    assert rules.voice_loop_tripped(["agent"] * N, True)
    assert not rules.voice_loop_tripped(["agent"] * (N - 1), True)


def test_without_a_live_call_the_rule_never_trips() -> None:
    """A room with no call keeps today's rules. Fails if the rule leaks to plain text rooms."""
    assert not rules.voice_loop_tripped(["agent"] * 30, False)


def test_a_person_in_the_room_or_a_line_from_the_call_ends_the_run() -> None:
    """Newest first. The run is counted back from the newest message to the first person."""
    assert not rules.voice_loop_tripped(["agent"] * 3 + ["human"] + ["agent"] * 20, True)
    assert not rules.voice_loop_tripped(["agent"] * 3 + ["meeting"] + ["agent"] * 20, True)
    assert rules.voice_loop_tripped(["agent"] * N + ["human"], True)


def test_a_kind_the_code_does_not_know_neither_ends_nor_counts_in_the_run() -> None:
    """Fails closed: an odd sender cannot be used to reset the count."""
    assert rules.voice_loop_tripped(["agent"] * 4 + ["robot"] + ["agent"] * 4, True)
    assert not rules.voice_loop_tripped(["agent"] * 4 + ["robot"] + ["agent"] * 3, True)


# ---- the engine
def call_world(tmp_path: Path, clock: FakeClock, *, live: bool = True, **delivery) -> World:
    cfg = Config(human_name="alice").with_delivery(quiet_s=0, max_hold_s=0, hop_limit=0, **delivery)
    w = World(tmp_path, clock, cfg)
    if live:
        call = w.store.insert_meeting(w.room.id, started_by="alice", platform="zoom")
        w.store.set_meeting_live(call.id)
        w.call_id = call.id
    return w


def agent_lines(w: World, n: int, to: str = "alpha") -> None:
    beta = w.store.active_membership_by_name(w.room.id, "beta")
    for i in range(n):
        w.agent_says(beta, f"@{to} point {i}", mentions=(to,))


def open_wait(w: World, name: str):
    m = w.store.active_membership_by_name(w.room.id, name)
    p = w.p(m.participant_id)
    sink, acts = w.engine.open_wait(p, m, f"w-{name}", 3600.0)
    w.actions += acts
    return sink


def two_agents(w: World) -> None:
    w.agent("alpha", status="idle")
    w.agent("beta", status="busy")


def test_seven_agent_lines_still_wake_an_agent_but_eight_hold_it(tmp_path: Path, clock: FakeClock) -> None:
    """The eighth agent message in a row on a live call stops waking agents. An @mention of an idle agent
    would otherwise wake it at once (budget left). Fails if the eighth still wakes it, or the seventh
    does not."""
    w = call_world(tmp_path, clock)
    two_agents(w)
    agent_lines(w, N - 1)
    assert woke(w, open_wait(w, "alpha"))
    (tmp_path / "b").mkdir()
    w2 = call_world(tmp_path / "b", clock)
    two_agents(w2)
    agent_lines(w2, N)
    held = open_wait(w2, "alpha")
    assert result(w2, held) is None  # still waiting: nothing woke it


def test_a_room_without_a_live_call_wakes_as_before(tmp_path: Path, clock: FakeClock) -> None:
    w = call_world(tmp_path, clock, live=False)
    two_agents(w)
    agent_lines(w, N + 4)
    assert woke(w, open_wait(w, "alpha"))


def test_a_person_in_the_room_wakes_the_held_agent_and_ends_the_run(tmp_path: Path, clock: FakeClock) -> None:
    w = call_world(tmp_path, clock)
    two_agents(w)
    agent_lines(w, N)
    sink = open_wait(w, "alpha")
    assert result(w, sink) is None
    w.human("alpha, what do you think?", mentions=("alpha",))
    assert woke(w, sink)
    assert "what do you think" in result(w, sink)["text"]


def test_a_line_from_the_call_ends_the_run_and_agents_wake_again(tmp_path: Path, clock: FakeClock) -> None:
    """A person speaking on the call counts as a person. Fails if only a typed message ends the run."""
    w = call_world(tmp_path, clock)
    two_agents(w)
    agent_lines(w, N)
    sink = open_wait(w, "alpha")
    assert result(w, sink) is None
    msg = w.store.insert_message(
        w.room.id,
        sender_name="call",
        sender_kind="meeting",
        via="meeting",
        text="alice: go on",
        meeting_id=w.call_id,
    )
    w.actions += w.engine.on_message(msg.id)
    w.actions += w.engine.tick()
    assert woke(w, sink)  # the earlier @mention is released with the call line (the run is over)


def test_the_room_is_told_once_when_the_rule_trips_and_again_after_a_person_spoke(
    tmp_path: Path, clock: FakeClock
) -> None:
    w = call_world(tmp_path, clock)
    two_agents(w)

    def notices() -> list[str]:
        return [a.text for a in w.actions if isinstance(a, Notice) and "agent messages in a row" in a.text]

    agent_lines(w, N + 3)
    assert len(notices()) == 1
    assert "wake only for alice's messages" in notices()[0]
    w.human("carry on")
    agent_lines(w, N)
    assert len(notices()) == 2


def test_a_busy_agent_is_not_handed_an_agent_mention_either(tmp_path: Path, clock: FakeClock) -> None:
    """Mid-turn context for a busy agent is a priority release too; with the rule tripped only a person's
    message is released. Fails if a busy agent still gets @mentions from agents."""
    w = call_world(tmp_path, clock)
    p, m = w.agent("alpha", status="busy", hooks=True)
    w.agent("beta", status="busy")
    agent_lines(w, N)
    out, _ = w.engine._claim_context(w.p(p), "post_tool")
    assert out is None


def test_hop_limit_is_not_changed_by_the_rule(tmp_path: Path, clock: FakeClock) -> None:
    """The loop guard still counts only agent messages since a person's message; a call line does not
    reset it (call audio must not defeat the guard), and the new rule does not pause the room."""
    cfg = Config(human_name="alice").with_delivery(quiet_s=0, max_hold_s=0, hop_limit=100)
    w = World(tmp_path, clock, cfg)
    call = w.store.insert_meeting(w.room.id, started_by="alice", platform="zoom")
    w.store.set_meeting_live(call.id)
    two_agents(w)
    agent_lines(w, N)
    room = w.store.room_by_id(w.room.id)
    assert room.hop_count == N and not room.paused
