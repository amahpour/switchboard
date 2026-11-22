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
    """The 1.13.0 database at ``path``, with ``extra`` settings rows added (or put in place of the fixture's)
    as #923's pre-release builds kept them (values as JSON)."""
    with sqlite3.connect(path) as old:
        old.executescript(FIXTURE.read_text())
        for k, v in (extra or {}).items():
            old.execute("INSERT OR REPLACE INTO broker_settings(key, value) VALUES(?, ?)", (k, json.dumps(v)))
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
        assert db.schema_version(con) == db.SCHEMA_VERSION == 61
        assert db.schema_version(backup) == 59
        q = (
            "SELECT type, name, sql FROM sqlite_master"
            " WHERE tbl_name IN ('webhooks', 'webhook_sent') ORDER BY name"
        )
        assert rows(con, q) == rows(fresh, q) and len(rows(con, q)) == 3  # and the UNIQUE(person) index
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
        assert rows(
            con, "SELECT w.person, s.lane, s.key FROM webhook_sent s JOIN webhooks w ON w.id = s.webhook"
        ) == [(0, "first", "q:build:12")]
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
        owner = state.store.webhook_get(0)
        assert owner is not None and (owner["first_gen"], owner["second_gen"]) == (0, 0)
        assert state.store.webhook_sent_keys(owner["id"], "first") == ["q:build:12"]
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


def moved_people(con: sqlite3.Connection) -> list[tuple[object, ...]]:
    return rows(con, "SELECT person, url FROM webhooks ORDER BY person")


CLEAN = [(0, OWNER_URL), (1, BOB_URL)]


@pytest.mark.parametrize(
    "at",
    [float("nan"), float("inf"), float("-inf"), -5.0, 0.0, 1e300, 32_503_680_000.0],
    ids=["nan", "inf", "-inf", "negative", "zero", "huge", "year-3000"],
)
def test_a_status_time_no_broker_wrote_is_dropped_and_the_upgrade_commits(tmp_path: Path, at: float) -> None:
    """bob's last status has a time that is not a finite number after 1970 and before the year 3000. His
    webhook still moves, with no status, beside the owner's, and the upgrade commits. Fails if the time is
    kept: NaN is stored as NULL, which the row's CHECK refuses, so the whole upgrade stopped; an infinite,
    negative or huge time was moved as if a post had been made then (#923, F11)."""
    path = tmp_path / "old.db"
    at_59(path, {"webhook_status:1": {"at": at, "ok": False, "error": notify.fail_reason("status")}})
    con = db.open_db(path)
    try:
        assert db.schema_version(con) == db.SCHEMA_VERSION
        assert moved_people(con) == CLEAN
        assert rows(con, "SELECT first_at, first_ok, first_error FROM webhooks WHERE person=1") == [
            (None, None, None)
        ]
        assert rows(con, "SELECT first_at, first_ok FROM webhooks WHERE person=0") == [(T0, 1)]
    finally:
        con.close()


@pytest.mark.parametrize(
    "tail",
    [
        "00012",
        "012",
        "00",
        "+12",
        "-1",
        " 12",
        "\u0661\u0662",
        "\u00b2",
        "\uff11",
        "9223372036854775808",
        "9999999999999999999999",
        "",
    ],
    ids=[
        "zeros",
        "zero",
        "double-zero",
        "plus",
        "minus",
        "space",
        "arabic-indic",
        "superscript",
        "fullwidth",
        "int64-max-plus-1",
        "huge",
        "empty",
    ],
)
def test_a_person_key_no_broker_wrote_is_skipped_and_the_upgrade_commits(tmp_path: Path, tail: str) -> None:
    """A webhook row (with its status and sent keys) whose person key is not the canonical form a broker
    wrote (digits with no leading zero, ASCII only, within SQLite's 64 bits, or ``0``) is skipped like one
    that does not parse, and its rows deleted; the owner's and bob's move as they are and the upgrade
    commits. Fails if the key is read with ``isdigit`` and ``int``: ``²`` is a digit to ``isdigit`` and not
    to ``int`` (a ValueError stopped the upgrade), a key past 64 bits stopped it at the insert, and other
    scripts' digits or a leading zero named someone else (#923, F11)."""
    path = tmp_path / "old.db"
    hook = {"url": "https://hooks.example.com/services/T000/B000/other?token=t9", "wait_s": 300.0}
    at_59(
        path,
        {
            f"webhook:{tail}": hook,
            f"webhook_status:{tail}": {"at": T0, "ok": True, "error": None},
            f"webhook_sent:{tail}": ["q:build:77"],
        },
    )
    con = db.open_db(path)
    try:
        assert db.schema_version(con) == db.SCHEMA_VERSION
        assert moved_people(con) == CLEAN
        assert rows(con, "SELECT key FROM webhook_sent") == [("q:build:12",)]
        assert rows(con, "SELECT key FROM broker_settings") == [("notify_text:0",)]
    finally:
        con.close()


LONE = "\ud800"  # an unpaired surrogate: JSON holds it, a str holds it, UTF-8 (and so a TEXT bind) does not


