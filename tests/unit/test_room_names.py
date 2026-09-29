"""Closed room names (DESIGN.md §28.2, §28.5): ``#<name>~closed-<id>``, never a valid room name."""

from __future__ import annotations

import pytest

from switchboard.models import (
    CLOSED_ROOM_RE,
    ROOM_RE,
    InvalidName,
    Room,
    closed_room_name,
    display_room,
    normalize_room,
    room_ref,
    split_closed,
)

BASE32 = "#" + "a" * 32  # the longest open name


def room(name: str, rid: int = 7) -> Room:
    return Room(id=rid, name=name, created_at=0.0, created_by="alice", paused=False, paused_reason=None,
                budget_per_hour=60, budget_remaining=60, budget_window_start=0.0, budget_notice_window=None,
                hop_count=0, hop_limit=6, last_msg_at=None)


@pytest.mark.parametrize("name, rid", [("#build", 7), ("#a_b-c", 1), ("#0", 12), (BASE32, 10**18),
                                       ("#x", 999_999_999_999_999_999)])
def test_closed_name_round_trip(name: str, rid: int) -> None:
    closed = closed_room_name(name, rid)
    assert closed == f"{name}~closed-{rid}"
    assert split_closed(closed) == (name, rid)
    assert display_room(closed) == name
    assert CLOSED_ROOM_RE.fullmatch(closed)


def test_closed_room_name_example() -> None:
    assert closed_room_name("#build", 7) == "#build~closed-7"


@pytest.mark.parametrize("name, rid", [("build", 7), ("#Build", 7), ("#build", 0), ("#build", -1),
                                       (BASE32 + "a", 1), ("#x~closed-1", 2), ("#x", 10**19)])
def test_closed_room_name_refuses_what_it_could_not_split(name: str, rid: int) -> None:
    with pytest.raises(ValueError):
        closed_room_name(name, rid)


@pytest.mark.parametrize("bad", ["#x~closed-0", "#x~closed-01", "#x~closed-1\n", "\n#x~closed-1",
                                 "#x~closed-", "#x~closed-1a", "x~closed-1", "#X~closed-1",
                                 "#x~closed-1~closed-2", "#x~Closed-1", "#x~closed-" + "1" * 20,
                                 BASE32 + "a~closed-1", "#-x~closed-1", "#x", "", "#x~", None, 7])
def test_split_closed_rejects(bad: object) -> None:
    assert split_closed(bad) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("closed", ["#build~closed-7", BASE32 + "~closed-1", "#a_b~closed-99"])
def test_no_closed_name_is_a_room_name(closed: str) -> None:
    assert not ROOM_RE.match(closed)
    with pytest.raises(InvalidName):
        normalize_room(closed)
    with pytest.raises(InvalidName):
        normalize_room(closed[1:])


def test_display_room() -> None:
    assert display_room("#build~closed-7") == "#build"
    assert display_room("#build") == "#build"
    assert display_room("#build~closed-0") == "#build~closed-0"  # not a closed name: unchanged


def test_room_ref() -> None:
    assert room_ref("  #Build~Closed-7 ") == "#build~closed-7"
    assert room_ref("#build~closed-7") == "#build~closed-7"
    assert room_ref(" Build ") == "#build"
    assert room_ref("#a_b") == "#a_b"
    for junk in ["#build~closed-0", "#build~closed-7x", "build~closed-7", "#a b", "", "#" + "a" * 33]:
        with pytest.raises(InvalidName):
            room_ref(junk)
    with pytest.raises(InvalidName, match="must be a string"):
        room_ref(None)  # type: ignore[arg-type]


def test_room_properties() -> None:
    open_room = room("#build")
    assert not open_room.closed and open_room.display_name == "#build" and open_room.slug == "build"
    closed = room("#build~closed-7")
    assert closed.closed and closed.display_name == "#build"
