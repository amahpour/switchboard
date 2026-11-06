"""store: how many calls are open (DESIGN.md §52, ``[meeting] max_calls``, #607)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock

from switchboard import db
from switchboard.store import Store


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> Store:
    return Store(db.open_db(tmp_path / "m.db"), clock)


def test_count_open_meetings_counts_joining_and_live_calls_in_every_room(store: Store) -> None:
    """The limit is the bot's memory, so it counts across rooms and counts a call from the moment it
    is joining; a call that ended, with or without an error, no longer counts. Fails if the count
    is per room (two rooms would each get a call under a limit of one), if joining calls are
    left out, or if ended ones stay in."""
    assert store.count_open_meetings() == 0
    a = store.create_room("a", "alice", 60, 6)
    b = store.create_room("b", "alice", 60, 6)
    c = store.create_room("c", "alice", 60, 6)
    first = store.insert_meeting(a.id, started_by="alice", platform="zoom")
    assert store.count_open_meetings() == 1  # joining
    store.set_meeting_live(first.id)
    second = store.insert_meeting(b.id, started_by="alice", platform="meet")
    assert store.count_open_meetings() == 2  # one live, one joining, two rooms
    store.end_meeting(second.id, "could not join")
    assert store.count_open_meetings() == 1
    store.end_meeting(first.id, None)
    assert store.count_open_meetings() == 0
    store.insert_meeting(c.id, started_by="alice", platform="teams")  # a room may start another one later
    store.insert_meeting(a.id, started_by="alice", platform="zoom")  # and so may the first
    assert store.count_open_meetings() == 2


def test_a_call_records_when_it_went_live_once(store: Store, clock: FakeClock) -> None:
    """The call's details say "Live since" (§54), so the time it went live is kept: nothing while it is
    joining, the broker's clock when it goes live, and never moved by a later call to set it live (a second
    push, a retry) or after it ended. Fails if ``live_at`` is the time it started, is overwritten, or is
    set on a call that is not joining."""
    room = store.create_room("a", "alice", 60, 6)
    row = store.insert_meeting(room.id, started_by="alice", platform="zoom")
    assert row.state == "joining" and row.live_at is None
    clock.advance(42)
    store.set_meeting_live(row.id)
    live = store.meeting_by_id(row.id)
    assert live is not None and live.state == "live" and live.live_at == clock.now() != row.started_at
    clock.advance(10)
    store.set_meeting_live(row.id)  # already live: left as it is
    again = store.meeting_by_id(row.id)
    assert again is not None and again.live_at == live.live_at
    store.end_meeting(row.id, None)
    store.set_meeting_live(row.id)  # ended: not brought back
    ended = store.meeting_by_id(row.id)
    assert ended is not None and ended.state == "ended" and ended.live_at == live.live_at
