"""Schema 56 (#893, DESIGN.md §11 item 10, §56.5a): a room's whiteboard is cleaned once, by removing what
held a secret.  The step from 55 has no schema change. A stored shape that the cleaning of a write would
change in any way (a secret in its text, original text or name; a legacy wrapped text without its
original words; an id or reference that is a secret; a tombstone with text) is removed, with the
references to it and its id in the change records, and a shape that needs no change is kept as it is, so
a board can only shrink. The step runs under ``secure_delete`` and finishes with the same VACUUM and
checked checkpoint as schema 55, once for both, whatever survives. Chat is not rewritten, and the backup
the migration writes first holds the old data: it is the one file the scans leave out. Every secret is
built from pieces, so no scanner reads this file as a leak."""

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
BODY = "a1b2c3d4e5f6" * 4
TEN = "\n".join(f"line {i}" for i in range(10))  # an ordinary ten-line label: more than eight breaks
CANARIES = (TOKEN, TOKEN.split("_", 1)[1], PASSWORD, SECRET_ID, GROUP, BIND, BODY)


def el(eid: str, **over: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": eid, "type": "rectangle", "x": 0, "y": 0, "width": 100, "height": 50, "version": 1,
        "versionNonce": 1, "isDeleted": False, "link": None,
    }  # fmt: skip
    out.update(over)
    return out


