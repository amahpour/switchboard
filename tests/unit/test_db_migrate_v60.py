"""Schema 60 moves a person's webhook out of ``broker_settings`` into its own tables (#923, #926, DESIGN.md
§62.4): ``webhooks``, one row a person, and ``webhook_sent``, a row a sent key a lane. A settings value holds
200 characters, which a long address (#926) and the sent keys (#923) overflowed. The step is tested on a
database 1.13.0 made (``tests/fixtures/db/v1_13_0.sql``, ``make_v1_13_0.py``)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from switchboard import db, notify
from switchboard.broker import webhook
from switchboard.store import Store

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/db/v1_13_0.sql"
T0 = 1_790_000_000.0
OWNER_URL = "https://hooks.example.com/services/T000/B000/ownersecret?token=t0"
BOB_URL = "https://hooks.example.com/services/T000/B000/bobsecret?token=t1"
LOUD_URL = "https://hooks.example.com/services/T000/B000/loud?token=t2"
NEW = ("webhooks", "webhook_sent")


def at_59(path: Path, extra: dict[str, object] | None = None) -> None:
    """The 1.13.0 database at ``path``, with ``extra`` settings rows added as #923's pre-release builds kept
    them (values as JSON)."""
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())
        for k, v in (extra or {}).items():
            old.execute("INSERT INTO broker_settings(key, value) VALUES(?, ?)", (k, json.dumps(v)))
    with sqlite3.connect(path) as check:
        assert db.schema_version(check) == 59


def rows(con: sqlite3.Connection, q: str) -> list[tuple[object, ...]]:
    return [tuple(r) for r in con.execute(q)]


def test_a_1_13_0_database_moves_its_webhooks_and_drops_the_one_that_does_not_parse(tmp_path: Path) -> None:
    """The owner's address, its ok status and its sent key, and bob's address and failed status, are rows of
    the new tables after the step, and load as they did; person 7's webhook row does not parse, so it and its
    status and sent rows are gone, as ``load`` already read it (no webhook). No webhook row is left in
    ``broker_settings``, the setting that is not a webhook's is kept, the tables are a fresh database's, and
    the backup is schema 59 with every old row. Fails if the step skips the move, keeps the old rows, moves
    the broken webhook, or the migration's row-count check refuses a move it was told of."""
    path = tmp_path / "old.db"
    at_59(path)
    con = db.open_db(path)
    backup = db.connect(db.backup_path_for(path, 59), readonly=True)
    fresh = db.open_db(tmp_path / "fresh.db")
    try:
        assert db.schema_version(con) == db.SCHEMA_VERSION == 60
        assert db.schema_version(backup) == 59
        q = (
            "SELECT type, name, sql FROM sqlite_master"
            " WHERE tbl_name IN ('webhooks', 'webhook_sent') ORDER BY name"
        )
        assert rows(con, q) == rows(fresh, q) and len(rows(con, q)) == 2
        assert rows(
            con, "SELECT person, url, wait_s, second_url, quiet_from FROM webhooks ORDER BY person"
        ) == [
            (0, OWNER_URL, 300.0, None, None),
            (1, BOB_URL, 600.0, None, None),
        ]
        assert rows(con, "SELECT person, first_at, first_ok, first_error FROM webhooks ORDER BY person") == [
            (0, T0, 1, None),
            (1, T0 + 5, 0, notify.fail_reason("status")),
        ]
        assert rows(con, "SELECT person, lane, key FROM webhook_sent") == [(0, "first", "q:build:12")]
        assert rows(con, "SELECT key, value FROM broker_settings") == [("notify_text:0", "0")]
        assert len(rows(backup, "SELECT key FROM broker_settings WHERE key LIKE 'webhook%'")) == 8
        state = SimpleNamespace(store=Store(con))
        w = webhook.load(state, 0)
        assert w is not None and (w["url"], w["wait_s"], w["second"], w["quiet"]) == (
            OWNER_URL,
            300.0,
            None,
            None,
        )
        assert webhook.last_status(state, 1) == {
            "at": T0 + 5,
            "ok": False,
            "error": notify.fail_reason("status"),
        }
        assert webhook.load(state, 7) is None
        assert state.store.webhook_sent_keys(0, "first") == ["q:build:12"]
        assert db.integrity_ok(con) is None
    finally:
        fresh.close()
        backup.close()
        con.close()


def test_the_pre_release_rows_of_923_move_too_and_a_quiet_zone_that_fails_drops_the_webhook(
    tmp_path: Path,
) -> None:
    """#923's builds before 60 kept a second address, quiet hours, a second status and second sent keys in
    rows of their own: each becomes its column or row. bob's quiet hours name a zone no server knows, so his
    webhook is dropped whole, as ``load`` read it. Fails if any of those rows is lost or left behind, or a
    webhook with a part that fails its checks is moved."""
    path = tmp_path / "old.db"
    at_59(
        path,
        {
            "webhook_second:0": {"url": LOUD_URL, "after_s": 900.0},
            "webhook_quiet:0": {"start": 1320, "end": 420, "tz": "UTC", "always": ["question", "approval"]},
            "webhook_status2:0": {"at": T0 + 9, "ok": False, "error": notify.fail_reason("timeout")},
            "webhook_sent2:0": ["q:build:12"],
            "webhook_quiet:1": {"start": 1320, "end": 420, "tz": "Mars/Olympus_Mons", "always": []},
        },
    )
    con = db.open_db(path)
    try:
        assert rows(
            con,
            "SELECT second_url, second_after_s, second_at, second_ok, second_error,"
            " quiet_from, quiet_to, quiet_tz, quiet_always FROM webhooks WHERE person=0",
        ) == [
            (
                LOUD_URL,
                900.0,
                T0 + 9,
                0,
                notify.fail_reason("timeout"),
                "22:00",
                "07:00",
                "UTC",
                "approval,question",
            )
        ]
        assert rows(con, "SELECT person FROM webhooks") == [(0,)]
        assert rows(con, "SELECT lane, key FROM webhook_sent ORDER BY lane") == [
            ("first", "q:build:12"),
            ("second", "q:build:12"),
        ]
        assert rows(con, "SELECT key FROM broker_settings") == [("notify_text:0",)]
        w = webhook.load(SimpleNamespace(store=Store(con)), 0)
        assert w is not None and w["second"] == {"url": LOUD_URL, "after_s": 900.0}
        assert w["quiet"] == notify.Quiet(1320, 420, "UTC", frozenset({"question", "approval"}))
    finally:
        con.close()


