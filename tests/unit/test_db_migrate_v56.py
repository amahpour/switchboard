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
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import child_env

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


def board_bytes(scene: list[dict[str, Any]]) -> int:
    return len(json.dumps(scene, ensure_ascii=False, separators=(",", ":")).encode())


def shape_bytes(shape: dict[str, Any]) -> int:
    return len(json.dumps(shape, separators=(",", ":")).encode())


def big_shape(eid: str, **fields: Any) -> dict[str, Any]:
    """A pen stroke the live write path could have stored (500 points and pressures, 16 group ids, 40 bound
    elements: every list at its cap) sized to between 31,985 and 31,998 bytes of its 32,000: so little room
    that a few bytes of growth put it over."""

    def build(points: int, pad: int) -> dict[str, Any]:
        base: dict[str, Any] = {
            "type": "freedraw", "points": [[-19999.123456789012, -19999.123456789012]] * points,
            "pressures": [0.12345678901234568] * 500, "index": "a" * pad,
            "groupIds": [f"g{i:02d}" + "x" * 60 for i in range(16)],
            "boundElements": [{"id": f"b{i:02d}" + "y" * 55, "type": "arrow"} for i in range(40)],
        }  # fmt: skip
        return el(eid, **{**base, **fields})

    points = 500
    while shape_bytes(build(points, 1)) > 31_920:  # a point is about 40 bytes: the index fills the rest
        points -= 1
    for pad in range(1, 121):
        if shape_bytes(build(points, pad)) >= 31_985:
            break
    out = build(points, pad)
    assert 31_985 <= shape_bytes(out) <= 31_998, shape_bytes(out)
    return out


