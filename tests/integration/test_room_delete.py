"""``room.delete`` (``switchboard rooms delete``) against an in-process broker, whose
``AllowAllHumans`` policy grants ``login`` (DESIGN.md §28.6; spec #16 §14 item 16)."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import InProcBroker, cookie_of, ws_connect
from fakes.fake_agent import FakeAgent
from switchboard import db
from switchboard.cli import main
from switchboard.config import Config
from switchboard.mcp.client import RpcError

FAST = Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0)
TABLES = ("rooms", "messages", "memberships", "deliveries", "batches", "events")


@pytest.fixture
def broker(tmp_home: Path):
    b = InProcBroker(tmp_home, FAST).start()
    web = b.web_client()
    for name in ("#build", "#other"):
        r = web.post("/api/rooms", json={"name": name}, headers=b.write_headers())
        assert r.status_code == 200
    b.web = web
    yield b
    web.close()
    b.stop()


def web_post(b: InProcBroker, path: str, body: dict[str, Any]) -> dict[str, Any]:
    r = b.web.post(path, json=body, headers=b.write_headers())
    assert r.status_code == 200, r.text
    return r.json()


def create(b: InProcBroker, name: str) -> int:
    return web_post(b, "/api/rooms", {"name": name})["room"]["id"]


def close(b: InProcBroker, slug: str) -> None:
    assert web_post(b, f"/api/rooms/{slug}/command", {"text": "/close"})["ok"]


def rpc_err(b: InProcBroker, params: dict[str, Any]) -> RpcError:
    with pytest.raises(RpcError) as ei:
        b.call("room.delete", params, timeout=30)
    return ei.value


def q(path: Path, sql: str, *args: Any) -> list[tuple[Any, ...]]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def room_rows(path: Path, rid: int, mids: list[int]) -> dict[str, int]:
    marks = ",".join("?" * len(mids)) or "NULL"
    return {
        "rooms": q(path, "SELECT COUNT(*) FROM rooms WHERE id=?", rid)[0][0],
        "messages": q(path, "SELECT COUNT(*) FROM messages WHERE room_id=?", rid)[0][0],
        "memberships": q(path, "SELECT COUNT(*) FROM memberships WHERE room_id=?", rid)[0][0],
        "deliveries": q(path, f"SELECT COUNT(*) FROM deliveries WHERE membership_id IN ({marks})", *mids)[0][0],
        "batches": q(path, f"SELECT COUNT(*) FROM batches WHERE membership_id IN ({marks})", *mids)[0][0],
        "events": q(path, "SELECT COUNT(*) FROM events WHERE room_id=?", rid)[0][0],
    }


def add_offline_member(b: InProcBroker, room_id: int, name: str) -> None:
    """A member whose agent is offline: still a member, so the delete is refused."""
    def go() -> None:
        con = b.state.store.con
        with db.tx(con):
            cur = con.execute("INSERT INTO participants(harness, session_key, status, created_at)"
                              " VALUES('test', ?, 'offline', 0)", (f"test:{name}",))
            con.execute("INSERT INTO memberships(room_id, participant_id, screen_name, cred_hash, joined_at,"
                        " join_msg_id) VALUES(?,?,?,?,0,?)",
                        (room_id, cur.lastrowid, name, "h" * 64, b.state.store.last_message_id(room_id)))
    b.on_loop(go)


def recv_until(ws: Any, pred: Any, timeout: float = 5.0) -> list[dict[str, Any]]:
    got: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        assert left > 0, f"timed out waiting for a frame: {got}"
        f = json.loads(ws.recv(timeout=left))
        got.append(f)
        if pred(f):
            return got


async def test_plan_refusals_close_then_delete(broker: InProcBroker, capsys: pytest.CaptureFixture[str]) -> None:
    b = broker
    dbp = b.paths.db
    async with FakeAgent(b.home, "ka") as a:
        assert (await a.join("#build", "alpha"))["ok"]
        web_post(b, "/api/rooms/build/say", {"text": "@alpha hello"})
        # refused while alpha is in it, the plan too
        e = rpc_err(b, {"room": "#build", "dry_run": True})
        assert e.code == "conflict" and e.message == (
            "#build has 1 agent(s) (alpha): close it first (/close in the web UI, or switchboard cmd '#build' /close)")
        mids = [r[0] for r in q(dbp, "SELECT id FROM memberships WHERE room_id=1")]
        close(b, "build")
    other_before = room_rows(dbp, 2, [])
    # the plan: counts and the backup path, and nothing written
    plan = b.call("room.delete", {"room": "#build", "dry_run": True}, timeout=30)
    backup = Path(plan["backup"])
    assert (plan["room_id"], plan["name"], plan["display"], plan["state"], plan["closed_by"]) == (
        1, "#build~closed-1", "#build", "closed", "alice")
    assert backup == dbp.with_name(f"{dbp.name}.delete-build-1.bak") and not backup.exists()
    assert plan["counts"] == room_rows(dbp, 1, mids)
    assert plan["counts"]["rooms"] == 1 and plan["counts"]["messages"] >= 4 and plan["counts"]["deliveries"] >= 1
    # a stale pin: conflict, nothing deleted
    e = rpc_err(b, {"room": "#build", "room_id": 99})
    assert e.code == "conflict" and "#build~closed-1 changed since the plan" in e.message
    e = rpc_err(b, {"room": "#build"})
    assert e.code == "bad_request" and "room_id is required" in e.message
    assert room_rows(dbp, 1, mids)["rooms"] == 1

    ws = ws_connect(b, cookie_of(b.web))
    try:
        ws.send(json.dumps({"t": "hello", "rooms": ["#other"], "after": {}}))
        recv_until(ws, lambda f: f.get("t") == "members")
        res = b.call("room.delete", {"room": "#build", "room_id": 1}, timeout=60)
        assert (res["room_id"], res["name"], res["display"]) == (1, "#build~closed-1", "#build")
        assert res["removed"] == plan["counts"] and Path(res["backup"]) == backup
        frames = recv_until(ws, lambda f: f.get("t") == "notice" and "deleted via cli" in f.get("text", ""))
        assert any(f.get("t") == "rooms" and f["rooms"] == ["#other"] for f in frames)
        assert frames[-1]["level"] == "warn"
        assert frames[-1]["text"].startswith("#build~closed-1 deleted via cli (")
        assert frames[-1]["text"].endswith(f"); backup {backup.name}")
    finally:
        ws.close()
    # the backup: 0600, checked, and it still has the room
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600
    assert q(backup, "PRAGMA integrity_check") == [("ok",)]
    assert q(backup, "SELECT name FROM rooms WHERE id=1") == [("#build~closed-1",)]
    assert q(backup, "SELECT COUNT(*) FROM messages WHERE room_id=1")[0][0] == plan["counts"]["messages"]
    # gone from the live database; the other room untouched
    assert room_rows(dbp, 1, mids) == dict.fromkeys(TABLES, 0)
    assert room_rows(dbp, 2, []) == other_before
    assert b.call("room.list", {"closed": True}) == {"rooms": [], "closed": 0}
    assert [r["name"] for r in b.call("room.list")["rooms"]] == ["#other"]
    capsys.readouterr()
    assert main(["report", "--room", "#build", "--home", str(b.home)]) == 1
    assert "no room #build" in capsys.readouterr().err


def test_refused_with_an_offline_member(broker: InProcBroker) -> None:
    add_offline_member(broker, 1, "sleeper")
    e = rpc_err(broker, {"room": "#build", "dry_run": True})
    assert e.code == "conflict" and "#build has 1 agent(s) (sleeper): close it first" in e.message
    close(broker, "build")  # a close ends it too
    assert broker.call("room.delete", {"room": "#build", "dry_run": True})["state"] == "closed"


def test_ambiguous_names_and_delete_by_full_name(broker: InProcBroker) -> None:
    b = broker
    close(b, "build")
    create(b, "#build")
    close(b, "build")
    e = rpc_err(b, {"room": "#build", "dry_run": True})
    assert e.code == "bad_request"
    assert e.message == ("#build names 2 closed rooms: #build~closed-3, #build~closed-1;"
                         " give the full name of the one to delete")
    plan = b.call("room.delete", {"room": "#build~closed-1", "dry_run": True})
    assert plan["room_id"] == 1
    assert b.call("room.delete", {"room": "#build~closed-1", "room_id": 1}, timeout=60)["room_id"] == 1
    assert [r["name"] for r in b.call("room.list", {"closed": True})["rooms"]] == ["#build~closed-3"]
    e = rpc_err(b, {"room": "#nope", "dry_run": True})
    assert e.code == "not_found" and "no such room: #nope" in e.message
    e = rpc_err(b, {"room": "bad name!", "dry_run": True})
    assert e.code == "bad_request" and "or a closed room's full name" in e.message


def test_an_empty_open_room_is_deleted_directly(broker: InProcBroker) -> None:
    b = broker
    plan = b.call("room.delete", {"room": "#other", "dry_run": True})
    assert (plan["state"], plan["closed_at"], plan["closed_by"]) == ("open", None, None)
    b.call("room.delete", {"room": "#other", "room_id": plan["room_id"]}, timeout=60)
    assert [r["name"] for r in b.web.get("/api/rooms").json()["rooms"]] == ["#build"]


def test_a_failed_backup_deletes_nothing(broker: InProcBroker, monkeypatch: pytest.MonkeyPatch) -> None:
    b = broker
    close(b, "build")
    before = room_rows(b.paths.db, 1, [])

    def boom(*a: Any, **k: Any) -> Any:
        raise db.SchemaError("disk said no")

    monkeypatch.setattr(db, "backup_verified", boom)
    e = rpc_err(b, {"room": "#build", "room_id": 1})
    assert e.code == "internal" and e.message == "the backup failed (disk said no); nothing was deleted"
    assert room_rows(b.paths.db, 1, []) == before and before["rooms"] == 1
