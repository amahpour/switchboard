"""What agents cost (#818, DESIGN.md §61): the counts are the stored rows over a fixed window.

A wake is a ``kind='wake'`` batch created inside the window. An agent's message is a chat
message it posted. A priority batch, a pull, a human message, a notice and a call line are
neither. A row before the window, or after the clock, is not in it. Today is the local
calendar day; the last 7 days are the seven local days ending today, today included.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock

from switchboard import db
from switchboard.models import Room, session_key
from switchboard.store import Store
from switchboard.usage import budget_left, clamp_offset, daily, earlier, parse_offset, shape, window_starts

DAY = 86_400.0
NOW = 1_800_000_000.0  # a fixed instant; the local day is derived, not assumed


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(db.open_db(tmp_path / "y.db"), FakeClock(NOW))


def _agent(store: Store, room_id: int, name: str, harness: str = "claude") -> int:
    p = store.upsert_participant(harness, session_key(harness, "", name))
    return store.create_membership(room_id, p.id, name).id


def _at(store: Store, t: float) -> None:
    store.clock.t = t  # type: ignore[attr-defined]


def test_the_window_is_the_local_day_and_the_seven_days_that_end_today() -> None:
    """Today starts at local midnight and the week is the six midnights before it. An offset
    outside a real zone is UTC, so a bad value cannot stretch the window."""
    today, week = window_starts(NOW, 0)
    assert today % DAY == 0
    assert today <= NOW < today + DAY
    assert week == today - 6 * DAY
    # NOW is 08:00 UTC, so an hour east is still that UTC date and local midnight is an hour earlier
    east, _ = window_starts(NOW, 60)
    assert east == today - 3600
    assert clamp_offset(-480) == -480
    assert clamp_offset(840) == 840
    for bad in (True, False, 1.5, 841, -841, "0", None):
        assert clamp_offset(bad) == 0
    assert parse_offset(None) == 0
    assert parse_offset("") == 0
    assert parse_offset("-480") == -480
    assert parse_offset("nope") == 0
    assert parse_offset("1.5") == 0
    assert parse_offset("99999") == 0
    assert parse_offset("+60") == 0  # not the digit form the query is allowed to send
    assert parse_offset(" 60") == 0


def test_counts_match_the_stored_rows_over_the_fixed_window(store: Store) -> None:
    """The bug this catches: a wake or a post just outside the window counted, or a batch that
    is not a wake counted as one, or a room that was not asked included."""
    today, week = window_starts(NOW, 0)
    _at(store, week - 1000)
    build = store.create_room("#build", "alice", 60, 0)
    quiet = store.create_room("#quiet", "alice", 10, 0)
    other = store.create_room("#other", "alice", 60, 0)
    claude = _agent(store, build.id, "claude-1")
    codex = _agent(store, build.id, "codex-1", "codex")
    outsider = _agent(store, other.id, "claude-1")

    def wake(mid: int, text: str) -> None:
        msg = store.insert_message(
            store.get_membership(mid).room_id,  # type: ignore[union-attr]
            sender_name="alice",
            sender_kind="human",
            via="web",
            text=text,
        )
        store.create_batch(mid, path="wait", kind="wake", counted=True, items=[(msg.id, 1)])

    _at(store, week - 10)  # before the seven days
    wake(claude, "old")
    store.insert_message(
        build.id,
        sender_name="claude-1",
        sender_kind="agent",
        via="mcp",
        text="old post",
        sender_membership_id=claude,
        sender_harness="claude",
    )
    store.add_event("budget_exhausted", room_id=build.id)

    _at(store, week)  # the first instant of the seven days: in the week, not today
    wake(codex, "week")
    store.insert_message(
        build.id,
        sender_name="codex-1",
        sender_kind="agent",
        via="mcp",
        text="week post",
        sender_membership_id=codex,
        sender_harness="codex",
    )

    _at(store, today)  # local midnight: today and the week
    wake(claude, "today-a")
    wake(codex, "today-b")
    wake(codex, "today-c")
    store.insert_message(
        build.id,
        sender_name="claude-1",
        sender_kind="agent",
        via="mcp",
        text="today post",
        sender_membership_id=claude,
        sender_harness="claude",
    )
    # not wakes, and not agent posts
    human = store.insert_message(
        build.id, sender_name="alice", sender_kind="human", via="web", text="a question"
    )
    store.create_batch(claude, path="hook", kind="priority", counted=False, items=[(human.id, 1)])
    pulled = store.insert_message(
        build.id,
        sender_name="codex-1",
        sender_kind="agent",
        via="mcp",
        text="a pull's source",
        sender_membership_id=codex,
        sender_harness="codex",
    )
    # that post is a real agent message, so it counts as a post; the pull batch must not count as a wake
    store.create_batch(claude, path="read", kind="pull", counted=False, items=[(pulled.id, 1)])
    store.insert_message(
        build.id, sender_name="switchboard", sender_kind="system", via="system", kind="notice", text="n"
    )
    store.insert_message(build.id, sender_name="call", sender_kind="meeting", via="meeting", text="a line")
    store.add_event("budget_exhausted", room_id=build.id)
    wake(outsider, "not this person's ask")  # a room the query is not given

    _at(store, NOW + 50)  # after the clock the page asked at: not in the window
    wake(claude, "future")

    _at(store, NOW)
    page = shape(
        [store.room_by_id(build.id), store.room_by_id(quiet.id)],
        store.usage_wakes([build.id, quiet.id], since=week, today=today, until=NOW),
        store.usage_posts([build.id, quiet.id], since=week, today=today, until=NOW),
        store.usage_exhausted([build.id, quiet.id], since=week, until=NOW),
        now=NOW,
        offset_min=0,
        today_start=today,
        week_start=week,
    )

    rooms = {r["name"]: r for r in page["rooms"]}
    assert list(rooms) == ["#build", "#quiet"]  # noisiest first; a quiet room is still listed
    assert rooms["#build"]["wakes_today"] == 3
    assert rooms["#build"]["wakes_week"] == 4
    # today: claude's post and codex's "a pull's source"; week adds codex's week post
    assert rooms["#build"]["posts_today"] == 2
    assert rooms["#build"]["posts_week"] == 3
    assert rooms["#build"]["budget_per_hour"] == 60
    # those wakes were stored without a refill, and the window they belong to has passed,
    # so the page shows the rate the next wake would refill to, not the dry stored row
    assert rooms["#build"]["budget_remaining"] == 60
    assert store.room_by_id(build.id).budget_remaining < 60
    assert rooms["#build"]["exhausted_week"] == 1
    assert rooms["#quiet"] == {
        "name": "#quiet",
        "slug": "quiet",
        "private": False,
        "budget_per_hour": 10,
        "budget_remaining": 10,
        "wakes_today": 0,
        "wakes_week": 0,
        "posts_today": 0,
        "posts_week": 0,
        "exhausted_week": 0,
    }
    assert [a["name"] for a in page["agents"]] == ["codex-1", "claude-1"]
    codex_row, claude_row = page["agents"]
    assert codex_row["room"] == "#build" and codex_row["harness"] == "codex"
    assert (codex_row["wakes_today"], codex_row["wakes_week"]) == (2, 3)
    assert (codex_row["posts_today"], codex_row["posts_week"]) == (1, 2)
    assert (claude_row["wakes_today"], claude_row["wakes_week"]) == (1, 1)
    assert (claude_row["posts_today"], claude_row["posts_week"]) == (1, 1)
    assert all(a["room"] != "#other" for a in page["agents"])
    assert "#other" not in rooms


def test_the_usage_queries_search_the_window_indexes(store: Store) -> None:
    """Opening the page must not scan every message and every batch. The plan searches
    ``messages_agent_ts`` and ``batches_wake_created`` by the window's bounds. Without those
    indexes the message query walks ``messages_room_id`` (every message in the room) and the
    batch query scans ``batches``."""
    room = store.create_room("#build", "alice", 40, 0)
    mid = _agent(store, room.id, "claude-1")
    asked = store.insert_message(
        room.id, sender_name="alice", sender_kind="human", via="web", text="a question"
    )
    store.create_batch(mid, path="wait", kind="wake", counted=True, items=[(asked.id, 1)])
    store.insert_message(
        room.id,
        sender_name="claude-1",
        sender_kind="agent",
        via="mcp",
        text="a post",
        sender_membership_id=mid,
        sender_harness="claude",
    )
    store.add_event("budget_exhausted", room_id=room.id)
    today, week = window_starts(NOW, 0)
    seen: list[str] = []

    def collect(sql: str) -> None:
        if sql.lstrip().upper().startswith("SELECT"):
            seen.append(sql)

    store.con.set_trace_callback(collect)
    try:
        store.usage_wakes([room.id], since=week, today=today, until=NOW)
        store.usage_posts([room.id], since=week, today=today, until=NOW)
        store.usage_exhausted([room.id], since=week, until=NOW)
    finally:
        store.con.set_trace_callback(None)

    def plan(fragment: str) -> list[str]:
        sqls = [s for s in seen if fragment in s]
        assert len(sqls) == 1, seen
        return [r[3] for r in store.con.execute("EXPLAIN QUERY PLAN " + sqls[0])]

    posts, wakes, exhausted = plan("FROM messages"), plan("FROM batches"), plan("FROM events")
    assert any("messages_agent_ts" in ln and "ts>" in ln for ln in posts), posts
    assert any("batches_wake_created" in ln and "created_at>" in ln for ln in wakes), wakes
    assert any("events_kind_ts" in ln and "ts>" in ln for ln in exhausted), exhausted
    assert not any(ln.startswith("SCAN") for ln in (*posts, *wakes, *exhausted)), (posts, wakes, exhausted)


def test_a_quiet_hour_shows_the_budget_refilled(store: Store) -> None:
    """The refill is written only when the next wake asks. A room that spent its hour and then
    went quiet keeps ``budget_remaining`` at 0, so the page would say "0 of 40 left" for ever.
    Once ``now`` reaches ``budget_window_start + 3600``, the page shows ``budget_per_hour``,
    computed from those stored times, and it does not write the refill. Inside the hour it
    still shows what is left, including 0."""
    room = store.create_room("#build", "alice", 40, 0)
    store.set_budget(room.id, 0)
    start = room.budget_window_start

    def page_at(t: float) -> int:
        _at(store, t)
        today, week = window_starts(t, 0)
        got = shape(
            [store.room_by_id(room.id)],
            [],
            [],
            {},
            now=t,
            offset_min=0,
            today_start=today,
            week_start=week,
        )
        return got["rooms"][0]["budget_remaining"]

    assert page_at(start + 3599) == 0
    assert store.room_by_id(room.id).budget_remaining == 0  # the row is still dry
    assert page_at(start + 3600) == 40
    assert page_at(start + 7200) == 40  # a later hour refills to the rate, it does not stack
    assert store.room_by_id(room.id).budget_remaining == 0  # still not written
    assert store.room_by_id(room.id).budget_window_start == start
    # a window that is not a finite time does not invent a full hour
    assert budget_left(40, 0, None, NOW) == 0
    assert budget_left(40, 3, True, NOW) == 3
    assert budget_left(40, 3, float("nan"), NOW) == 3
    assert budget_left(40, 0, float("inf"), NOW) == 0


def test_ranking_is_noisiest_first_and_a_room_not_passed_is_dropped() -> None:
    """More wakes rank first. A count row for a room the caller did not pass is dropped, so the
    caller decides who is visible, and two casings of one agent are one row."""
    build = Room(
        id=1,
        name="#build",
        created_at=0,
        created_by="alice",
        paused=False,
        paused_reason=None,
        budget_per_hour=60,
        budget_remaining=60,
        budget_window_start=0,
        budget_notice_window=None,
        hop_count=0,
        hop_limit=0,
        last_msg_at=None,
    )
    ops = Room(
        id=2,
        name="#ops",
        created_at=0,
        created_by="alice",
        paused=False,
        paused_reason=None,
        budget_per_hour=30,
        budget_remaining=4,
        budget_window_start=NOW,  # this hour is still open, so 4 left is what the page shows
        budget_notice_window=None,
        hop_count=0,
        hop_limit=0,
        last_msg_at=None,
        private=True,
    )
    page = shape(
        [ops, build],
        [
            {"room_id": 1, "name": "claude-1", "harness": "claude", "host": "", "today": 2, "week": 3},
            {"room_id": 9, "name": "hidden", "harness": "claude", "host": "", "today": 9, "week": 9},
            # the same agent, two casings: one row, and the weeks add (1 + 1), not two agents
            {"room_id": 2, "name": "Claude-1", "harness": "claude", "host": "", "today": 0, "week": 1},
            {"room_id": 2, "name": "claude-1", "harness": "claude", "host": "", "today": 1, "week": 1},
        ],
        [
            {"room_id": 1, "name": "claude-1", "harness": "claude", "host": "", "today": 0, "week": 1},
            {"room_id": 2, "name": "claude-1", "harness": "claude", "host": "laptop", "today": 1, "week": 1},
        ],
        {1: 2},
        now=NOW,
        offset_min=0,
        today_start=NOW,
        week_start=NOW,
    )
    # #build was woken more often, so it is first even though it was passed second
    assert [r["name"] for r in page["rooms"]] == ["#build", "#ops"]
    assert page["rooms"][0]["wakes_week"] == 3 and page["rooms"][0]["posts_week"] == 1
    assert page["rooms"][0]["exhausted_week"] == 2
    assert page["rooms"][1]["private"] is True
    assert page["rooms"][1]["wakes_week"] == 2  # the two casings summed into the one agent
    assert page["rooms"][1]["budget_remaining"] == 4
    assert all(a["name"] != "hidden" for a in page["agents"])
    assert [(a["name"], a["room"], a["host"]) for a in page["agents"]][0] == ("claude-1", "#build", "")
    folded = [a for a in page["agents"] if a["room"] == "#ops" and a["host"] == ""]
    assert len(folded) == 1 and folded[0]["name"].casefold() == "claude-1"
    assert folded[0]["wakes_week"] == 2 and folded[0]["wakes_today"] == 1
    assert {a["host"] for a in page["agents"] if a["room"] == "#ops"} == {"", "laptop"}


def test_the_chart_puts_each_wake_in_its_local_day() -> None:
    """Seven bars, oldest first, today last. A wake one second before local midnight is yesterday's
    and one at midnight is today's, and the local midnight moves with the offset. The bug: bucketing
    by UTC day, which would put an evening wake in the west into the next day's bar."""
    for offset in (0, -480, 330):
        today, week = window_starts(NOW, offset)
        bars = daily([(week - 1, 9), (week, 1), (today - 1, 2), (today, 3), (NOW, 4), (today + DAY, 7)], week)
        assert [b["start"] for b in bars] == [week + i * DAY for i in range(7)]
        # before the window and from the end of today on are in no bar
        assert bars[0]["wakes"] == 1
        assert bars[5]["wakes"] == 2
        assert bars[6]["wakes"] == 3 + 4
        assert bars[6]["start"] == today
    # an offset west of UTC: 23:30 local on the evening before today is yesterday's, not today's
    today, week = window_starts(NOW, -480)
    assert daily([(today - 1800, 1)], week)[5]["wakes"] == 1
    assert daily([(today - 1800, 1)], week)[6]["wakes"] == 0


