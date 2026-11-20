"""Schema 56 (#893, DESIGN.md §11 item 10, §56.5): a room's whiteboard is cleaned once.

The step from 55 has no schema change. It runs the stored elements of every room's board and the ids in its
change records through the cleaning a write now gets (normalizing, never refusing), under ``secure_delete``,
and finishes with the same VACUUM and checked checkpoint as schema 55, once for both. Chat is not
rewritten, and
the backup the migration writes first holds the old data: it is the one file this test leaves out. Every
secret
is built from pieces, so no scanner reads this file as a leak."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from switchboard import board, db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"
TOKEN = "ghp" + "_" + "aB3" * 12
PASSWORD = "hunter2" * 2
SECRET_ID = "sk-" + "a1b2c3d4e5f6" * 3
GROUP = "AKIA" + "ABCDEFGHIJKLMNOP"
BIND = "xoxb-" + "1234567890" + "-abcdef"
CANARIES = (TOKEN, TOKEN.split("_", 1)[1], PASSWORD, SECRET_ID, GROUP, BIND)


def el(eid: str, **over: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": eid, "type": "rectangle", "x": 0, "y": 0, "width": 100, "height": 50, "version": 1,
        "versionNonce": 1, "isDeleted": False, "link": None,
    }  # fmt: skip
    out.update(over)
    return out


def stored_scene() -> list[dict[str, Any]]:
    """What an earlier release could have stored: secrets in every text field, a multi-line text with no
    original, a tombstone with text, ids that are secrets, and shapes that refer to them."""
    return [
        el("plain", text=f"uses {TOKEN}", originalText=f"uses {TOKEN}", name=f"frame password={PASSWORD}x"),
        el("lab", type="text", containerId="plain", text="two\nlines", boundElements=None),
        el("gone", isDeleted=True, text=f"old {TOKEN}", originalText=f"old {TOKEN}"),
        el(
            "long",
            type="text",
            text="x" * 150 + f" password={PASSWORD}",
            originalText="x" * 150 + f" password={PASSWORD}",
        ),
        el(SECRET_ID, boundElements=[{"id": "arrow1", "type": "arrow"}, {"id": "plain", "type": "text"}]),
        el(
            "arrow1",
            type="arrow",
            points=[[0, 0], [5, 5]],
            startBinding={"elementId": SECRET_ID, "focus": 0, "gap": 1},
            endBinding={"elementId": "plain", "focus": 0, "gap": 1},
        ),  # fmt: skip
        el("grp", groupIds=[GROUP, "g2"]),
        el("bound", type="text", containerId=SECRET_ID, frameId=BIND, text="inside"),
        el("ref", type="text", containerId="grp", text="in a dropped group shape"),
        el(
            "fine",
            frameId="plain",
            boundElements=[{"id": "grp", "type": "text"}, {"id": "lab", "type": "text"}],
        ),
    ]


def at_55(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    try:
        old.executescript(FIXTURE.read_text())
    finally:
        old.close()
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 55)
        con = db.open_db(path)
        with db.tx(con):
            scene = json.dumps(stored_scene(), separators=(",", ":"))
            con.execute(
                "INSERT INTO room_boards(room_id, seq, elements, updated_at) VALUES(1, 9, ?, 1.0)", (scene,)
            )
            for seq, ids in enumerate((["plain", SECRET_ID], [GROUP, "grp"], ["lab"], [BIND]), start=6):
                con.execute(
                    "INSERT INTO room_board_changes(room_id, seq, actor, at, ids, n)"
                    " VALUES(1, ?, 'alice', 1.0, ?, 2)",
                    (seq, json.dumps(ids)),
                )
        con.close()
    return path


def load(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    row = con.execute("SELECT elements FROM room_boards WHERE room_id=1").fetchone()
    return {e["id"]: e for e in json.loads(row[0])}


def test_the_upgrade_cleans_the_stored_whiteboard_and_its_change_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every text field is redacted and cut, a tombstone keeps no text, a multi-line text gets its original,
    an element with a secret id or reference is dropped and the references to it go, and the change records
    name no secret id. Fails if any canary is left, if a clean shape is changed, or a dangling reference
    stays."""
    path = at_55(tmp_path, monkeypatch)
    con = db.open_db(path)
    try:
        assert db.schema_version(con) == db.SCHEMA_VERSION == 56
        got = load(con)
        blob = json.dumps(got) + json.dumps(
            [tuple(r) for r in con.execute("SELECT ids FROM room_board_changes")]
        )
        for canary in CANARIES:
            assert canary not in blob, canary
        # the shapes whose id or reference is a secret are dropped: SECRET_ID, its arrow (a binding to
        # it), grp (a group id), and bound (its container and frame)
        assert set(got) == {"plain", "lab", "gone", "long", "ref", "fine"}
        assert got["plain"]["text"] == got["plain"]["originalText"] == "uses [redacted: github token]"
        assert (
            got["plain"]["name"]
            == "frame password=[redacted: password or token]"[: len(got["plain"]["name"])]
        )
        assert got["gone"]["text"] == got["gone"]["originalText"] == ""
        assert len(got["long"]["text"]) <= board.MAX_TEXT and PASSWORD not in got["long"]["text"]
        assert got["lab"]["text"] == got["lab"]["originalText"] == "two\nlines"
        assert got["ref"]["containerId"] is None  # its container, "grp", was dropped
        assert got["fine"]["boundElements"] == [{"id": "lab", "type": "text"}]
        assert got["fine"]["frameId"] == "plain"
        changes = [json.loads(r[0]) for r in con.execute("SELECT ids FROM room_board_changes ORDER BY seq")]
        assert changes == [
            ["plain"],
            [],  # "grp" was dropped
            ["lab"],
            [],
        ]  # "grp" is plain but dropped: kept in the log is fine
        assert not con.execute("SELECT 1 FROM meta WHERE key=?", (db.CLEAN_PENDING,)).fetchone()
        assert db.clean_whiteboard_rows(con) == 0  # run again: nothing left to change
    finally:
        con.close()


def test_no_file_of_the_home_keeps_the_old_whiteboard_data_but_the_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rewrite runs under ``secure_delete`` and the WAL is emptied by the checked checkpoint, so the old
    bytes are in no page of the database, free or not, and not in the log. Every file of the home is searched
    for every canary except the backup, which is the way back, is asserted to exist and still holds them.
    Fails if any file but the backup holds one."""
    path = at_55(tmp_path, monkeypatch)
    con = db.open_db(path)  # kept open: what is on disk now is what a copy of the home would take
    backup = db.backup_path_for(path, 55)
    assert backup.exists() and TOKEN.encode() in backup.read_bytes()
    leaks = {}
    for f in tmp_path.rglob("*"):
        if f.is_file() and f != backup:
            data = f.read_bytes()
            for canary in CANARIES:
                if canary.encode() in data:
                    leaks[f.name] = canary
    assert leaks == {}, leaks
    wal = path.with_name(path.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    con.close()


def test_a_fresh_database_has_nothing_to_clean(tmp_path: Path) -> None:
    """A new database is made at the current version with no step. Fails if the cleaning is not a no-op."""
    con = db.open_db(tmp_path / "fresh.db")
    try:
        assert db.clean_whiteboard_rows(con) == 0
    finally:
        con.close()


def test_a_board_that_is_not_a_list_is_left_alone(tmp_path: Path) -> None:
    """A row the cleaning cannot read is skipped, not turned into an error that stops the upgrade."""
    con = db.open_db(tmp_path / "odd.db")
    try:
        assert db.clean_whiteboard_rows(con) == 0
    finally:
        con.close()