GOOD = {"person": 0, "url": OWNER_URL, "wait_s": 300.0}


@pytest.mark.parametrize(
    "bad",
    [
        {"url": "https://hooks.example.com/" + "x" * (notify.URL_MAX - 25)},  # 501 characters
        {"url": "http://hooks.example.com/x"},
        {"person": -1},
        {"wait_s": notify.WAIT_MIN_S - 1},
        {"wait_s": notify.WAIT_MAX_S + 1},
        {"second_url": LOUD_URL},  # no delay
        {"second_url": LOUD_URL, "second_after_s": notify.SECOND_MIN_S - 1},
        {"second_url": LOUD_URL, "second_after_s": notify.SECOND_MAX_S + 1},
        {"quiet_from": "24:00", "quiet_to": "07:00", "quiet_tz": "UTC", "quiet_always": ""},
        {"quiet_from": "7:00", "quiet_to": "08:00", "quiet_tz": "UTC", "quiet_always": ""},
        {"quiet_from": "22:00", "quiet_to": "22:00", "quiet_tz": "UTC", "quiet_always": ""},
        {
            "quiet_from": "22:00",
            "quiet_to": "07:00",
            "quiet_tz": "x" * (notify.TZ_MAX + 1),
            "quiet_always": "",
        },
        {"quiet_from": "22:00", "quiet_to": "07:00", "quiet_tz": "UTC", "quiet_always": "everything"},
        {"quiet_from": "22:00", "quiet_to": "07:00", "quiet_tz": "UTC", "quiet_always": "question,approval"},
        {"quiet_from": "22:00", "quiet_to": "07:00"},  # half of the quiet hours
        {"first_ok": 1},  # a status with no time
        {"first_at": T0, "first_ok": 1, "first_error": "an ok post has no error"},
    ],
)
def test_the_table_refuses_what_a_save_would(tmp_path: Path, bad: dict[str, object]) -> None:
    """Each column's CHECK is ``notify.py``'s limit, so a row that got past the code still could not hold
    what a save refuses. Fails if a CHECK is looser than the code (or missing)."""
    con = db.open_db(tmp_path / "x.db")
    try:
        con.execute(
            "INSERT INTO webhooks(person, url, wait_s) VALUES(1, ?, 300)", (OWNER_URL,)
        )  # it can hold one
        cols = {**GOOD, **bad}
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                f"INSERT INTO webhooks({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})",
                tuple(cols.values()),
            )
    finally:
        con.close()


def test_the_kinds_the_table_takes_are_every_set_of_notifys_kinds() -> None:
    """``quiet_always`` holds a sorted, comma-joined set of ``notify.KINDS``: the CHECK's list is every one.
    Fails if a kind is added to notify.py and not to the schema."""
    from itertools import combinations

    every = {",".join(sorted(c)) for n in range(len(notify.KINDS) + 1) for c in combinations(notify.KINDS, n)}
    assert set(db.QUIET_ALWAYS_VALUES) == every


def test_a_sent_key_goes_with_its_webhook_and_needs_one(tmp_path: Path) -> None:
    """A sent key is a row of a person who has a webhook: removing the webhook removes its keys, and a key
    for nobody's webhook is not kept. Fails if keys outlive the webhook they were sent on."""
    con = db.open_db(tmp_path / "x.db")
    store = Store(con)
    try:
        assert store.webhook_set(0, {"url": OWNER_URL}, second_default_s=1200.0) == "ok"
        store.webhook_keep_sent(0, "first", ["q:build:1", "b:build:" + "u" * 900], T0)
        store.webhook_keep_sent(3, "first", ["q:build:1"], T0)
        assert store.webhook_sent_keys(0, "first") == ["b:build:" + "u" * 900, "q:build:1"]
        assert store.webhook_sent_keys(3, "first") == []
        store.webhook_keep_sent(0, "first", ["q:build:1"], T0 + 1)  # pruned to what still needs them
        assert store.webhook_sent_keys(0, "first") == ["q:build:1"]
        store.webhook_remove(0)
        assert rows(con, "SELECT count(*) FROM webhook_sent") == [(0,)]
    finally:
        con.close()


def test_a_schema_60_database_opens_unchanged(tmp_path: Path) -> None:
    """A database at the current version is opened as it is: no step and no backup. Fails if opening a 60
    database migrates or backs it up again."""
    path = tmp_path / "now.db"
    con = db.open_db(path)
    Store(con).webhook_set(0, {"url": OWNER_URL}, second_default_s=1200.0)
    schema = sorted(tuple(r) for r in con.execute("SELECT type, name, sql FROM sqlite_master"))
    con.close()
    again = db.open_db(path)
    try:
        assert db.schema_version(again) == db.SCHEMA_VERSION
        assert sorted(tuple(r) for r in again.execute("SELECT type, name, sql FROM sqlite_master")) == schema
        assert rows(again, "SELECT person FROM webhooks") == [(0,)]
        assert not list(tmp_path.glob("now.db.v*.bak*"))
    finally:
        again.close()
