"""store: rooms, messages + classification, membership reads, events, web sessions, restart recovery."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from conftest import FakeClock

from switchboard import db
from switchboard.store import Conflict, Store


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> Store:
    return Store(db.open_db(tmp_path / "y.db"), clock)


def add_agent(
    store: Store,
    room_id: int,
    name: str,
    *,
    pid: int | None = None,
    start: float | None = None,
    harness: str = "test",
) -> tuple[int, int]:
    """Insert a participant + active membership directly (M2 owns the real join path)."""
    con = store.con
    with db.tx(con):
        cur = con.execute(
            "INSERT INTO participants(harness, session_key, agent_pid, agent_start, status, created_at)"
            " VALUES(?,?,?,?, 'idle', 0)",
            (harness, f"{harness}:{name}", pid, start),
        )
        pid_row = cur.lastrowid
        last = store.last_message_id(room_id)
        cur = con.execute(
            "INSERT INTO memberships(room_id, participant_id, screen_name, cred_hash, joined_at, join_msg_id)"
            " VALUES(?,?,?,?,0,?)",
            (room_id, pid_row, name, "h" * 64, last),
        )
        return pid_row, cur.lastrowid


def test_rooms(store: Store, clock: FakeClock) -> None:
    r = store.create_room("#build", "alice", 60, 6)
    assert r.name == "#build" and r.slug == "build"
    assert r.budget_remaining == 60 and r.budget_per_hour == 60 and r.hop_limit == 6
    assert r.budget_window_start == clock.now() and not r.paused
    with pytest.raises(Conflict):
        store.create_room("#build", "alice", 60, 6)
    store.create_room("#alpha", "alice", 60, 6)
    assert [x.name for x in store.list_rooms()] == ["#alpha", "#build"]
    assert store.get_room("#nope") is None
    r = store.set_paused(r.id, True, "paused by alice")
    assert r.paused and r.paused_reason == "paused by alice"
    r = store.set_budget(r.id, 3)
    assert r.budget_remaining == 3
    with pytest.raises(ValueError):
        store.set_budget(r.id, -1)


def test_budget_refill_fixed_hourly_window(store: Store, clock: FakeClock) -> None:
    r = store.create_room("#b", "alice", 60, 6)
    store.set_budget(r.id, 5)
    clock.advance(3599)
    assert store.refill_budget(r.id).budget_remaining == 5
    clock.advance(2)
    r2 = store.refill_budget(r.id)
    assert r2.budget_remaining == 60
    assert r2.budget_window_start == r.budget_window_start + 3600
    clock.advance(3 * 3600)
    r3 = store.refill_budget(r.id)
    assert r3.budget_window_start == r.budget_window_start + 4 * 3600


def test_messages_history_and_hops(store: Store, clock: FakeClock) -> None:
    r = store.create_room("#b", "alice", 60, 6)
    ids = []
    for i in range(5):
        clock.advance(1)
        m = store.insert_message(r.id, sender_name="alice", sender_kind="human", via="web", text=f"m{i}")
        ids.append(m.id)
    assert ids == sorted(ids)
    assert [m.text for m in store.history(r.id)] == [f"m{i}" for i in range(5)]
    assert [m.text for m in store.history(r.id, limit=2)] == ["m3", "m4"]
    assert [m.text for m in store.history(r.id, after=ids[1])] == ["m2", "m3", "m4"]
    assert [m.text for m in store.history(r.id, after=ids[1], limit=1)] == ["m2"]
    assert store.last_message_id(r.id) == ids[-1]
    assert store.room_by_id(r.id).last_msg_at == clock.now()
    # hop counter: agent chat +1, human chat resets, notices don't count
    store.insert_message(r.id, sender_name="a", sender_kind="agent", via="mcp", text="x")
    store.insert_message(r.id, sender_name="a", sender_kind="agent", via="mcp", text="y")
    store.insert_message(
        r.id, sender_name="switchboard", sender_kind="system", via="system", kind="notice", text="n"
    )
    assert store.room_by_id(r.id).hop_count == 2
    store.insert_message(r.id, sender_name="alice", sender_kind="human", via="cli", text="reset")
    assert store.room_by_id(r.id).hop_count == 0
    m = store.get_message(ids[0])
    assert m is not None and m.sender_kind == "human" and m.via == "web" and m.mentions == []


def test_delivery_classification(store: Store) -> None:
    """§8.1: human -> prio 2, @mention -> 1, chatter -> 0; sender excluded; notices never delivered."""
    r = store.create_room("#b", "alice", 60, 6)
    _, m_a = add_agent(store, r.id, "alpha")
    _, m_b = add_agent(store, r.id, "beta")
    h = store.insert_message(r.id, sender_name="alice", sender_kind="human", via="web", text="hi all")
    a = store.insert_message(
        r.id,
        sender_name="alpha",
        sender_kind="agent",
        via="mcp",
        text="@beta look",
        sender_membership_id=m_a,
        mentions=["beta"],
    )
    c = store.insert_message(
        r.id, sender_name="beta", sender_kind="agent", via="mcp", text="ok", sender_membership_id=m_b
    )
    store.insert_message(
        r.id, sender_name="switchboard", sender_kind="system", via="system", kind="notice", text="n"
    )
    rows = {
        (x["membership_id"], x["message_id"]): (x["prio"], x["mentioned"], x["state"])
        for x in store.con.execute("SELECT * FROM deliveries")
    }
    assert rows == {
        (m_a, h.id): (2, 0, "pending"),
        (m_b, h.id): (2, 0, "pending"),
        (m_b, a.id): (1, 1, "pending"),
        (m_a, c.id): (0, 0, "pending"),
    }


def test_members_view_hold_and_end(store: Store) -> None:
    r = store.create_room("#b", "alice", 60, 6)
    _, mid = add_agent(store, r.id, "claude-1", harness="claude")
    store.insert_message(r.id, sender_name="alice", sender_kind="human", via="web", text="x")
    [m] = store.members(r.id)
    assert (m.name, m.harness, m.status, m.queued, m.inflight, m.held) == (
        "claude-1",
        "claude",
        "idle",
        1,
        0,
        False,
    )
    assert store.find_member(r.id, "CLAUDE-1") is not None
    store.set_held(mid, True)
    assert store.members(r.id)[0].held
    store.set_held(mid, False)
    assert not store.members(r.id)[0].held
    store.end_membership(mid, "kick", kicked=True)
    assert store.members(r.id) == []
    row = store.con.execute("SELECT * FROM memberships WHERE id=?", (mid,)).fetchone()
    assert row["kicked"] == 1 and row["cred_hash"] is None and row["left_reason"] == "kick"
    assert {x[0] for x in store.con.execute("SELECT state FROM deliveries")} == {"revoked"}
    # the name is free again for a new active membership
    add_agent(store, r.id, "claude-1")


def test_events(store: Store, clock: FakeClock) -> None:
    r = store.create_room("#b", "alice", 60, 6)
    store.add_event("pause", room_id=r.id, data={"via": "web"})
    clock.advance(1)
    store.add_event("login", data={"what": "session"})
    evs = store.recent_events(limit=10)
    assert [e.kind for e in evs] == ["login", "pause"]
    assert [e.kind for e in store.recent_events(room_id=r.id)] == ["pause"]
    assert store.recent_events(kinds=["pause"])[0].data == {"via": "web"}


def test_web_sessions_slide_and_expire(store: Store, clock: FakeClock) -> None:
    store.web_session_create("h1", 100)
    assert store.web_session_valid("h1")
    clock.advance(90)
    assert store.web_session_touch("h1", 100)  # slides to now+100
    clock.advance(90)
    assert store.web_session_touch("h1", 100)
    clock.advance(101)
    assert not store.web_session_valid("h1")
    assert not store.web_session_touch("h1", 100)
    assert store.web_session_count() == 0  # expired session deleted on touch
    store.web_session_create("a", 100)
    store.web_session_create("b", 100)
    assert store.web_session_delete("a") == 1
    assert store.web_session_delete_all() == 1
    store.web_session_create("c", 10)
    clock.advance(11)
    assert store.web_session_purge() == 1


def test_recover_on_start(store: Store, clock: FakeClock) -> None:
    """§4 restart semantics: offered batches expire and revert; everyone offline; dead agents end."""
    r = store.create_room("#b", "alice", 60, 6)
    live_part, live_m = add_agent(store, r.id, "live", pid=os.getpid(), start=123.0)
    dead_part, dead_m = add_agent(store, r.id, "dead", pid=999999, start=1.0)
    msg = store.insert_message(r.id, sender_name="alice", sender_kind="human", via="web", text="x")
    msg2 = store.insert_message(r.id, sender_name="alice", sender_kind="human", via="web", text="y")
    con = store.con
    with db.tx(con):
        bid = con.execute(
            "INSERT INTO batches(membership_id, path, kind, created_at) VALUES(?, 'wait', 'wake', 0)",
            (live_m,),
        ).lastrowid
        con.execute(
            "UPDATE deliveries SET state='offered', batch_id=? WHERE membership_id=? AND message_id=?",
            (bid, live_m, msg.id),
        )
    alive = lambda pid, start: pid == os.getpid()  # noqa: E731
    res = store.recover_on_start(alive)
    assert res == {
        "batches_expired": 1,
        "deliveries_reverted": 1,
        "participants_offline": 2,
        "participants_ended": 1,
    }
    b = con.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
    assert b["state"] == "expired" and b["expire_reason"] == "restart"
    d = con.execute(
        "SELECT * FROM deliveries WHERE membership_id=? AND message_id=?", (live_m, msg.id)
    ).fetchone()
    assert d["state"] == "pending" and d["attempts"] == 1
    d2 = con.execute(
        "SELECT * FROM deliveries WHERE membership_id=? AND message_id=?", (live_m, msg2.id)
    ).fetchone()
    assert d2["state"] == "pending" and d2["attempts"] == 0
    statuses = {x["id"]: x["status"] for x in con.execute("SELECT id, status FROM participants")}
    assert statuses == {live_part: "offline", dead_part: "offline"}
    dm = con.execute("SELECT * FROM memberships WHERE id=?", (dead_m,)).fetchone()
    assert dm["left_reason"] == "session_end" and dm["cred_hash"] is None
    assert {x[0] for x in con.execute("SELECT state FROM deliveries WHERE membership_id=?", (dead_m,))} == {
        "revoked"
    }
    leave = store.history(r.id)[-1]
    assert (leave.kind, leave.sender_name, leave.text) == ("leave", "dead", "left (session ended)")
    assert [m.name for m in store.members(r.id)] == ["live"]
    # live credentials persist
    assert con.execute("SELECT cred_hash FROM memberships WHERE id=?", (live_m,)).fetchone()[0] is not None
    # a second recovery has nothing left to do
    assert store.recover_on_start(alive) == {
        "batches_expired": 0,
        "deliveries_reverted": 0,
        "participants_offline": 1,
        "participants_ended": 0,
    }


def test_mentions_are_stored_lowercase_json(store: Store) -> None:
    r = store.create_room("#b", "alice", 60, 6)
    m = store.insert_message(
        r.id,
        sender_name="alice",
        sender_kind="human",
        via="web",
        text="x",
        mentions=["Beta", "alpha", "beta"],
    )
    row = store.con.execute("SELECT mentions FROM messages WHERE id=?", (m.id,)).fetchone()
    assert json.loads(row[0]) == ["alpha", "beta"]
