"""Loop guard with a limit changed mid-conversation (/hops, DESIGN.md §8.5, §10).

The engine reads the room's ``hop_limit`` from the store on every agent message, so
a new limit applies to the very next one: raised, the room talks past the old limit;
lowered below the current count, the next agent message pauses the room (not the
change itself); 0 turns the guard off, and turning it back on counts the run so far.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock
from engine_world import World

from switchboard.broker.commands import Actor
from switchboard.broker.hub import Hub
from switchboard.broker.service import BrokerInfo, RoomService
from switchboard.config import Config
from switchboard.models import Message, Notice, Room

WEB = Actor(role="human", via="web")
CLI = Actor(role="human_cli", via="cli", chain="zsh ← Terminal")


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def warn_notices(w: World) -> list[Notice]:
    return [a for a in w.take() if isinstance(a, Notice) and a.level == "warn"]


def room(w: World) -> Room:
    r = w.store.room_by_id(w.room.id)
    assert r is not None
    return r


class Shim:
    """RoomService's delivery hooks, wired straight to the World's engine (what AgentService does)."""

    def __init__(self, w: World) -> None:
        self.w = w

    def on_message(self, msg: Message) -> None:
        self.w.actions += self.w.engine.on_message(msg.id)

    def on_command(self, room: Room, name: str, membership_id: int | None) -> None:
        self.w.actions += self.w.engine.on_command(room.id, name, membership_id)

    def on_membership_ended(self, membership_id: int, reason: str) -> None:
        self.w.actions += self.w.engine.on_membership_ended(membership_id, reason)

    def parked_reason(self, membership_id: int) -> str | None:
        return self.w.engine.parked_reason(membership_id)


@pytest.fixture
def svc(w: World, clock: FakeClock) -> RoomService:
    s = RoomService(w.store, Hub(), w.cfg, BrokerInfo(port=7419, test_mode=True), clock)
    s.delivery = Shim(w)
    return s


def test_raising_the_limit_mid_conversation_lets_agents_talk_past_six(w: World) -> None:
    _pa, ma = w.agent("a")
    _pb, mb = w.agent("b")
    for i in range(5):
        w.agent_says(ma if i % 2 else mb, f"hop {i}")
    w.store.set_hop_limit(w.room.id, 30)
    for i in range(5, 29):
        w.agent_says(ma if i % 2 else mb, f"hop {i}")
    r = room(w)
    assert r.hop_count == 29 and not r.paused
    assert w.store.count_events("loop_guard") == 0 and not warn_notices(w)
    w.agent_says(ma, "hop 29")  # the 30th in a row trips it
    r = room(w)
    assert r.paused and r.paused_reason == "loop guard" and r.hop_count == 30
    [note] = warn_notices(w)
    assert note.text.startswith("loop guard: 30 agent messages in a row with no message from alice.")
    assert "/resume" in note.text and "/hops <n> changes the limit (now 30)" in note.text


def test_lowering_below_the_count_pauses_on_the_next_agent_message(w: World) -> None:
    _pa, ma = w.agent("a")
    w.store.set_hop_limit(w.room.id, 30)
    for i in range(10):
        w.agent_says(ma, f"hop {i}")
    w.take()
    w.store.set_hop_limit(w.room.id, 5)
    assert not room(w).paused  # the change itself pauses nothing
    w.agent_says(ma, "one more")
    r = room(w)
    assert r.paused and r.paused_reason == "loop guard" and r.hop_count == 11
    [note] = warn_notices(w)
    assert "11 agent messages in a row" in note.text and "(now 5)" in note.text
    assert w.store.count_events("loop_guard") == 1


def test_after_lowering_a_human_message_still_resets_the_run(w: World) -> None:
    _pa, ma = w.agent("a")
    w.store.set_hop_limit(w.room.id, 30)
    for i in range(10):
        w.agent_says(ma, f"hop {i}")
    w.store.set_hop_limit(w.room.id, 5)
    w.human("back to you")
    for i in range(4):
        w.agent_says(ma, f"again {i}")
    assert not room(w).paused and room(w).hop_count == 4
    w.agent_says(ma, "fifth")
    assert room(w).paused


def test_turning_the_guard_off_then_on_mid_conversation(w: World) -> None:
    _pa, ma = w.agent("a")
    for i in range(5):
        w.agent_says(ma, f"hop {i}")
    w.store.set_hop_limit(w.room.id, 0)
    for i in range(20):
        w.agent_says(ma, f"free {i}")
    r = room(w)
    assert not r.paused and r.hop_count == 25 and w.store.count_events("loop_guard") == 0
    w.store.set_hop_limit(w.room.id, 12)  # back on: the run so far counts
    assert not room(w).paused
    w.agent_says(ma, "next")
    assert room(w).paused and w.store.count_events("loop_guard") == 1


# ------------------------------------------- through the command and the service
def test_hops_command_raise_reaches_the_engine(w: World, svc: RoomService) -> None:
    _pa, ma = w.agent("a")
    _pb, mb = w.agent("b")
    for i in range(5):
        w.agent_says(ma if i % 2 else mb, f"hop {i}")
    assert svc.command("#build", "/hops 30", WEB)["text"] == "#build: hop limit set to 30 (was 6); hops 5/30"
    for i in range(5, 29):
        w.agent_says(ma if i % 2 else mb, f"hop {i}")
    assert not room(w).paused and w.store.count_events("loop_guard") == 0
    w.agent_says(ma, "thirtieth")
    assert room(w).paused and room(w).paused_reason == "loop guard"


def test_hops_command_lower_reaches_the_engine(w: World, svc: RoomService) -> None:
    _pa, ma = w.agent("a")
    svc.command("#build", "/hops 30", WEB)
    for i in range(8):
        w.agent_says(ma, f"hop {i}")
    text = svc.command("#build", "/hops 3", CLI)["text"]
    assert text.endswith("hops 8/3; the next agent message pauses the room")
    assert not room(w).paused
    w.agent_says(ma, "ninth")
    assert room(w).paused and w.store.count_events("loop_guard") == 1


def test_raising_while_guard_paused_needs_resume_then_runs_to_the_new_limit(
    w: World, svc: RoomService
) -> None:
    _pa, ma = w.agent("a")
    pb, mb = w.agent("b")
    for i in range(6):
        w.agent_says(ma, f"hop {i}")
    assert room(w).paused
    sink, acts = w.engine.open_wait(w.p(pb), w.m(mb), "wb", 50)
    w.actions += acts
    text = svc.command("#build", "/hops 30", WEB)["text"]
    assert text == "#build: hop limit set to 30 (was 6); now 6/30; /resume to continue"
    assert room(w).paused  # still paused: nothing was delivered
    assert w.resolved(sink.id) == []
    svc.command("#build", "/resume", WEB)
    r = room(w)
    assert not r.paused and r.hop_count == 0 and r.hop_limit == 30
    [res] = w.resolved(sink.id)
    assert res["status"] == "messages"  # the held messages go out after /resume
    for i in range(29):
        w.agent_says(ma, f"after {i}")
    assert not room(w).paused
    w.agent_says(ma, "thirtieth")
    assert room(w).paused