def stored_scene() -> list[dict[str, Any]]:
    """What an earlier release could have stored: a secret in each text field, a legacy wrapped text, a
    secret id, a tombstone with text, and shapes that refer to them; and shapes that need nothing."""
    return [
        el("clean1", text="a note", originalText="a note", name="frame one"),
        el("wrapped", type="text", text="two\nlines", originalText="two lines"),  # a normal wrapped label
        el("ten_equal", type="text", text=TEN, originalText=TEN),  # nine breaks, text equal to its original
        el("ten_wrapped", type="text", text=TEN, originalText=TEN.replace("\n", " ")),  # a wrapped copy
        el("secret_text", text=f"uses {TOKEN}", originalText="uses it"),
        el("secret_original", text="uses it", originalText=f"uses {TOKEN}"),
        el("secret_name", type="frame", name=f"frame password={PASSWORD}x"),
        el("legacy", type="text", text="one\ntwo"),  # no originalText
        el("split", type="text", text=f"sk-{BODY[:14]}\n{BODY[14:]}", originalText="x"),  # a wrap hid a token
        el("gone", isDeleted=True, text=f"old {TOKEN}", originalText=f"old {TOKEN}"),
        el(SECRET_ID, boundElements=[{"id": "arrow1", "type": "arrow"}]),
        el("grp", groupIds=[GROUP, "g2"]),
        el("bound", type="text", containerId="clean1", frameId=BIND, text="inside"),
        el(
            "arrow1",
            type="arrow",
            points=[[0, 0], [5, 5]],
            startBinding={"elementId": "secret_text", "focus": 0, "gap": 1},
            endBinding={"elementId": "clean1", "focus": 0, "gap": 1},
        ),  # fmt: skip
        el("label", type="text", containerId="secret_name", text="in", originalText="in"),
        el(
            "holder",
            frameId="legacy",
            groupIds=["grp", "g3"],
            boundElements=[{"id": "split", "type": "text"}, {"id": "clean1", "type": "arrow"}],
        ),  # fmt: skip
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
            for seq, ids in enumerate((["clean1", SECRET_ID], [GROUP, "grp"], ["label"], [BIND]), start=6):
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


def test_the_upgrade_removes_every_shape_that_held_a_secret_and_what_pointed_at_it() -> None:
    """Each kind of shape the cleaning would change is removed whole, not marked: a secret in ``text``,
    in ``originalText``, in a frame's ``name``; a legacy multi-line ``text`` with no ``originalText``; a
    wrap that hid a token; a tombstone with text; a secret id; a secret group id; a secret in a
    reference. The shapes that held a reference to one lose just that reference (a binding, a container,
    a frame, a group list, a bound list). A shape that needed nothing is kept as it is, wrapped label
    included. Fails if a shape is kept or marked, or a reference is left."""
    scene = stored_scene()
    kept, dropped = board.clean_stored(scene)
    got = {e["id"]: e for e in kept}
    assert dropped == {
        "secret_text", "secret_original", "secret_name", "legacy", "split", "gone", SECRET_ID, "grp", "bound",
    }  # fmt: skip
    assert set(got) == {"clean1", "wrapped", "ten_equal", "ten_wrapped", "arrow1", "label", "holder"}
    assert all(
        got[k] == e
        for e in scene
        for k in [e["id"]]
        if k in got and k in {"clean1", "wrapped", "ten_equal", "ten_wrapped"}
    )
    assert {"ten_equal", "ten_wrapped"} <= set(
        got
    )  # clean multi-line labels are kept, whatever their line count
    assert got["arrow1"]["startBinding"] is None and got["arrow1"]["endBinding"]["elementId"] == "clean1"
    assert "containerId" not in got["label"]  # deleted, not null: "null" can be longer than the id
    assert "frameId" not in got["holder"] and got["holder"]["groupIds"] == ["g3"]
    assert got["holder"]["boundElements"] == [{"id": "clean1", "type": "arrow"}]
    blob = json.dumps(kept)
    for canary in CANARIES:
        assert canary not in blob, canary


def test_the_upgrade_cleans_the_stored_whiteboard_and_its_change_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real upgrade: the removed shapes are gone from the stored scene, the surviving ones
    are byte for byte what was stored (apart from the entries removed), and the change records name no
    removed id and no secret id. Fails if any canary is left, or an untouched shape is rewritten."""
    path = at_55(tmp_path, monkeypatch)
    con = db.open_db(path)
    try:
        assert db.schema_version(con) == db.SCHEMA_VERSION == 65
        raw = con.execute("SELECT elements FROM room_boards WHERE room_id=1").fetchone()[0]
        got = {e["id"]: e for e in json.loads(raw)}
        assert set(got) == {"clean1", "wrapped", "ten_equal", "ten_wrapped", "arrow1", "label", "holder"}
        stored = {e["id"]: e for e in stored_scene()}
        assert got["clean1"] == stored["clean1"] and got["wrapped"] == stored["wrapped"]
        changes = [json.loads(r[0]) for r in con.execute("SELECT ids FROM room_board_changes ORDER BY seq")]
        blob = raw + json.dumps(changes)
        for canary in CANARIES:
            assert canary not in blob, canary
        assert changes == [["clean1"], [], ["label"], []]
        assert not con.execute("SELECT 1 FROM meta WHERE key=?", (db.CLEAN_PENDING,)).fetchone()
        assert db.clean_whiteboard_rows(con) == 0  # run again: nothing left to change
    finally:
        con.close()


def test_a_reference_helper_returns_the_shape_itself_when_nothing_names_a_removed_id() -> None:
    """``drop_references`` keeps the object when it changes nothing (and a copy that has only lost
    entries when it does), as its docstring says."""
    shape = el("a", groupIds=["g1"], boundElements=[{"id": "b", "type": "arrow"}])
    assert board.drop_references(shape, {"zzz"}) is shape
    assert board.drop_references(shape, set()) is shape
    cut = board.drop_references(shape, {"b"})
    assert cut is not shape and cut["boundElements"] == [] and shape["boundElements"] != []


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
    good = json.dumps(
        [el("kept", text="a note", originalText="a note"), el("held", text=f"uses {TOKEN}", originalText="x")]
    )
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
        assert [e["id"] for e in json.loads(rows[1])] == ["kept"]  # the shape that held a secret is gone
        assert [r[0] for r in con.execute("SELECT ids FROM room_board_changes ORDER BY seq")] == ["[]", "[]"]
    finally:
        con.close()
    backup = db.backup_path_for(path, 55)
    for f in tmp_path.rglob("*"):
        if f.is_file() and f != backup:
            assert TOKEN.encode() not in f.read_bytes(), f.name


def test_a_board_near_every_limit_only_shrinks_and_stays_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A board the old broker could have saved at its limits (1,500 shapes, one of them a pen stroke at
    its 32,000-byte limit, the whole scene near 1,000,000 bytes) with a few shapes that held secrets. The
    upgrade only removes shapes and entries, so the migrated board is smaller, within every limit, and
    the shapes that needed nothing are as stored. Fails if the upgrade aborts on the column's limit, or a
    kept shape is altered."""
    stroke = el(
        "stroke", type="freedraw", points=[[-19999.123456789012, -19999.123456789012]] * 480,
        pressures=[0.12345678901234568] * 500,
    )  # fmt: skip
    assert len(json.dumps(stroke, separators=(",", ":")).encode()) <= board.MAX_SHAPE_BYTES
    pad = ["g" + "x" * 60] * 6
    scene = [stroke] + [
        el(f"u{i:04d}", type="text", text="ok", originalText="ok", groupIds=pad, name="n" * 40)
        for i in range(1_480)
    ]
    scene += [el(f"s{i:02d}", text=f"uses {TOKEN}", originalText=f"uses {TOKEN}") for i in range(19)]
    assert len(scene) == 1_500
    raw_in = json.dumps(scene, ensure_ascii=False, separators=(",", ":"))
    assert len(raw_in.encode()) <= board.MAX_BYTES
    path = at_55_with(tmp_path, monkeypatch, [(1, raw_in)])
    con = db.open_db(path)
    try:
        raw = con.execute("SELECT elements FROM room_boards WHERE room_id=1").fetchone()[0]
    finally:
        con.close()
    got = json.loads(raw)
    assert len(got) == 1_481 and len(raw.encode()) < len(raw_in.encode())
    assert got == scene[:1_481]  # the shapes that needed nothing, in order, as stored
    assert all(len(json.dumps(e, separators=(",", ":")).encode()) <= board.MAX_SHAPE_BYTES for e in got)


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


@pytest.mark.parametrize("key", ["frameId", "containerId"])
def test_removing_a_reference_never_grows_a_shape_that_is_at_its_limit(key: str) -> None:
    """A survivor that held a reference to a removed shape loses the reference. Setting it to ``null``
    would grow a one-character id (``"x"``, 3 bytes) to ``null`` (4), and a shape already at 32,000 bytes
    would then be over the live cap and refuse every edit; the key is deleted instead. Fails if the
    survivor grows (or still has the key)."""

    def build(points: int, pad: int, **extra: Any) -> dict[str, Any]:
        return el(
            "stroke", type="freedraw", points=[[-19999.123456789012, -19999.123456789012]] * points,
            pressures=[0.12345678901234568] * 500, index="a" * pad, **extra,
        )  # fmt: skip

    def size(shape: dict[str, Any]) -> int:
        return len(json.dumps(shape, separators=(",", ":")).encode())

    points = 500
    while size(build(points, 1, **{key: "x"})) > 31_900:
        points -= 1
    pad = 1 + 32_000 - size(build(points, 1, **{key: "x"}))
    survivor = build(points, pad, **{key: "x"})
    assert size(survivor) == board.MAX_SHAPE_BYTES  # exactly at the live cap
    assert size({**survivor, key: None}) > board.MAX_SHAPE_BYTES  # the problem is real
    kept, dropped = board.clean_stored([el("x", text=f"uses {TOKEN}"), survivor])
    assert dropped == {"x"} and len(kept) == 1
    assert key not in kept[0] and size(kept[0]) <= board.MAX_SHAPE_BYTES


def kept_by_the_upgrade(text: str, original: str) -> bool:
    shape = el("t", type="text", text=text, originalText=original)
    kept, _ = board.clean_stored([shape])
    assert kept in ([], [shape])  # a shape is kept as it is, or removed: never rewritten
    return bool(kept)


@pytest.mark.parametrize(
    ("text", "original"),
    [
        ("one\ntwo", ""),  # an empty original approves nothing
        ("one\ntwo", "one"),  # a short original that is a prefix of the text
        ("one\ntwo", "one two three"),  # a text cut short of its original's wrap
        ("abc\nd", "ab cd"),  # the break loses a word boundary: the original has a space after "ab"
        ("ab\ncd", "abXcd"),  # other words
        ("one\ntwo", "one two\n"),  # the original is not consumed completely
        ("one\ntwo", "one two three  "),  # a cut text, however many spaces end the original
        ("one\ntwo", "one  x\ntwo"),  # spaces then other characters before the newline: not a trim
        ("abc\nd", "ab cd   "),  # a lost word boundary is not excused by trailing spaces after it
        ("one\ntwo", "one   \ntwo\n"),  # a trimmed typed newline, and the original still not consumed
    ],
)
def test_a_multi_line_text_that_is_not_a_full_wrap_of_its_original_is_removed(
    text: str, original: str
) -> None:
    """The old broker stored ``text`` and ``originalText`` independently, so a prefix, a cut or a lost
    word boundary says nothing about what the display text holds. Only equal strings, or a full wrap read
    in order with both consumed completely, are kept. Fails on a check that lets a prefix, a cut or an
    empty original through."""
    assert not kept_by_the_upgrade(text, original)


@pytest.mark.parametrize(
    ("text", "original"),
    [
        ("one\ntwo", "one\ntwo"),  # equal
        (TEN, TEN),  # ten lines, equal: more than eight breaks is no reason to remove it
        (TEN, TEN.replace("\n", " ")),  # ten lines, a wrap at single spaces
        ("ab\ncd", "abcd"),  # a break inside a word
        ("ab\ncd", "ab   cd"),  # a wrap at a run of spaces: wrapText trims the whole run
        ("ab \ncd", "ab \ncd"),  # a typed newline after a space
        ("ab\ncd\nef", "ab cd\nef"),  # a wrap and a typed newline
        ("one\ntwo", "one   \ntwo"),  # a typed newline whose trailing spaces were trimmed by the wrap
        ("one  \ntwo", "one   \ntwo"),  # ... trimmed in part
        ("one\ntwo\nthree", "one two   \nthree"),  # a wrap, then a trimmed typed newline
        ("one\ntwo", "one two   "),  # the last segment only had its trailing spaces trimmed
        ("one\ntwo", "one\ntwo  "),  # a typed newline, then trailing spaces trimmed
    ],
)
def test_a_multi_line_text_that_is_equal_to_or_a_full_wrap_of_its_original_is_kept_as_it_is(
    text: str, original: str
) -> None:
    """The cases above that must not be lost: nothing to change, whatever the line count. The bundled
    ``wrapText`` splits on typed newlines, wraps each segment alone and can trim the trailing spaces of a
    segment's last line, wholly or in part, so those spaces may be absent before a typed newline or at the
    end. Fails on a walk that consumes the spaces as a wrap and leaves the typed newline unmatched."""
    assert kept_by_the_upgrade(text, original)