@pytest.mark.parametrize(
    "extra, status_1",
    [
        (
            {"webhook:2": {"url": f"https://hooks.example.com/services/{LONE}", "wait_s": 300.0}},
            (T0 + 5, 0, notify.fail_reason("status")),
        ),
        (
            {"webhook_status:1": {"at": T0 + 5, "ok": False, "error": f"refused {LONE}"}},
            (None, None, None),
        ),
        (
            {"webhook_sent:0": ["q:build:12", f"q:build:{LONE}"]},
            (T0 + 5, 0, notify.fail_reason("status")),
        ),
    ],
    ids=["address", "error-text", "sent-key"],
)
def test_text_that_cannot_be_stored_is_dropped_and_the_upgrade_commits(
    tmp_path: Path, extra: dict[str, object], status_1: tuple[object, ...]
) -> None:
    """Legacy JSON can hold an unpaired surrogate. A webhook whose address holds one is skipped (it is no
    address, ``notify.url_problem``), a status whose error text holds one is dropped (bob's webhook moves with
    no status), and a sent key that holds one is skipped (the owner's other key moves); the clean webhooks
    move and the upgrade commits. Fails without ``notify.storable``: the TEXT bind raised UnicodeEncodeError
    and the whole upgrade stopped (#923, F11)."""
    path = tmp_path / "old.db"
    at_59(path, extra)
    con = db.open_db(path)
    try:
        assert db.schema_version(con) == db.SCHEMA_VERSION
        assert moved_people(con) == CLEAN
        assert rows(con, "SELECT first_at, first_ok, first_error FROM webhooks WHERE person=1") == [status_1]
        assert rows(con, "SELECT key FROM webhook_sent") == [("q:build:12",)]
        assert rows(con, "SELECT key FROM broker_settings") == [("notify_text:0",)]
    finally:
        con.close()


@pytest.mark.parametrize(
    "key", ["\x00", "\x00q:build:13", "q:build:\x0013"], ids=["nul", "leading", "inside"]
)
def test_a_sent_key_with_a_nul_is_skipped_and_the_upgrade_commits(tmp_path: Path, key: str) -> None:
    """A legacy sent key holding a NUL has a positive Python length and encodes as UTF-8, but SQLite's
    ``length()`` stops at the NUL, so ``webhook_sent``'s CHECK (``length(key) BETWEEN 1 AND 1000``) refuses a
    key that starts with one, and a NUL anywhere is no key a broker wrote. It is skipped beside clean keys
    (the owner's other key and bob's moves), the clean webhooks move, and the upgrade commits. Fails without
    the NUL rule in ``notify.storable``: the INSERT failed its CHECK and the upgrade stopped, or a NUL key
    was kept (#923, F11)."""
    path = tmp_path / "old.db"
    at_59(path, {"webhook_sent:0": ["q:build:12", key], "webhook_sent:1": ["b:plan:3"]})
    con = db.open_db(path)
    try:
        assert db.schema_version(con) == db.SCHEMA_VERSION
        assert moved_people(con) == CLEAN
        assert sorted(rows(con, "SELECT key FROM webhook_sent")) == [("b:plan:3",), ("q:build:12",)]
        assert rows(con, "SELECT key FROM broker_settings") == [("notify_text:0",)]
    finally:
        con.close()


def test_the_largest_canonical_person_key_moves(tmp_path: Path) -> None:
    """The largest key SQLite holds is a key like any other: its webhook moves. Fails if the range check is
    off by one."""
    path = tmp_path / "old.db"
    big = 2**63 - 1
    at_59(path, {f"webhook:{big}": {"url": LOUD_URL, "wait_s": 300.0}})
    con = db.open_db(path)
    try:
        assert moved_people(con) == [*CLEAN, (big, LOUD_URL)]
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
    """A sent key is a row of a webhook, by its id: removing the webhook removes its keys, a key for no
    webhook, or for a lane whose address changed since (an older generation), is not kept, and a webhook
    made again has a new id and none of the old one's keys. Fails if keys outlive the webhook they were sent
    on, or land on its successor (#923, F10)."""
    con = db.open_db(tmp_path / "x.db")
    store = Store(con)
    try:
        assert store.webhook_set(0, {"url": OWNER_URL}, second_default_s=1200.0) == "ok"
        w = store.webhook_get(0)
        assert w is not None and w["first_gen"] == 0
        wid = w["id"]
        store.webhook_keep_sent(wid, "first", 0, ["q:build:1", "b:build:" + "u" * 900], T0)
        store.webhook_keep_sent(wid + 1, "first", 0, ["q:build:1"], T0)
        assert store.webhook_set(0, {"url": OWNER_URL + "&v=2"}, second_default_s=1200.0) == "ok"
        assert store.webhook_get(0)["first_gen"] == 1  # type: ignore[index]
        store.webhook_keep_sent(wid, "first", 0, ["q:build:2"], T0)  # read before the address was replaced
        assert store.webhook_sent_keys(wid, "first") == ["b:build:" + "u" * 900, "q:build:1"]
        assert store.webhook_sent_keys(wid + 1, "first") == []
        store.webhook_keep_sent(wid, "first", 1, ["q:build:1"], T0 + 1)  # pruned to what still needs them
        assert store.webhook_sent_keys(wid, "first") == ["q:build:1"]
        store.webhook_remove(0)
        assert rows(con, "SELECT count(*) FROM webhook_sent") == [(0,)]
        assert store.webhook_set(0, {"url": OWNER_URL}, second_default_s=1200.0) == "ok"
        again = store.webhook_get(0)
        assert again is not None and again["id"] != wid
        store.webhook_keep_sent(wid, "first", 1, ["q:build:1"], T0 + 2)  # the old webhook's tick, late
        store.webhook_keep_sent(again["id"], "first", 1, ["q:build:1"], T0 + 2)  # its own gen is 0
        assert store.webhook_sent_keys(again["id"], "first") == []
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
