"""/close, reopen and the service side of `switchboard rooms delete` (DESIGN.md §28)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from test_commands import CLI, WEB, Rec, rec, svc  # noqa: F401  (fixtures)
from test_store import add_agent
from switchboard import db
from switchboard.broker.commands import Actor, CommandError, HELP_TEXT, parse_command, required_role
from switchboard.broker.hub import Subscriber
from switchboard.broker.service import RoomService, ServiceError
from switchboard.store import Conflict, StoreError


class StubDelivery:
    """Records what RoomService tells the delivery side."""

    def __init__(self) -> None:
        self.ended: list[tuple[list[int], str]] = []

    def on_message(self, msg: Any) -> None:
        pass

    def on_command(self, room: Any, name: str, membership_id: int | None) -> None:
        pass

    def on_membership_ended(self, membership_id: int, reason: str) -> None:
        self.ended.append(([membership_id], reason))

    def on_memberships_ended(self, membership_ids: list[int], reason: str) -> None:
        self.ended.append((list(membership_ids), reason))

    def parked_reason(self, membership_id: int) -> str | None:
        return None


class Follower(Subscriber):
    def __init__(self, *rooms: str) -> None:
        super().__init__()
        self.rooms = set(rooms)

    def format(self, kind: str, room: str | None, data: dict[str, Any]) -> Any:
        return (kind, room, data)


@pytest.fixture
def stub(svc: RoomService) -> StubDelivery:  # noqa: F811
    s = StubDelivery()
    svc.delivery = s
    return s


def remote_agent(svc: RoomService, room_id: int, name: str, host: str = "fpga-pi") -> tuple[int, int]:
    pid, mid = add_agent(svc.store, room_id, name)
    with db.tx(svc.store.con):
        svc.store.con.execute("UPDATE participants SET host=? WHERE id=?", (host, pid))
    return pid, mid


# ---------------------------------------------------------------- parsing and roles
def test_close_parses_and_needs_human_cli(svc: RoomService) -> None:  # noqa: F811
    assert parse_command("/close").name == "close" and parse_command("  /CLOSE ").name == "close"
    with pytest.raises(CommandError) as e:
        parse_command("/close now")
    assert (e.value.code, e.value.message) == ("bad_request", "/close takes no arguments")
    assert required_role(parse_command("/close"), svc.room("#build")) == "human_cli"
    with pytest.raises(ServiceError) as e2:
        svc.command("#build", "/close", Actor(role="anon", via="cli"))
    assert e2.value.code == "forbidden"
    assert svc.room("#build")  # still open


def test_help_lists_close() -> None:
    assert ("  /close              close this room: every agent leaves, the history is kept;\n"
            "                      reopen it from Closed rooms in the web UI\n") in HELP_TEXT


# ---------------------------------------------------------------- /close
def test_close_ends_every_member_and_publishes_under_the_old_name(
    svc: RoomService, rec: Rec, stub: StubDelivery  # noqa: F811
) -> None:
    room = svc.room("#build")
    _pa, ma = add_agent(svc.store, room.id, "claude-1")
    _pb, mb = add_agent(svc.store, room.id, "codex-1")
    _pc, mc = remote_agent(svc, room.id, "bench")
    rec.items.clear()
    res = svc.command("#build", "/close", WEB)
    assert res == {"ok": True, "text": "closed #build: 3 agent(s) removed (1 on fpga-pi); history kept."
                                       " The name is free again; reopen this room from Closed rooms in the web UI"}
    for mid in (ma, mb, mc):
        m = svc.store.get_membership(mid)
        assert m.left_reason == "closed" and not m.kicked and m.left_at is not None
    closed = svc.store.room_by_id(room.id)
    assert closed.name == f"#build~closed-{room.id}" and closed.closed
    # the frames: three leave lines and the notice under #build, then the rooms frame without it
    kinds = [(k, r) for k, r, _d in rec.items]
    msgs = [d["msg"] for k, _r, d in rec.items if k == "msg"]
    assert [(m["kind"], m["from"], m["text"], m["host"]) for m in msgs] == [
        ("leave", "claude-1", "left (#build closed)", None),
        ("leave", "codex-1", "left (#build closed)", None),
        ("leave", "bench", "left (#build closed)", "fpga-pi"),
        ("notice", "switchboard", "#build closed by alice (via web): 3 agent(s) removed; the history is kept", None),
    ]
    assert all(r == "#build" for k, r in kinds if k == "msg")
    last_msg = max(i for i, (k, _r) in enumerate(kinds) if k == "msg")
    rooms_frames = [(i, d) for i, (k, _r, d) in enumerate(rec.items) if k == "rooms"]
    assert rooms_frames and rooms_frames[0][0] > last_msg and rooms_frames[0][1] == {"rooms": []}
    ev = svc.store.latest_close_event(room.id)
    assert ev.data == {"name": "#build", "closed_name": closed.name, "by": "alice", "via": "web", "chain": None,
                       "members": [ma, mb, mc]}
    assert stub.ended == [([ma, mb, mc], "closed")]
    # the kept credential names the room; it authorizes nothing
    assert svc.store.membership_by_cred("h" * 64) is None
    assert svc.store.closed_membership_by_cred("h" * 64)[1].id == room.id


def test_close_over_the_cli_leaves_one_audit_notice(svc: RoomService, rec: Rec) -> None:  # noqa: F811
    room = svc.room("#build")
    res = svc.command("#build", "/close", CLI)
    assert res["text"].startswith("closed #build: 0 agent(s) removed; history kept.")
    notices = [m.text for m in svc.store.history(room.id) if m.kind == "notice"]
    assert notices[-1] == "#build closed by alice (via cli: zsh ← Terminal): 0 agent(s) removed; the history is kept"
    assert not [n for n in notices if n.startswith("/close by")]


def test_after_a_close_the_name_is_free(svc: RoomService, rec: Rec) -> None:  # noqa: F811
    old = svc.room("#build")
    web = Follower("#build")
    svc.hub.add(web)
    svc.command("#build", "/close", WEB)
    assert web.rooms == set()  # hub.drop_room
    with pytest.raises(ServiceError) as e:
        svc.room("#build")
    assert (e.value.code, e.value.message) == (
        "not_found", "no such room: #build (it is closed: reopen it from Closed rooms in the web UI)")
    with pytest.raises(ServiceError) as e:
        svc.room("#other")
    assert e.value.message == "no such room: #other"
    new = svc.create_room("#build")
    assert new.id != old.id
    assert [(r["name"], r["id"]) for r in svc.rooms()] == [("#build", new.id)]
    assert svc.status()["closed_rooms"] == 1
    with pytest.raises(ServiceError) as e:
        svc.close_room(svc.store.room_by_id(old.id), WEB)  # defensive: room() never returns it
    assert (e.value.code, e.value.message) == ("conflict", "#build is already closed")


def test_closed_room_dicts_and_reopen(svc: RoomService, rec: Rec, clock: Any) -> None:  # noqa: F811
    room = svc.room("#build")
    pk, mk = add_agent(svc.store, room.id, "kicked-1")
    svc.command("#build", "/kick kicked-1", WEB)
    pc, mc = add_agent(svc.store, room.id, "claude-1")
    svc.human_say("#build", "keep me", via="web")
    svc.command("#build", "/close", WEB)
    new = svc.create_room("#build")
    [d] = svc.closed_room_dicts()
    ev = svc.store.latest_close_event(room.id)
    assert d == {"id": room.id, "name": f"#build~closed-{room.id}", "display": "#build",
                 "created_at": room.created_at, "closed_at": ev.ts, "closed_by": "alice",
                 "messages": svc.store.count_messages(room.id), "reopenable": False}
    with pytest.raises(ServiceError) as e:
        svc.reopen_room(room.id)
    assert (e.value.code, e.value.http_status, e.value.message) == (
        "conflict", 409, "#build is taken by an open room: close or delete that room first, then reopen this one")
    with pytest.raises(ServiceError) as e:
        svc.reopen_room(new.id)  # an open room
    assert (e.value.code, e.value.http_status, e.value.message) == (
        "not_found", 404, f"no closed room with id {new.id}")
    svc.command("#build", "/close", WEB)
    assert [x["reopenable"] for x in svc.closed_room_dicts()] == [True, True]
    rec.items.clear()
    back = svc.reopen_room(room.id)
    assert back.id == room.id and back.name == "#build"
    assert svc.room("#build").id == room.id
    texts = [m.text for m in svc.store.history(room.id)]
    assert "keep me" in texts and texts[-1] == "#build reopened by alice (via web); agents join() it again"
    [ev] = svc.store.recent_events(room_id=room.id, kinds=["room_reopen"])
    assert ev.data == {"name": "#build", "from": f"#build~closed-{room.id}", "by": "alice", "via": "web"}
    kinds = [k for k, _r, _d in rec.items]
    assert kinds.index("msg") < kinds.index("rooms")
    assert [d for k, _r, d in rec.items if k == "rooms"][-1] == {"rooms": ["#build"]}
    # nobody is re-added; the kick still holds, the closed member may join again
    assert svc.store.members(room.id) == []
    assert svc.store.was_kicked(room.id, pk) and not svc.store.was_kicked(room.id, pc)
    assert svc.store.closed_membership_by_cred("h" * 64) is None  # the kept credentials are cleared
    assert mk and mc


# ---------------------------------------------------------------- delete (service side)
def delete(svc: RoomService, ref: str, tmp_path: Path, **kw: Any) -> dict[str, Any]:
    return svc.delete_room(ref, dry_run=kw.pop("dry_run", False), room_id=kw.pop("room_id", None),
                           db_path=tmp_path / "y.db", chain="zsh ← Terminal", **kw)


def test_delete_refusals(svc: RoomService, tmp_path: Path) -> None:  # noqa: F811
    room = svc.room("#build")
    add_agent(svc.store, room.id, "claude-1")
    remote_agent(svc, room.id, "bench")
    with pytest.raises(ServiceError) as e:
        delete(svc, "#build", tmp_path, dry_run=True)
    assert (e.value.code, e.value.message) == (
        "conflict", "#build has 2 agent(s) (claude-1, bench@fpga-pi): close it first"
                    " (/close in the web UI, or switchboard cmd '#build' /close)")
    with pytest.raises(ServiceError) as e:
        delete(svc, "#Bad Name!", tmp_path, dry_run=True)
    assert e.value.code == "bad_request" and e.value.message.endswith(
        " (or a closed room's full name, e.g. #build~closed-7)")
    with pytest.raises(ServiceError) as e:
        delete(svc, "#nope", tmp_path, dry_run=True)
    assert (e.value.code, e.value.message) == ("not_found", "no such room: #nope")
    svc.command("#build", "/close", WEB)
    svc.create_room("#build")
    svc.command("#build", "/close", WEB)
    names = [r.name for r in svc.store.list_rooms(closed=True)]
    with pytest.raises(ServiceError) as e:
        delete(svc, "build", tmp_path, dry_run=True)
    assert (e.value.code, e.value.message) == (
        "bad_request", f"#build names 2 closed rooms: {', '.join(names)}; give the full name of the one to delete")
    with pytest.raises(ServiceError) as e:
        delete(svc, names[0], tmp_path, room_id=room.id)
    assert (e.value.code, e.value.message) == (
        "conflict", f"{names[0]} changed since the plan (reopened, deleted or re-created); run the command again")
    assert not list(tmp_path.glob("*.delete-*"))


def test_delete_a_closed_room(svc: RoomService, rec: Rec, tmp_path: Path) -> None:  # noqa: F811
    room = svc.room("#build")
    add_agent(svc.store, room.id, "claude-1")
    svc.human_say("#build", "old news", via="web")
    svc.command("#build", "/close", WEB)
    keep = svc.create_room("#keep")
    name = f"#build~closed-{room.id}"
    plan = delete(svc, "#build", tmp_path, dry_run=True)
    counts = svc.store.room_delete_counts(room.id)
    assert plan == {"room_id": room.id, "name": name, "display": "#build", "state": "closed",
                    "created_at": room.created_at, "closed_at": svc.store.latest_close_event(room.id).ts,
                    "closed_by": "alice", "counts": counts,
                    "backup": str(tmp_path / f"y.db.delete-build-{room.id}.bak")}
    assert not Path(plan["backup"]).exists()
    rec.items.clear()
    res = delete(svc, name, tmp_path, room_id=room.id)
    assert res["removed"] == counts and res["name"] == name and res["display"] == "#build"
    backup = Path(res["backup"])
    assert backup.exists() and (os.stat(backup).st_mode & 0o777) == 0o600
    with sqlite3.connect(backup) as b:
        assert b.execute("SELECT COUNT(*) FROM rooms WHERE id=?", (room.id,)).fetchone()[0] == 1
    assert svc.store.room_by_id(room.id) is None and svc.store.room_by_id(keep.id) is not None
    assert ("rooms", None, {"rooms": ["#keep"]}) in rec.items
    assert ("notice", None, {"level": "warn",
                             "text": f"{name} deleted via cli (zsh ← Terminal); backup {backup.name}"}) in rec.items


def test_delete_an_empty_open_room(svc: RoomService, tmp_path: Path) -> None:  # noqa: F811
    web = Follower("#build")
    svc.hub.add(web)
    plan = delete(svc, "#build", tmp_path, dry_run=True)
    assert plan["state"] == "open" and plan["closed_at"] is None and plan["closed_by"] is None
    delete(svc, "#build", tmp_path, room_id=plan["room_id"])
    assert svc.rooms() == [] and web.rooms == set()


def test_delete_is_refused_when_the_planned_closed_room_was_reopened(
    svc: RoomService, tmp_path: Path  # noqa: F811
) -> None:
    """A reopen keeps the id and only changes the name: the pin must catch it, or the room the
    human just reopened would go although the plan showed a closed one."""
    room = svc.room("#build")
    svc.command("#build", "/close", WEB)
    plan = delete(svc, "#build", tmp_path, dry_run=True)
    assert (plan["state"], plan["name"]) == ("closed", f"#build~closed-{room.id}")
    svc.reopen_room(room.id)
    assert svc.store.resolve_room("#build").id == plan["room_id"]  # the same id, open again
    with pytest.raises(ServiceError) as e:
        delete(svc, "#build", tmp_path, room_id=plan["room_id"], name=plan["name"], created_at=plan["created_at"])
    assert (e.value.code, e.value.message) == (
        "conflict", f"#build~closed-{room.id} changed since the plan (reopened, deleted or re-created);"
                    " run the command again")
    assert svc.room("#build").id == room.id  # still there, open
    assert not list(tmp_path.glob("*.delete-*"))


def test_delete_is_refused_for_a_re_created_room_that_reused_the_id(
    svc: RoomService, clock: Any, tmp_path: Path  # noqa: F811
) -> None:
    """Room ids have no AUTOINCREMENT: deleting the highest one frees it for the next room.
    A stale plan for the deleted room must not delete the new one."""
    old = svc.create_room("#z")
    plan = delete(svc, "#z", tmp_path, dry_run=True)
    pin = {"room_id": plan["room_id"], "name": plan["name"], "created_at": plan["created_at"]}
    delete(svc, "#z", tmp_path, **pin)
    clock.advance(5)
    new = svc.create_room("#z")
    assert new.id == old.id and new.name == old.name  # the id came back
    with pytest.raises(ServiceError) as e:
        delete(svc, "#z", tmp_path, **pin)
    assert e.value.code == "conflict" and e.value.message.startswith("#z changed since the plan")
    assert svc.room("#z").created_at == new.created_at


def test_delete_backup_or_store_failure_deletes_nothing(
    svc: RoomService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    room = svc.room("#build")

    def boom(*a: Any, **k: Any) -> Any:
        raise db.SchemaError("disk full")

    real = db.backup_verified
    monkeypatch.setattr(db, "backup_verified", boom)
    with pytest.raises(ServiceError) as e:
        delete(svc, "#build", tmp_path, room_id=room.id)
    assert (e.value.code, e.value.message) == ("internal", "the backup failed (disk full); nothing was deleted")
    monkeypatch.setattr(db, "backup_verified", real)
    for exc, code in ((Conflict("x changed"), "conflict"), (StoreError("bad"), "internal")):
        def fail(*a: Any, _exc: Exception = exc, **k: Any) -> Any:
            raise _exc

        monkeypatch.setattr(svc.store, "delete_room", fail)
        with pytest.raises(ServiceError) as e:
            delete(svc, "#build", tmp_path, room_id=room.id)
        assert e.value.code == code and e.value.message.startswith(f"{exc}; nothing was deleted (backup: y.db.delete-")
    assert svc.room("#build").id == room.id
