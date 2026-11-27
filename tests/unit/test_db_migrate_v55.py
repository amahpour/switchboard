"""Schema 55 (#890, DESIGN.md §11 item 10, §37.2): a review board's stored text is cleaned once.

The step from 54 has no schema change. It runs every string a board stored (its URLs, a card's text, the
strings in the data of the board's events) through the cleaning its input now gets, under ``secure_delete``,
and checkpoints the WAL, so the old bytes are not left on disk in the database, its free pages or its log.
Room chat is not rewritten (#819 chose that), and the backup the migration writes first holds the old text:
it is the one file this test leaves out. Every secret is built from pieces, so no scanner reads this file as
a leak."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from switchboard import db, reviews

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"
TOKEN = "ghp" + "_" + "aB3" * 12
PASSWORD = "hunter2" * 2
USERINFO = "al" + "ice:p@" + "ss"  # a password with an @ in it
CANARIES = (
    TOKEN,
    TOKEN.split("_", 1)[1],
    TOKEN.split("_", 1)[1].lower(),
    PASSWORD,
    "p@" + "ss",
    "al" + "ice:",
)
KEY_BODY = ("QUJD" * 16 + "\n") * 50  # a private key's lines: long enough that its card has an overflow page
KEY = "-----BEGIN PRIVATE " + "KEY-----\n" + KEY_BODY + "-----END PRIVATE " + "KEY-----"
PR = "https://example.com/shop/pull/7"
LONG = "https://example.com/p?" + "&".join(["token=abcdefgh1"] * 29)  # 500 or less; a marker lengthens it
assert len(LONG) <= 500 < len(LONG.replace("abcdefgh1", "[redacted]"))
LONG_PROOF = LONG.replace("example.com/p", "ci.example.com/run")


def at_54(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The 0.37.2 dump migrated to 54, with boards as an earlier release stored them: secrets in the
    places an agent or a person could write them, and chat that holds the same."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    try:
        old.executescript(FIXTURE.read_text())
    finally:
        old.close()
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", 54)
        con = db.open_db(path)
        with db.tx(con):
            boards = [  # (id, room, url): ids past the dump's own boards
                (101, 1, f"https://{USERINFO}@example.com/shop/pull/7"),  # cleans to the same as board 2
                (102, 1, PR),  # already clean: keeps its URL
                (103, 1, f"{PR.replace('7', '8')}?access_token={PASSWORD}"),  # cleans like board 104
                (104, 1, f"{PR.replace('7', '8')}?access_token={PASSWORD}x"),  # a board of its own
                (
                    105,
                    1,
                    f"{PR.replace('7', '8')}?access_token={PASSWORD}",
                ),  # a reset of board 103's URL: its newest row, so this board is the newest
                (106, 2, f"https://{USERINFO}@example.com/shop/pull/7"),  # another room: no collision
                (107, 1, LONG),  # over 500 once cut
                (108, 1, f"{PR}#board-101"),  # clean, and has the ending board 101 is about to be given
                (109, 1, "https://[" + "redacted-host" + "]x/shop/pull/9"),  # no host a parser takes
                (110, 1, "https://[abc/shop/pull/10"),  # malformed, accepted once, left alone by a cleaner
            ]
            for rid, room, url in boards:
                con.execute(
                    "INSERT INTO reviews(id, room_id, url, opened_by, opened_at)"
                    " VALUES(?, ?, ?, 'codex-1', ?)",
                    (rid, room, url[:500], 1790000000.0 + rid),
                )
            con.execute(
                "INSERT INTO review_items(id, review_id, kind, n, state, title, detail, file, raised_by,"
                " reason, options, answer, proof, deferred_to, insisted, created_at, updated_at)"
                " VALUES(101, 102, 'finding', 1, 'fixed', ?, ?, ?, 'codex-1', ?, ?, ?, ?, ?, ?, 1.0, 1.0)",
                (
                    "x" * 170 + f" password={PASSWORD}",  # longer once cut: cut to the column
                    f"the log prints {TOKEN} and https://{USERINFO}@example.com/x\n{KEY}",
                    f"shop/{TOKEN}.py",
                    f"no: {TOKEN}",
                    json.dumps([f"use {TOKEN}", "rotate it"]),
                    f"token={PASSWORD}",
                    f"https://ci.example.com/run?access_token={PASSWORD}",
                    f"SHOP-1 {TOKEN}",
                    f"it prints {TOKEN}",
                ),
            )
            con.execute(
                "INSERT INTO review_items(id, review_id, kind, n, state, title, file, lines, raised_by,"
                " proof,"
                " created_at, updated_at) VALUES(103, 102, 'finding', 3, 'fixed', 'spans', '\"token\"',"
                " '12345678', 'codex-1', ?, 1.0, 1.0)",
                (LONG_PROOF,),
            )
            con.execute(  # what an earlier edit or delete left in a free page: a secret on a deleted card
                "INSERT INTO review_items(id, review_id, kind, n, state, title, detail, raised_by,"
                " created_at, updated_at)"
                " VALUES(150, 102, 'finding', 9, 'raised', 'gone', ?, 'codex-1', 1.0, 1.0)",
                (KEY,),
            )
            con.execute("DELETE FROM review_items WHERE id=150")
            con.execute(
                "INSERT INTO review_items(id, review_id, kind, n, state, title, raised_by, proof,"
                " created_at, updated_at) VALUES(102, 102, 'finding', 2, 'fixed', 'plain', 'codex-1',"
                " 'test_total', 1.0, 1.0)"
            )
            for kind, data in (
                (
                    "review",
                    {
                        "board": 102,
                        "item": "F1",
                        "proof": f"t {TOKEN}",
                        "deep": {"x": [f"password={PASSWORD}"]},
                    },
                ),
                ("review_line", {"board": 102, "items": ["F1"], "to": ["claude-1"]}),
                ("say", {"note": f"not a board event {TOKEN}"}),
            ):
                con.execute(
                    "INSERT INTO events(ts, room_id, kind, data) VALUES(1790000100.0, 1, ?, ?)",
                    (kind, json.dumps(data)),
                )
            con.execute(
                "INSERT INTO messages(room_id, ts, sender_name, sender_kind, via, kind, text, mentions)"
                " VALUES(1, 1790000200.0, 'codex-1', 'agent', 'mcp', 'chat', ?, '[]')",
                (f"chat keeps {TOKEN}",),
            )
        con.close()
    return path


def urls(con: sqlite3.Connection) -> dict[int, str]:
    return {r[0]: r[1] for r in con.execute("SELECT id, url FROM reviews")}


def test_the_upgrade_cleans_what_a_board_stored_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Board URLs, card text and event strings come out clean, and are still valid for what reads them:
    JSON options stay a list, a cleaned value is cut to its column, a link stays a link. Chat, other events
    and every row count are as they were. Fails if any of the above is still there, or any column is kept as
    written."""
    path = at_54(tmp_path, monkeypatch)
    old = db.connect(path)
    counts = db.row_counts(old, db.tables_of(54))
    old.close()
    con = db.open_db(path)
    try:
        assert db.schema_version(con) == db.SCHEMA_VERSION == 65
        assert db.row_counts(con, db.tables_of(54)) == counts
        assert db.integrity_ok(con) is None
        dump = json.dumps(
            [
                list(map(tuple, con.execute(f"SELECT * FROM {t}").fetchall()))
                for t in ("reviews", "review_items")
            ]
        )
        events = json.dumps(
            [tuple(r) for r in con.execute("SELECT data FROM events WHERE kind LIKE 'review%'")]
        )
        for canary in CANARIES:
            assert canary not in dump and canary not in events, canary
        item = con.execute("SELECT * FROM review_items WHERE id=101").fetchone()
        assert len(item["title"]) == 200 and item["title"].startswith("x" * 170)
        assert json.loads(item["options"]) == ["use [redacted: github token]", "rotate it"]
        assert item["file"] == "shop/[redacted: github token].py"
        assert item["proof"] == "https://ci.example.com/run?access_token=[redacted]"
        assert item["detail"] == (
            "the log prints [redacted: github token] and https://[redacted: url credentials]@example.com/x\n"
            "[redacted: private key]"
        )
        assert con.execute("SELECT title FROM review_items WHERE id=102").fetchone()[0] == "plain"
        assert con.execute("SELECT proof FROM review_items WHERE id=102").fetchone()[0] == "test_total"
        review = json.loads(con.execute("SELECT data FROM events WHERE kind='review'").fetchone()[0])
        assert review == {
            "board": 102,
            "item": "F1",
            "proof": "t [redacted: github token]",
            "deep": {"x": ["password=[redacted: password or token]"]},
        }
        assert json.loads(con.execute("SELECT data FROM events WHERE kind='review_line'").fetchone()[0]) == {
            "board": 102,
            "items": ["F1"],
            "to": ["claude-1"],
        }
        assert TOKEN in con.execute("SELECT data FROM events WHERE kind='say'").fetchone()[0]
        assert TOKEN in con.execute("SELECT text FROM messages WHERE text LIKE 'chat keeps%'").fetchone()[0]
        spans = con.execute("SELECT file, lines, proof FROM review_items WHERE id=103").fetchone()
        assert (spans["file"], spans["lines"]) == ('"token":[redacted: password or token]', "")
        assert spans["proof"] == "https://invalid.example/proof-103"  # grew past its column: a placeholder
        assert not con.execute("SELECT 1 FROM meta WHERE key=?", (db.CLEAN_PENDING,)).fetchone()
        assert db.clean_review_rows(con) == 0  # run again: nothing left to change
    finally:
        con.close()


