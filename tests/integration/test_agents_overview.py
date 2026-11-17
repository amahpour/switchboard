"""The agents overview on Home (#815, DESIGN.md §55.9): every agent session the person can see, once, with
the rooms they see it in, and nobody else's.

A hosted broker with alice (the owner) and bob. What these catch: an agent that is only in a room bob cannot
see appearing for him, or being counted; a private room's name next to an agent that is also in a public one;
one session listed twice for two rooms; the stuck mark that never appears or never clears; an agent listed
with its status line after it left every visible room."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from conftest import FakeClock, InProcBroker
from test_passkeys import hosted_broker, stop
from test_people import Browser, join_as_bob, set_up

from switchboard.models import session_key


@pytest.fixture
def team() -> Iterator[tuple[InProcBroker, Browser, Browser, FakeClock]]:
    clock = FakeClock()
    b = hosted_broker(clock)
    admin = set_up(b)
    bob = join_as_bob(b, admin)
    try:
        assert admin.post("/api/rooms", {"name": "#build"}).status_code == 200
        assert admin.post("/api/rooms", {"name": "#secret", "private": True}).status_code == 200
        assert admin.post("/api/rooms", {"name": "#docs"}).status_code == 200
        yield b, admin, bob, clock
    finally:
        bob.close()
        admin.close()
        stop(b)


def agent(
    b: InProcBroker, name: str, rooms: list[str], *, harness: str = "claude", status: str = "idle", **kw: Any
) -> int:
    """A session in each of ``rooms``, as the store would hold it."""

    def run() -> int:
        store = b.state.store
        p = store.upsert_participant(harness, session_key(harness, "", name), host="")
        now = b.clock.now()  # type: ignore[attr-defined]
        store.update_participant(p.id, status=status, status_at=now, **kw)
        for room in rooms:
            r = store.get_room(room)
            assert r is not None
            store.create_membership(r.id, p.id, name)
        return int(p.id)

    return int(b.on_loop(run))


def overview(who: Browser) -> dict[str, Any]:
    r = who.get("/api/home")
    assert r.status_code == 200, r.text
    return r.json()


def names(data: dict[str, Any]) -> list[str]:
    return [a["name"] for a in data["agents"]]


def test_one_session_is_listed_once_with_each_room_it_is_in_and_its_state(
    team: tuple[InProcBroker, Browser, Browser, FakeClock],
) -> None:
    """Two public rooms, one session: one row with both rooms, its status line and its state. Fails if it is
    listed for each room, or the rooms or the words are missing."""
    b, admin, bob, clock = team
    agent(b, "claude-1", ["#build", "#docs"], status="busy", status_line="refactoring the cart")
    agent(b, "codex-1", ["#build"], harness="codex", status="idle")
    got = overview(bob)
    assert sorted(names(got)) == ["claude-1", "codex-1"] and got["agents_total"] == 2
    one = next(a for a in got["agents"] if a["name"] == "claude-1")
    assert [r["room"] for r in one["rooms"]] == ["#build", "#docs"]
    assert (one["state"], one["line"], one["harness"], one["stuck"]) == (
        "working",
        "refactoring the cart",
        "claude",
        False,
    )
    assert got["agent_counts"] == {
        "waiting": 0,
        "working": 1,
        "idle": 1,
        "offline": 0,
        "not_connected": 0,
        "stuck": 0,
    }


def test_a_person_never_sees_an_agent_that_is_only_in_rooms_they_cannot_see(
    team: tuple[InProcBroker, Browser, Browser, FakeClock],
) -> None:
    """#secret is private to alice. An agent only there is not in bob's list or counts, and one in both is
    there for bob with #build alone, its private room's name nowhere in what he is sent. Fails if either
    leaks, and checks alice sees both."""
    b, admin, bob, clock = team
    agent(b, "hidden-1", ["#secret"], status="busy", status_line="the private plan")
    agent(b, "both-1", ["#build", "#secret"])
    got = overview(bob)
    assert names(got) == ["both-1"] and got["agents_total"] == 1
    assert [r["room"] for r in got["agents"][0]["rooms"]] == ["#build"]
    assert "#secret" not in str(got) and "hidden-1" not in str(got) and "private plan" not in str(got)
    assert got["agent_counts"] == {
        "waiting": 0,
        "working": 0,
        "idle": 1,
        "offline": 0,
        "not_connected": 0,
        "stuck": 0,
    }
    mine = overview(admin)
    assert sorted(names(mine)) == ["both-1", "hidden-1"]
    assert [r["room"] for r in next(a for a in mine["agents"] if a["name"] == "both-1")["rooms"]] == [
        "#build",
        "#secret",
    ]


def test_a_working_agent_with_no_sign_of_life_for_30_minutes_is_possibly_stuck_until_the_next_one(
    team: tuple[InProcBroker, Browser, Browser, FakeClock],
) -> None:
    """The mark follows the clock and the rows: 29 minutes none, 30 marked (and it sorts above the rest of the
    workers), a hook or a say clears it. Idle agents are never marked. Fails if it is missing, never clears,
    or
    marks an idle agent."""
    b, admin, bob, clock = team
    pid = agent(b, "claude-1", ["#build"], status="busy")
    agent(b, "codex-1", ["#build"], harness="codex", status="idle")
    clock.advance(29 * 60)
    assert not overview(bob)["agent_counts"]["stuck"]
    clock.advance(60)
    got = overview(bob)
    assert got["agent_counts"]["stuck"] == 1
    assert [(a["name"], a["stuck"]) for a in got["agents"]] == [("claude-1", True), ("codex-1", False)]
    b.on_loop(lambda: b.state.store.update_participant(pid, last_seen=clock.now()))
    assert overview(bob)["agent_counts"]["stuck"] == 0


def test_the_states_and_the_order_a_person_reads_them_in(
    team: tuple[InProcBroker, Browser, Browser, FakeClock],
) -> None:
    """What needs a person first, then workers, idle, offline. Fails if the order or a state is wrong."""
    b, admin, bob, clock = team
    for name, status in (
        ("a-off", "offline"),
        ("b-idle", "idle"),
        ("c-work", "busy"),
        ("d-wait", "waiting-approval"),
    ):
        agent(b, name, ["#build"], status=status)
    got = overview(bob)
    assert [(a["name"], a["state"]) for a in got["agents"]] == [
        ("d-wait", "waiting"),
        ("c-work", "working"),
        ("b-idle", "idle"),
        ("a-off", "offline"),
    ]


def test_an_agent_that_left_the_room_leaves_the_overview(
    team: tuple[InProcBroker, Browser, Browser, FakeClock],
) -> None:
    """Fails if a membership that ended still lists its session."""
    b, admin, bob, clock = team
    agent(b, "claude-1", ["#build"])
    assert names(overview(bob)) == ["claude-1"]

    def leave() -> None:
        store = b.state.store
        m = store.find_member(store.get_room("#build").id, "claude-1")
        store.end_membership(m.membership_id, "left")

    b.on_loop(leave)
    assert overview(bob)["agents"] == []


def test_a_session_nobody_has_heard_from_is_shown_as_not_connected_with_when_it_was_last_heard(
    team: tuple[InProcBroker, Browser, Browser, FakeClock],
) -> None:
    """The engine's rule for a session that vanished (no wait open, nothing heard for 30 minutes idle or 2
    hours busy, §49.14) shows here as its own state, sorted last, with the time it was last heard, and is
    never "possibly stuck" however long a busy one was quiet. Fails if a vanished session still reads as
    working or idle, or the time is the time it changed state."""
    b, admin, bob, clock = team
    gone = agent(b, "gone-idle", ["#build"], status="idle")
    away = agent(b, "gone-busy", ["#build"], status="busy")
    here = agent(b, "here", ["#build"], status="idle")
    heard = clock.now()
    clock.advance(2 * 3600 + 60)
    b.on_loop(lambda: b.state.store.update_participant(gone, last_seen=heard + 60))  # the later sign
    b.on_loop(lambda: b.state.store.update_participant(away, last_seen=heard))

    b.on_loop(lambda: b.state.store.update_participant(here, last_seen=clock.now(), status_at=clock.now()))
    got = overview(bob)
    assert [(a["name"], a["state"]) for a in got["agents"]] == [
        ("here", "idle"),
        ("gone-idle", "not_connected"),  # heard last, so first among them
        ("gone-busy", "not_connected"),
    ]
    by = {a["name"]: a for a in got["agents"]}
    assert by["gone-idle"]["since"] == heard + 60 and not by["gone-busy"]["stuck"]
    assert got["agent_counts"]["not_connected"] == 2 and got["agent_counts"]["stuck"] == 0
