"""Persistence across broker restarts (DESIGN.md §4 restart semantics, §12.2)."""

from __future__ import annotations

from pathlib import Path

from conftest import InProcBroker

from switchboard import db


def seed_agents(dbpath: Path, room_name: str) -> dict[str, int]:
    """Stand-in for M2's join path: two sessions over HTTPS, which are tied to no process (§49.3)."""
    con = db.connect(dbpath)
    ids: dict[str, int] = {}
    with db.tx(con):
        room_id = con.execute("SELECT id FROM rooms WHERE name=?", (room_name,)).fetchone()[0]
        last = con.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
        for name in ("live-1", "quiet-1"):
            p = con.execute(
                "INSERT INTO participants(harness, session_key, host, status, created_at)"
                " VALUES('test', ?, 'laptop', 'busy', 0)",
                (f"test@laptop:{name}",),
            ).lastrowid
            ids[name] = con.execute(
                "INSERT INTO memberships(room_id, participant_id, screen_name, joined_at, join_msg_id)"
                " VALUES(?,?,?,0,?)",
                (room_id, p, name, last),
            ).lastrowid
    con.close()
    return ids


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

    # simulate an in-flight offer of m1 to live-1 at the moment the broker died
    con = db.connect(broker.paths.db)
    with db.tx(con):
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
    assert texts == ["#build created by alice", "before agents", "for the agents", "second"]  # nobody left
    members = web.get("/api/rooms/build/members").json()["members"]
    # both sessions are still members, offline until their next call or hook (§49.3)
    assert sorted(m["name"] for m in members) == ["live-1", "quiet-1"]
    live = next(m for m in members if m["name"] == "live-1")
    assert live["status"] == "offline" and live["queued"] == 2 and live["inflight"] == 0

    ro = db.connect(broker.paths.db, readonly=True)
    b = ro.execute("SELECT state, expire_reason FROM batches WHERE id=?", (bid,)).fetchone()
    assert tuple(b) == ("expired", "restart")
    rows = {
        r["message_id"]: (r["state"], r["attempts"])
        for r in ro.execute("SELECT * FROM deliveries WHERE membership_id=?", (ids["live-1"],))
    }
    assert rows == {m1: ("pending", 1), m2: ("pending", 0)}
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
