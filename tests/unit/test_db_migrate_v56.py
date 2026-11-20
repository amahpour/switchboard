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


def at_55_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: list[tuple[int, str]]) -> Path:
    """A database at 55 whose room boards are exactly ``rows`` (room id, raw elements text)."""
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
            for room, raw in rows:
                con.execute(
                    "INSERT INTO room_boards(room_id, seq, elements, updated_at) VALUES(?, 3, ?, 1.0)",
                    (room, raw),
                )
        con.close()
    return path


def test_a_stored_board_that_is_not_a_board_is_cleared_and_its_neighbour_is_still_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row that is not JSON, or JSON that is not a list, is not a board (``Store.board_load`` reads it as
    empty): it is cleared, since nothing in it can be checked, and the upgrade goes on to clean the board next
    to it. A change record whose ids are not a list is cleared the same way. Fails if the JSON or list check
    is removed (the upgrade aborts), or the neighbour keeps a canary."""
    good = json.dumps([el("plain", text=f"uses {TOKEN}", originalText=f"uses {TOKEN}")])
    path = at_55_with(
        tmp_path,
        monkeypatch,
        [(1, good), (2, f"not json {TOKEN}"), (3, json.dumps({"elements": f"object {TOKEN}"}))],
    )
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO room_board_changes(room_id, seq, actor, at, ids, n) VALUES(1, 1, 'a', 1.0, ?, 1)",
        (f"[not {TOKEN}",),
    )
    con.execute(
        "INSERT INTO room_board_changes(room_id, seq, actor, at, ids, n) VALUES(1, 2, 'a', 1.0, ?, 1)",
        (json.dumps({"x": TOKEN}),),
    )
    con.commit()
    con.close()
    con = db.open_db(path)
    try:
        rows = {r[0]: r[1] for r in con.execute("SELECT room_id, elements FROM room_boards")}
        assert rows[2] == "[]" and rows[3] == "[]"
        assert json.loads(rows[1])[0]["text"] == "uses [redacted: github token]"
        assert [r[0] for r in con.execute("SELECT ids FROM room_board_changes ORDER BY seq")] == ["[]", "[]"]
    finally:
        con.close()
    backup = db.backup_path_for(path, 55)
    for f in tmp_path.rglob("*"):
        if f.is_file() and f != backup:
            assert TOKEN.encode() not in f.read_bytes(), f.name


def board_over_the_limit(word: str, secret: str) -> list[dict[str, Any]]:
    """A board whose size is just under the limit as stored and over it once its short secrets are
    markers: many
    unchanged shapes, and changed ones (``word`` plus a short secret in ``text`` and ``originalText``)."""
    changed = [
        el(f"c{i}", type="text", text=f"{word} {secret}", originalText=f"{word} {secret}")
        for i in range(1500)
    ]
    each = board_bytes([el("u00000", fontSize=16, strokeColor="#000000")]) + 1
    fill = (985_000 - board_bytes(changed)) // each  # the unchanged shapes that fill the rest
    scene = [*changed, *(el(f"u{n:05d}", fontSize=16, strokeColor="#000000") for n in range(fill))]
    return scene


def board_bytes(scene: list[dict[str, Any]]) -> int:
    return len(json.dumps(scene, ensure_ascii=False, separators=(",", ":")).encode())


@pytest.mark.parametrize("word", ["note", "\u30e1\u30e2" * 20], ids=["ascii", "multibyte"])
def test_a_board_near_the_limit_still_fits_after_the_cleaning_and_unchanged_shapes_are_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, word: str
) -> None:
    """Cleaning grows a board: a short secret becomes a longer marker. The scene must still fit the byte limit
    the live write path and the browser use, in the column's character count and in encoded bytes (multibyte
    text passes the first and not the second), and the upgrade must not roll back. Only the changed shapes are
    touched, and the unchanged ones are as stored. Fails if the migrated board is over the limit (the upgrade
    aborts on the column's CHECK, or the browser refuses the whole board) or an unchanged shape is altered."""
    scene = board_over_the_limit(word, "password=" + "ab" * 4)
    assert board_bytes(scene) <= board.MAX_BYTES
    cleaned, _ = board.clean_stored(scene)
    assert board_bytes(cleaned) > board.MAX_BYTES  # the problem is real
    path = at_55_with(
        tmp_path, monkeypatch, [(1, json.dumps(scene, ensure_ascii=False, separators=(",", ":")))]
    )
    con = db.open_db(path)
    try:
        assert db.schema_version(con) == 56
        raw = con.execute("SELECT elements FROM room_boards WHERE room_id=1").fetchone()[0]
        assert len(raw.encode()) <= board.MAX_BYTES and len(raw) <= 1_000_000
        got = {e["id"]: e for e in json.loads(raw)}
        assert all(got[e["id"]] == e for e in scene if e["id"].startswith("u"))
        assert any(i.startswith("c") for i in got)  # changed shapes that fit are kept, with their marker
        assert "ab" * 4 not in raw.replace("[redacted", "")
    finally:
        con.close()


def test_a_legacy_wrapped_text_whose_wrap_split_a_token_is_replaced_by_a_marker() -> None:
    """A legacy text with line breaks and no ``originalText`` is unverified: the wrap may have broken a
    token, or
    the space before it, where no pattern matches, so as written it looks clean and copying it into
    ``originalText`` would make the pieces authoritative. It is read flattened every way (each break as
    nothing
    and as a space, in every combination, up to 8 breaks), and if a reading finds what the as-written one did
    not remove, the shape keeps one marker naming the kinds instead (kept, not dropped, so the board's layout
    stays). More breaks than can be read, or a text too long, is ambiguous and gets one marker too.
    Fails if any
    piece of the token stays in text or originalText."""
    body = "a1b2c3d4e5f6" * 4
    for wrapped in (
        f"sk-{body[:14]}\n{body[14:]}",
        "Bearer\n" + body,
        "Bearer\n" + body[:14] + "\n" + body[14:],
    ):
        (out,), _ = board.clean_stored([el("w", type="text", text=wrapped)])
        assert out["text"] == out["originalText"] and out["text"].startswith("[redacted: ")
        assert body[:14] not in json.dumps(out) and body[14:] not in json.dumps(out)
    many = "\n".join(["word"] * 10)
    (out,), _ = board.clean_stored([el("w", type="text", text=many)])
    assert out["text"] == "[redacted: wrapped text]"
    (kept,), _ = board.clean_stored([el("w", type="text", text="two\nlines")])  # nothing hidden: kept
    assert kept["text"] == kept["originalText"] == "two\nlines"


def test_the_upgrade_always_cleans_the_files_even_when_every_whiteboard_and_review_board_was_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting a room removes its tables' rows and leaves their bytes in free pages (DESIGN §28.6), and the
    cleaning cannot reach what is not a row. The upgrade schedules the one ``VACUUM`` and checked checkpoint
    whatever survives, so a database whose only whiteboard and review board were deleted before the upgrade
    comes out with no trace of their text. Fails if the deleted text is still in any file but the backup."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    try:
        old.executescript(FIXTURE.read_text())
    finally:
        old.close()
    big = json.dumps(
        [el(f"s{i}", text=f"leaked {TOKEN}", originalText=f"leaked {TOKEN}") for i in range(200)]
    )
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 55)
        con = db.open_db(path)
        with db.tx(con):
            con.execute(
                "INSERT INTO room_boards(room_id, seq, elements, updated_at) VALUES(1, 3, ?, 1.0)", (big,)
            )
            con.execute(
                "INSERT INTO reviews(id, room_id, url, opened_by, opened_at) VALUES(201, 1, ?, 'a', 1.0)",
                ("https://example.com/shop/pull/9",),
            )
            con.execute(
                "INSERT INTO review_items(id, review_id, kind, n, state, title, detail, raised_by,"
                " created_at, updated_at)"
                " VALUES(201, 201, 'finding', 1, 'raised', 't', ?, 'a', 1.0, 1.0)",
                (f"prints {TOKEN} " * 70,),
            )
        with db.tx(con):  # the room's boards deleted, as room deletion does
            con.execute("DELETE FROM room_boards")
            con.execute("DELETE FROM review_items WHERE review_id=201")
            con.execute("DELETE FROM reviews WHERE id=201")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
    assert TOKEN.encode() in path.read_bytes()  # the deleted text is still in a free page of the file
    con = db.open_db(path)
    try:
        assert con.execute("SELECT count(*) FROM room_boards").fetchone()[0] == 0
        assert not con.execute("SELECT 1 FROM meta WHERE key=?", (db.CLEAN_PENDING,)).fetchone()
    finally:
        con.close()
    backup = db.backup_path_for(path, 55)
    assert backup.exists()
    for f in tmp_path.rglob("*"):
        if f.is_file() and f != backup:
            assert TOKEN.encode() not in f.read_bytes(), f.name