def test_boards_whose_urls_clean_to_the_same_string_stay_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule: a URL already clean keeps it; of the others the board whose newest row is last keeps the
    cleaned one and
    each older one gets ``#board-<id of its newest row>``, so every key in the room is unique and each still
    names its pull request. A reset's rows (same URL) move together, another room is not compared, and a
    cleaned URL too long for its column is cut before the ending. Fails if two boards share a URL, a clean
    one is renamed, or a row of one board is left behind."""
    con = db.open_db(at_54(tmp_path, monkeypatch))
    try:
        got = urls(con)
        assert got[102] == PR  # clean already: kept
        assert got[106] == "https://example.com/shop/pull/7"  # room 2: nobody else has it
        eight = "https://example.com/shop/pull/8?access_token=[redacted]"
        assert got[103] == got[105] == eight and got[104] == eight + "#board-104"  # ids 103, 105: one board
        assert len(got[107]) <= 500 and got[107].endswith("#board-107") and "abcdefgh1" not in got[107]
        assert got[108] == PR + "#board-101"  # clean: kept, so 101's ending is taken and it gets the next
        assert got[101] == "https://example.com/shop/pull/7#board-101-1"
        for rid in (109, 110):  # no link ``reviews.url`` takes: a fixed placeholder
            assert got[rid] == f"https://invalid.example/board-{rid}"
        assert all(reviews.url(u) == u for u in got.values())  # every key is a link a lookup takes
        rows = con.execute("SELECT room_id, url FROM reviews WHERE id > 100").fetchall()
        assert len({(r["room_id"], r["url"]) for r in rows}) == 9
    finally:
        con.close()


def test_no_file_of_the_home_keeps_the_old_text_but_the_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rewrite runs under ``secure_delete`` and the WAL is checkpointed and emptied, so the old bytes
    are in no page of the database, free or not, and not in the log: every file of the home is searched,
    but the backup the migration made first (``<db>.v54.bak``), which is the way back and is named in the
    docs. Chat still holds its token, so the search is for the board's own secrets. Fails if any is found."""
    path = at_54(tmp_path, monkeypatch)
    # a secret only a card and an event hold, to look for beside the chat's token
    con = db.open_db(path)  # kept open: what is on disk now is what a copy of the home would take
    backup = db.backup_path_for(path, 54)
    assert backup.exists() and PASSWORD.encode() in backup.read_bytes()  # the way back keeps the old text
    leaks = {}
    for f in tmp_path.rglob("*"):
        if f.is_file() and f != backup:
            data = f.read_bytes()
            for canary in (PASSWORD, "p@" + "ss", USERINFO, "QUJD" * 16):
                if canary.encode() in data:
                    leaks[f.name] = canary
    assert leaks == {}, leaks
    wal = path.with_name(path.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    con.close()


def test_a_fresh_database_has_nothing_to_clean(tmp_path: Path) -> None:
    """A new database is made at the current version with no step: the cleaning is only the upgrade's.
    Fails if it is not."""
    con = db.open_db(tmp_path / "fresh.db")
    try:
        assert db.schema_version(con) == db.SCHEMA_VERSION
        assert db.clean_review_rows(con) == 0
    finally:
        con.close()


def test_a_busy_checkpoint_leaves_the_cleaning_owed_and_the_next_start_finishes_it(tmp_path: Path) -> None:
    """A reader holding an old snapshot makes ``wal_checkpoint(TRUNCATE)`` report busy; that is read, and the
    owed flag stays, and the next call (every start runs it) finishes: the WAL is empty and the flag gone.
    Fails if a busy checkpoint is taken for done."""
    path = tmp_path / "x.db"
    con = db.open_db(path)
    reader = db.connect(path)
    con.execute("PRAGMA busy_timeout=0")
    try:
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, '1')", (db.CLEAN_PENDING,))
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM meta").fetchone()
        con.execute("INSERT INTO meta(key, value) VALUES('x', '1')")  # the log moves past the reader
        assert db.finish_review_clean(con) is False
        assert con.execute("SELECT 1 FROM meta WHERE key=?", (db.CLEAN_PENDING,)).fetchone()
        reader.execute("ROLLBACK")
        assert db.finish_review_clean(con) is True
        assert not con.execute("SELECT 1 FROM meta WHERE key=?", (db.CLEAN_PENDING,)).fetchone()
        assert db.finish_review_clean(con) is True  # nothing owed: a no-op
    finally:
        reader.close()
        con.close()


