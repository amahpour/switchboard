"""Persistence across broker restarts (DESIGN.md §4 restart semantics, §12.2)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from conftest import InProcBroker

from switchboard import db
from switchboard.broker import proc
from switchboard.remote import satellite


def seed_agents(dbpath: Path, room_name: str) -> dict[str, int]:
    """Stand-in for M2's join path: two agents whose 'agent process' is this test process."""
    con = db.connect(dbpath)
    me = proc.info(os.getpid())
    assert me is not None
    ids: dict[str, int] = {}
    with db.tx(con):
        room_id = con.execute("SELECT id FROM rooms WHERE name=?", (room_name,)).fetchone()[0]
        last = con.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
        for name, pid, start in (("live-1", os.getpid(), me.start), ("dead-1", os.getpid(), me.start)):
            p = con.execute(
                "INSERT INTO participants(harness, session_key, agent_pid, agent_start, status, created_at)"
                " VALUES('test', ?, ?, ?, 'busy', 0)",
                (f"test:{name}", pid, start),
            ).lastrowid
            ids[name] = con.execute(
                "INSERT INTO memberships(room_id, participant_id, screen_name, cred_hash,"
                " joined_at, join_msg_id)"
                " VALUES(?,?,?,?,0,?)",
                (room_id, p, name, "c" * 64, last),
            ).lastrowid
    con.close()
    return ids


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc btime: Linux")
def test_restart_keeps_live_local_sessions_after_clock_step(
    broker: InProcBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new broker must use the previous boot pin before deciding a live agent has ended."""
    web = broker.web_client()
    try:
        assert (
            web.post("/api/rooms", json={"name": "#build"}, headers=broker.write_headers()).status_code == 200
        )
    finally:
        web.close()
    broker.stop()
    base = proc.read_linux_btime()
    bid = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    satellite.boot_time(broker.paths, bid, base)
    monkeypatch.setattr(proc, "_BTIME_PIN", base)
    ids = seed_agents(broker.paths.db, "#build")
    monkeypatch.setattr(proc, "_BTIME_PIN", None)  # the next broker is a fresh process
    monkeypatch.setattr(proc, "read_linux_btime", lambda: base + 4.0)
    monkeypatch.setattr(proc, "_linux_btime", lambda: base + 4.0)
    broker.start()
    assert broker.state.recovery["participants_ended"] == 0
    room = broker.state.store.get_room("#build")
    assert room is not None
    assert {m.name for m in broker.state.store.members(room.id)} == set(ids)


def test_history_pending_and_offers_survive_restart(broker: InProcBroker) -> None:
    web = broker.web_client()
    h = broker.write_headers()
    web.post("/api/rooms", json={"name": "#build"}, headers=h)
    web.post("/api/rooms/build/say", json={"text": "before agents"}, headers=h)
    broker.stop()

    ids = seed_agents(broker.paths.db, "#build")
    broker.start()
    web = broker.web_client()
    m1 = web.post("/api/rooms/build/say", json={"text": "for the agents"}, headers=h).json()["id"]
    m2 = web.post("/api/rooms/build/say", json={"text": "second"}, headers=h).json()["id"]
    broker.stop()

    # simulate an in-flight offer of m1 to live-1 at the moment the broker died,
    # and dead-1's agent process going away while the broker was down
    con = db.connect(broker.paths.db)
    with db.tx(con):
        con.execute(
            "UPDATE participants SET agent_pid=999999, agent_start=1.0 WHERE session_key='test:dead-1'"
        )
        bid = con.execute(
            "INSERT INTO batches(membership_id, path, kind, created_at) VALUES(?, 'wait', 'wake', 0)",
            (ids["live-1"],),
        ).lastrowid
        con.execute(
            "UPDATE deliveries SET state='offered', batch_id=? WHERE membership_id=? AND message_id=?",
            (bid, ids["live-1"], m1),
        )
    con.close()

    broker.start()
    web = broker.web_client()
    texts = [m["text"] for m in web.get("/api/rooms/build/messages").json()["messages"]]
    assert texts[:4] == ["#build created by alice", "before agents", "for the agents", "second"]
    assert texts[-1] == "left (session ended)"  # dead-1 was ended on restart
    members = web.get("/api/rooms/build/members").json()["members"]
    assert [m["name"] for m in members] == ["live-1"]
    assert members[0]["status"] == "offline" and members[0]["queued"] == 2 and members[0]["inflight"] == 0

    ro = db.connect(broker.paths.db, readonly=True)
    b = ro.execute("SELECT state, expire_reason FROM batches WHERE id=?", (bid,)).fetchone()
    assert tuple(b) == ("expired", "restart")
    rows = {
        r["message_id"]: (r["state"], r["attempts"])
        for r in ro.execute("SELECT * FROM deliveries WHERE membership_id=?", (ids["live-1"],))
    }
    assert rows == {m1: ("pending", 1), m2: ("pending", 0)}
    dead = {
        r["state"] for r in ro.execute("SELECT state FROM deliveries WHERE membership_id=?", (ids["dead-1"],))
    }
    assert dead == {"revoked"}
    ro.close()
    # ids keep increasing after restarts
    m3 = web.post("/api/rooms/build/say", json={"text": "after"}, headers=h).json()["id"]
    assert m3 > m2


def test_sessions_survive_restart_but_login_tokens_do_not(broker: InProcBroker) -> None:
    web = broker.web_client()
    unused = broker.login_url().removeprefix(broker.base)
    broker.restart()
    # the cookie session persisted (sha256 in SQLite) ...
    import httpx

    c = httpx.Client(base_url=broker.base, cookies=web.cookies)
    assert c.get("/api/rooms").status_code == 200
    # ... but one-time tokens lived only in memory
    assert httpx.get(broker.base + unused).status_code == 403