def test_a_shape_the_cleaning_grows_past_its_own_limit_is_shrunk_and_the_board_still_loads_in_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser refuses a whole board when one shape is over 32,000 bytes (the live write path refuses the
    shape), even if the scene fits. A shape at the limit whose short secrets grow into markers is shrunk (its
    grown fields become the shortest marker) and kept; its neighbours are as stored. The migrated scene passes
    the browser's own cleaner (``board-rules.js``). Fails if the scene-wide check alone is made, which returns
    the board as it is and the browser then drops every shape."""
    text = "a password=abcdefgh"
    scene = [big_shape("big", text=text, originalText=text), el("small", text="ok", originalText="ok")]
    assert shape_bytes(scene[0]) <= board.MAX_SHAPE_BYTES and board_bytes(scene) < board.MAX_BYTES
    cleaned, _ = board.clean_stored(scene)
    assert shape_bytes(cleaned[0]) > board.MAX_SHAPE_BYTES  # the problem is real
    path = at_55_with(
        tmp_path, monkeypatch, [(1, json.dumps(scene, ensure_ascii=False, separators=(",", ":")))]
    )
    con = db.open_db(path)
    try:
        raw = con.execute("SELECT elements FROM room_boards WHERE room_id=1").fetchone()[0]
    finally:
        con.close()
    got = {e["id"]: e for e in json.loads(raw)}
    assert shape_bytes(got["big"]) <= board.MAX_SHAPE_BYTES and got["big"]["text"] == "[redacted]"
    assert got["small"] == scene[1]
    rules = Path(__file__).resolve().parents[2] / "src/switchboard/web/static/board-rules.js"
    code = "const r=require(process.argv[1]);const e=JSON.parse(require('fs').readFileSync(0,'utf8'));"
    code += "process.stdout.write(JSON.stringify(r.batch(e, true) === null))"
    out = subprocess.run(
        [shutil.which("node") or "node", "-e", code, str(rules)],
        input=raw, capture_output=True, text=True, check=True, env=child_env(),
    )  # fmt: skip
    assert json.loads(out.stdout) is False  # the browser's cleaner accepts the whole board


@pytest.mark.parametrize("word", ["note", "\u30e1\u30e2" * 20], ids=["ascii", "multibyte"])
def test_a_board_near_the_scene_limit_that_the_cleaning_grows_is_brought_back_inside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, word: str
) -> None:
    """A scene the OLD broker could have saved (at most 1,500 shapes, each within 32,000 bytes, the scene
    within 1,000,000) that the cleaning pushes over the scene limit: it is held to the limit in encoded
    bytes (what the live path and the browser measure; multibyte text passes the column's character count
    and not the bytes), and the upgrade does not roll back. Legacy wrapped texts (no ``originalText``)
    gain one, which no shrinking undoes, so the largest changed shapes are dropped; the unchanged shapes
    are as stored. Fails if the upgrade aborts on the column's CHECK or the migrated board is over the
    limit."""
    groups = 4  # group ids make the shapes a few hundred bytes each: 1,500 of them near 1 MB

    def ids() -> list[str]:
        return [f"g{i:02d}" + "x" * 60 for i in range(groups)]

    def changed(i: int, fill: int) -> dict[str, Any]:
        return el(
            f"c{i:04d}",
            type="text",
            text=f"{word}\nsecond line",
            groupIds=ids(),
            name="n" * 200,
            index="a" * fill,
        )

    def plain(i: int, fill: int) -> dict[str, Any]:
        return el(
            f"u{i:04d}",
            type="text",
            text="ok",
            originalText="ok",
            groupIds=ids(),
            name="n" * 200,
            index="a" * fill,
        )

    while groups and 1_500 * (board_bytes([changed(0, 0)]) + 1) > board.MAX_BYTES - 1_500:
        groups -= 1  # multibyte text is wider: fewer ids
    fill = 0  # the name filler brings the scene to just under the limit as stored
    while 1_500 * (board_bytes([changed(0, fill)]) + 1) < board.MAX_BYTES - 1_500 and fill < 320:
        fill += 1
    scene = [changed(i, fill) for i in range(1_200)] + [plain(i, fill) for i in range(300)]
    assert len(scene) == 1_500 and all(shape_bytes(e) <= board.MAX_SHAPE_BYTES for e in scene)
    cleaned, _ = board.clean_stored(scene)
    assert board_bytes(scene) <= board.MAX_BYTES < board_bytes(cleaned)  # the problem is real
    path = at_55_with(
        tmp_path, monkeypatch, [(1, json.dumps(scene, ensure_ascii=False, separators=(",", ":")))]
    )
    con = db.open_db(path)
    try:
        assert db.schema_version(con) == 56
        raw = con.execute("SELECT elements FROM room_boards WHERE room_id=1").fetchone()[0]
    finally:
        con.close()
    assert len(raw.encode()) <= board.MAX_BYTES and len(raw) <= 1_000_000
    got = {e["id"]: e for e in json.loads(raw)}
    assert all(got[e["id"]] == e for e in scene if e["id"].startswith("u"))
    assert any(i.startswith("c") for i in got)  # changed shapes are kept where they fit
    shrunk = any(e.get("originalText") == "[redacted]" for e in got.values())
    assert shrunk or len(got) < len(scene)  # grown ones shrink first, and are dropped if that is not enough


def test_shapes_dropped_for_size_leave_no_reference_and_no_change_record_naming_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ids a size drop removes are removed from every other shape's references (a binding, a container, a
    group, a frame, a bound list) and from the change records, with the secret-id drops: one final set. Here a
    legacy wrapped text at its shape limit gains an ``originalText`` and cannot be shrunk back, so it is
    dropped. Fails if any reference or any change id still names it."""
    victim = big_shape(
        "victim", type="text", text="one\ntwo"
    )  # no originalText: it gains one, over the limit
    scene = [
        victim,
        el(
            "arrow1",
            type="arrow",
            points=[[0, 0], [5, 5]],
            startBinding={"elementId": "victim", "focus": 0, "gap": 1},
        ),
        el("label", type="text", containerId="victim", text="in", originalText="in"),
        el("grouped", groupIds=["victim", "other"]),
        el("framed", frameId="victim"),
        el("holder", boundElements=[{"id": "victim", "type": "arrow"}, {"id": "arrow1", "type": "arrow"}]),
    ]
    assert shape_bytes(victim) <= board.MAX_SHAPE_BYTES
    path = at_55_with(tmp_path, monkeypatch, [(1, json.dumps(scene, separators=(",", ":")))])
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO room_board_changes(room_id, seq, actor, at, ids, n) VALUES(1, 1, 'a', 1.0, ?, 2)",
        (json.dumps(["victim", "label"]),),
    )
    con.commit()
    con.close()
    con = db.open_db(path)
    try:
        got = load(con)
        assert "victim" not in got and set(got) == {"arrow1", "label", "grouped", "framed", "holder"}
        assert got["arrow1"]["startBinding"] is None and got["label"]["containerId"] is None
        assert got["grouped"]["groupIds"] == ["other"] and got["framed"]["frameId"] is None
        assert got["holder"]["boundElements"] == [{"id": "arrow1", "type": "arrow"}]
        assert "victim" not in json.dumps(got)
        assert [json.loads(r[0]) for r in con.execute("SELECT ids FROM room_board_changes")] == [["label"]]
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