def test_a_busy_cleanup_rebuilds_the_database_once_in_a_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upgrade owes its ``VACUUM`` and checkpoint, and ``migrate`` pays them once whatever the upgrade
    did: a busy first attempt leaves the flag for the next start and does not rebuild the file again in
    this one. Fails if a start runs ``VACUUM`` twice."""
    path = at_54(tmp_path, monkeypatch)
    con = db.connect(path)
    reader = db.connect(path)
    statements: list[str] = []
    try:
        con.execute("PRAGMA busy_timeout=0")
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM meta").fetchone()  # an old snapshot: the checkpoint will be busy
        con.set_trace_callback(statements.append)
        db.migrate(con, backup_to=db.backup_path_for(path, 54))
        assert [s for s in statements if s.startswith("VACUUM")] == ["VACUUM"]
        assert con.execute("SELECT 1 FROM meta WHERE key=?", (db.CLEAN_PENDING,)).fetchone()
        reader.execute("ROLLBACK")
        db.migrate(con)  # the next start finishes it
        assert [s for s in statements if s.startswith("VACUUM")] == ["VACUUM", "VACUUM"]
        assert not con.execute("SELECT 1 FROM meta WHERE key=?", (db.CLEAN_PENDING,)).fetchone()
    finally:
        reader.close()
        con.close()
