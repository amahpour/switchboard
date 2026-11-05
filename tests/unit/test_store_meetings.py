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