def test_the_chart_ignores_what_is_not_a_time_or_a_count() -> None:
    """A row that is not a finite time, or whose count is not a whole number, moves no bar."""
    _, week = window_starts(NOW, 0)
    bars = daily(
        [(float("nan"), 5), (True, 5), ("x", 5), (week + 1, True), (week + 1, -3), (week + 1, 2)], week
    )  # type: ignore[list-item]
    assert [b["wakes"] for b in bars] == [2, 0, 0, 0, 0, 0, 0]
    assert daily([], week)[0]["wakes"] == 0


def test_the_periods_before_are_yesterday_and_the_seven_days_before_the_week() -> None:
    """The tiles compare today with the whole of yesterday, and the week with the 7 days that end
    where it starts. They touch: no day is in both, and none is skipped."""
    today, week = window_starts(NOW, 0)
    (y0, y1), (w0, w1) = earlier(today, week)
    assert (y0, y1) == (today - DAY, today)
    assert (w0, w1) == (week - 7 * DAY, week)


def test_the_counts_before_are_a_half_open_span_of_wakes_and_agent_posts(store: Store) -> None:
    """``usage_span`` counts wakes and agent chat in ``[since, until)``: a row exactly at ``until`` is the
    next period's, so no row is in two periods; a person's message and a pull are not in it; the
    day chart's rows are the same wakes. The bug: an inclusive end counting midnight twice."""
    room = store.create_room("#build", "alice", 40, 0)
    other = store.create_room("#other", "alice", 40, 0)
    mid = _agent(store, room.id, "claude-1")
    today, week = window_starts(NOW, 0)
    yesterday = today - DAY

    def wake(t: float) -> None:
        _at(store, t)
        msg = store.insert_message(room.id, sender_name="alice", sender_kind="human", via="web", text="x")
        store.create_batch(mid, path="wait", kind="wake", counted=True, items=[(msg.id, 1)])

    for t in (yesterday, yesterday + 60, today - 1, today):
        wake(t)
    _at(store, yesterday + 5)
    store.insert_message(
        room.id, sender_name="claude-1", sender_kind="agent", via="mcp", text="p",
        sender_membership_id=mid, sender_harness="claude",
    )  # fmt: skip
    store.insert_message(room.id, sender_name="alice", sender_kind="human", via="web", text="not an agent")
    _at(store, NOW)
    got = store.usage_span([room.id], since=yesterday, until=today)
    assert got == {"wakes": 3, "posts": 1}  # the wake at midnight is today's
    assert store.usage_span([other.id], since=yesterday, until=today) == {"wakes": 0, "posts": 0}
    assert store.usage_span([], since=yesterday, until=today) == {"wakes": 0, "posts": 0}
    times = store.usage_wake_times([room.id], since=week, until=NOW)
    assert sum(n for _, n in times) == 4
    assert [b["wakes"] for b in daily(times, week)] == [0, 0, 0, 0, 0, 3, 1]
