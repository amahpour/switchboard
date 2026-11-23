"""How reviews go (#863, DESIGN.md §61.5): the figures are the boards, items and moves already stored.

A board counts for its holder, a finding in the window it was raised in and for its first raiser, and a
duration in the window where it ended. Every number here is worked out by hand from the rows a
``FakeClock`` stamped, so a wrong window, a wrong owner, or a median taken over the wrong boards fails.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock

from switchboard import db, review_stats
from switchboard.broker.usage import counts
from switchboard.models import Room, session_key
from switchboard.store import Store
from switchboard.usage import window_starts

NOW = 1_800_000_000.0
TODAY, WEEK = window_starts(NOW, 0)
W = WEEK  # a week's first instant: every time below is an offset from it, or from TODAY
URL = "https://github.com/example/shop/pull/{}"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(db.open_db(tmp_path / "r.db"), FakeClock(NOW))


def at(store: Store, t: float) -> None:
    store.clock.t = t  # type: ignore[attr-defined]


def member(store: Store, room: Room, name: str, harness: str) -> None:
    p = store.upsert_participant(harness, session_key(harness, "", name))
    store.create_membership(room.id, p.id, name)


def board(store: Store, room: Room, n: int, by: str, t: float, head: str) -> dict[str, Any]:
    at(store, t)
    rv = store.open_review(room.id, URL.format(n), head, by)
    event(store, room, rv, t, open=rv["url"], head=head, by=by, how="opened")
    return rv


def event(store: Store, room: Room, rv: dict[str, Any], t: float, **data: Any) -> None:
    at(store, t)
    store.add_event("review", room_id=room.id, data={**data, "board": rv["id"]})


def item(store: Store, rv: dict[str, Any], t: float, by: str, kind: str = "finding") -> Any:
    at(store, t)
    return store.add_review_item(
        rv["id"], kind, "raised" if kind == "finding" else "open", title="t", raised_by=by
    )


def move(
    store: Store, room: Room, rv: dict[str, Any], t: float, it: Any, to: str, state: str, **fields: Any
) -> None:
    at(store, t)
    store.move_review_item(it.id, state, **fields)
    event(store, room, rv, t, item=it.label, to=to, by="x")


@pytest.fixture
def world(store: Store) -> tuple[Store, Room]:
    """One room, agents rev and dev, and six boards that exercise every rule once."""
    at(store, W - 100_000)
    room = store.create_room("#build", "alice", 60, 0)
    member(store, room, "rev", "claude")
    member(store, room, "dev", "codex")
    # D: all of it before the window: it is in no figure
    d = board(store, room, 4, "rev", W - 9000, "dddddd1")
    d1 = item(store, d, W - 8000, "rev")
    move(store, room, d, W - 7000, d1, "dropped", "dropped", reason="no")
    # A: rev's board. F1 fixed, reopened once, confirmed. F2 contested and dropped. Merged later.
    a = board(store, room, 1, "rev", W + 1000, "aaaaaaa")
    f1, f2 = item(store, a, W + 1100, "rev"), item(store, a, W + 1200, "rev")
    move(store, room, a, W + 1300, f1, "conceded", "conceded", owner="dev")
    move(store, room, a, W + 1400, f2, "contested", "contested", reason="why")
    move(store, room, a, W + 1500, f1, "fixed", "fixed", commit_sha="abc1234", proof="t")
    move(store, room, a, W + 1500, f2, "dropped", "dropped", reason="ok")
    move(store, room, a, W + 1600, f1, "reopened", "conceded")
    event(store, room, a, W + 1700, open=a["url"], head="bbbbbbb", by="rev", how="moved")
    move(store, room, a, W + 1800, f1, "fixed", "fixed", commit_sha="abc1235", proof="t")
    move(store, room, a, W + 2000, f1, "confirmed", "fixed", confirmed_by="rev")
    event(store, room, a, W + 5000, report={"pr_state": "merged"}, by="rev")
    at(store, W + 5000)
    store.set_review(a["id"], pr_state="merged", reported_by="rev", reported_at=W + 5000)
    # C: rev's board, settled by a drop of dev's finding, never reported merged
    c = board(store, room, 3, "rev", W + 3000, "ccccccc")
    c1 = item(store, c, W + 3100, "dev")
    move(store, room, c, W + 3500, c1, "dropped", "dropped", reason="no")
    # E: opened before the window, a finding raised in it, and no event
    at(store, W - 1000)
    e = store.open_review(room.id, URL.format(5), "eeeeeee", "rev")
    item(store, e, W + 500, "dev")
    # F: opened by switchboard itself: the room's, no agent's
    board(store, room, 6, "switchboard", W + 4000, "")
    # B: today, dev's, one finding and a question still open
    b = board(store, room, 2, "dev", TODAY + 100, "ddddddd")
    item(store, b, TODAY + 200, "dev")
    item(store, b, TODAY + 300, "dev", "question")
    at(store, NOW)
    return store, room


def page(store: Store, room: Room, tmp_path: Path) -> dict[str, Any]:
    return counts(tmp_path / "r.db", store.clock, [room], "0")["reviews"]  # type: ignore[arg-type]


def test_the_review_figures_are_the_stored_boards_by_window_holder_and_first_raiser(
    world: tuple[Store, Room], tmp_path: Path
) -> None:
    """Hand-worked: opened 4 (A, C, F, B), settled 2 (A at 2000, C at 500 after opening: 1000 and 500),
    raised 5 (A 2, C 1, E 1 on a board opened before the window, B 1), confirmed 1, dropped 2, contested
    1, a share of 1/3, one reopen (of dev's fix), two heads on A and one on C, merged 1 and unknown 1.
    Board D (all before the window) is in nothing; F (switchboard's) is in the room and no agent's."""
    store, room = world
    got = page(store, room, tmp_path)
    (row,) = got["rooms"]
    wk = row["week"]
    assert (wk["opened"], wk["settled"], wk["raised"]) == (4, 2, 5)
    assert (wk["confirmed"], wk["dropped"], wk["contested"], wk["reopened"]) == (1, 2, 1, 1)
    assert wk["share_confirmed"] == pytest.approx(1 / 3)
    assert wk["settle_s"] == 750.0  # median of A's 1000 and C's 500
    assert wk["heads"] == 1.5  # A went through aaaaaaa and bbbbbbb, C only ccccccc
    assert wk["first_finding_s"] == 100.0  # A 100, B 100, C 100, E 1500
    assert wk["to_settled_s"] == 650.0  # first finding to settled: A 900, C 400
    assert wk["to_merged_s"] == 3000.0  # settled W+2000, reported merged W+5000
    assert (wk["merged"], wk["unknown"]) == (1, 1)
    td = row["today"]
    assert (td["opened"], td["raised"], td["settled"], td["merged"]) == (1, 1, 0, 0)
    assert td["first_finding_s"] == 100.0 and td["settle_s"] is None and td["share_confirmed"] is None
    agents = {a["name"]: a for a in got["agents"]}
    assert set(agents) == {"rev", "dev"}
    rev, dev = agents["rev"]["week"], agents["dev"]["week"]
    assert (rev["opened"], rev["settled"], rev["raised"]) == (2, 2, 2)
    assert (rev["confirmed"], rev["dropped"], rev["contested"], rev["reopened"]) == (1, 1, 1, 0)
    assert rev["share_confirmed"] == 0.5
    assert (rev["merged"], rev["unknown"]) == (1, 1)
    assert (dev["opened"], dev["settled"], dev["raised"]) == (1, 0, 3)
    assert (dev["confirmed"], dev["dropped"], dev["reopened"]) == (0, 1, 1)
    assert dev["share_confirmed"] == 0.0 and dev["settle_s"] is None
    assert agents["dev"]["harness"] == "codex" and agents["dev"]["room"] == "#build"
    assert agents["dev"]["today"]["raised"] == 1 and agents["rev"]["today"]["raised"] == 0


def test_a_room_the_caller_did_not_pass_and_a_quiet_week_are_not_on_the_page(
    world: tuple[Store, Room], tmp_path: Path
) -> None:
    """The caller's rooms are the whole picture: no rooms is no figures, and a room whose boards are all
    older than the window has no row (the page says so, it does not list zeros)."""
    store, room = world
    assert counts(tmp_path / "r.db", store.clock, [], "0")["reviews"] == {"rooms": [], "agents": []}  # type: ignore[arg-type]
    store.clock.t = NOW + 30 * 86_400  # type: ignore[attr-defined]
    assert page(store, room, tmp_path) == {"rooms": [], "agents": []}


def test_a_takeover_leaves_the_finding_with_its_first_raiser(store: Store, tmp_path: Path) -> None:
    """Precision belongs to the reviewer who raised the finding, so a takeover (§37.2) moves the
    finding's responsibility and not its credit. The bug: counting it for whoever holds it now."""
    at(store, W - 100)
    room = store.create_room("#build", "alice", 60, 0)
    member(store, room, "rev", "claude")
    member(store, room, "rev2", "claude")
    rv = board(store, room, 1, "rev", W + 10, "aaaaaaa")
    it = item(store, rv, W + 20, "rev")
    at(store, W + 30)
    store.take_review_item(it.id, "rev2")
    event(store, room, rv, W + 30, item="F1", to="taken", by="rev2", previous_raiser="rev")
    at(store, NOW)
    agents = {a["name"]: a["week"]["raised"] for a in page(store, room, tmp_path)["agents"]}
    assert agents == {"rev": 1}


def test_a_deferred_finding_is_neither_confirmed_nor_dropped(store: Store, tmp_path: Path) -> None:
    """A finding deferred to a ticket is settled but not withdrawn, so it is no mark against the reviewer
    (stored as dropped with the ticket, §37.2): raised, and in neither of the two that make the share."""
    at(store, W - 100)
    room = store.create_room("#build", "alice", 60, 0)
    member(store, room, "rev", "claude")
    rv = board(store, room, 1, "rev", W + 10, "aaaaaaa")
    it = item(store, rv, W + 20, "rev")
    move(store, room, rv, W + 30, it, "deferred", "dropped", deferred_to="JIRA-1")
    at(store, NOW)
    wk = page(store, room, tmp_path)["rooms"][0]["week"]
    assert (wk["raised"], wk["dropped"], wk["confirmed"], wk["share_confirmed"]) == (1, 0, 0, None)
    assert wk["settled"] == 1


def test_stored_values_that_are_not_what_a_board_writes_never_raise() -> None:
    """Times and events are claims: a non-finite or boolean time, an event without a mapping, and a report
    that is not a mapping are left out. The bug: a bad row taking the whole usage page down."""
    assert review_stats.settled_at([], []) is None
    assert review_stats.merged_at({}, [(math.nan, {"report": {"pr_state": "merged"}})]) is None
    assert review_stats.merged_at({}, [(1.0, {"report": "merged"})]) is None
    assert review_stats.merged_at({"pr_state": "merged", "reported_at": 7.0}, []) == 7.0
    assert review_stats.merged_at({"pr_state": "merged", "reported_at": True}, []) is None
    assert review_stats.merged_at({}, [(5.0, {"report": {"pr_state": "merged"}})]) == 5.0
    assert (
        review_stats.head_count(
            [(1.0, {"open": "u", "head": "aaaaaaa"}), (math.inf, {"open": "u", "head": "bbbbbbb"})], None
        )
        == 1
    )
    assert review_stats.head_count([(1.0, {"head": "aaaaaaa"})], None) == 0  # not an open event
    got = review_stats.shape(
        [None],
        [{"id": True, "room_id": 1}],
        [],
        {},
        [(1, "x", {})],
        {},
        now=NOW,
        today_start=TODAY,
        week_start=WEEK,
    )  # type: ignore[list-item,arg-type]
    assert got == {"rooms": [], "agents": []}


def test_the_review_queries_use_the_indexes(world: tuple[Store, Room]) -> None:
    """Opening the page reads the review events through ``events_kind_ts`` and a room's boards through
    ``reviews_room``, and an item by its board: never a scan of the events or of every board. This is
    why #863 adds no index and no schema step."""
    store, room = world
    seen: list[str] = []
    store.con.set_trace_callback(
        lambda sql: seen.append(sql) if sql.lstrip().upper().startswith("SELECT") else None
    )
    try:
        store.usage_reviews([room.id], since=WEEK, until=NOW)
    finally:
        store.con.set_trace_callback(None)
    plans = []
    for sql in seen:
        if "FROM reviews r" in sql or "FROM events e" in sql:
            plans += [r[3] for r in store.con.execute("EXPLAIN QUERY PLAN " + sql)]
    assert any("events_kind_ts" in ln for ln in plans), plans
    assert any("reviews_room" in ln for ln in plans), plans
    assert not any(ln.startswith("SCAN") and ("events" in ln or "review_items" in ln) for ln in plans), plans
