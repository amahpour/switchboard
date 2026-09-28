"""The first schema migration, v1 -> v2 (DESIGN.md §27.6), on a database made by 0.2.0.

The fixture ``tests/fixtures/db/v0_2_0.sql`` is a dump of a database written by
switchboard 0.2.0's own store and engine (see its README). The migration must
back the file up first (0600, never overwriting a backup), check the copy, run
every statement in one transaction, check the result, and leave a version-1
database untouched when anything fails.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from switchboard import db
from switchboard.store import Store

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v0_2_0.sql"
V1_TABLES = ("meta", "rooms", "participants", "memberships", "messages", "batches", "deliveries", "events",
             "web_sessions")


def make_v1(path: Path) -> list[str]:
    """A version-1 database file from the 0.2.0 fixture; returns its dump."""
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text(encoding="utf-8"))
    con.close()
    os.chmod(path, 0o600)
    return dump(path)


def dump(path: Path) -> list[str]:
    con = sqlite3.connect(path)
    try:
        return list(con.iterdump())
    finally:
        con.close()


def version(path: Path) -> int | None:
    con = sqlite3.connect(path)
    try:
        return db.schema_version(con)
    finally:
        con.close()


def columns(con: sqlite3.Connection, table: str) -> list[tuple]:
    return [tuple(r) for r in con.execute(f"PRAGMA table_info({table})").fetchall()]


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + ".v1.bak*"))


def test_fixture_is_a_v1_database_with_rows_in_every_table(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    make_v1(p)
    con = sqlite3.connect(p)
    assert db.schema_version(con) == 1
    assert all(db.row_counts(con, V1_TABLES).values())
    assert "host" not in {c[1] for c in columns(con, "participants")}
    con.close()


def test_fresh_db_is_v2(tmp_path: Path) -> None:
    con = db.open_db(tmp_path / "switchboard.db")
    assert db.schema_version(con) == db.SCHEMA_VERSION == 2
    host = {c[1]: c for c in columns(con, "participants")}["host"]
    assert host[2] == "TEXT" and host[3] == 1 and host[4] == "''"  # NOT NULL DEFAULT ''
    assert "sender_host" in {c[1] for c in columns(con, "messages")}
    assert "remotes" in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    idx = con.execute("SELECT sql FROM sqlite_master WHERE name='participants_host_mcp'").fetchone()
    assert idx is not None and "WHERE ended_at IS NULL" in idx[0]
    assert backups(tmp_path / "switchboard.db") == []  # nothing to back up


def test_v1_fixture_migrates_to_v2(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    make_v1(p)
    src = sqlite3.connect(p)
    before = db.row_counts(src, V1_TABLES)
    src.close()
    con = db.open_db(p)
    assert db.schema_version(con) == 2
    assert db.integrity_ok(con) is None
    assert db.row_counts(con, V1_TABLES) == before
    assert db.row_counts(con, ("remotes",)) == {"remotes": 0}
    # the migrated schema is the fresh one: same columns in the same order, same indexes
    fresh = db.open_db(tmp_path / "fresh.db")
    for t in db.TABLES:
        assert columns(con, t) == columns(fresh, t), t
    q = "SELECT name, tbl_name FROM sqlite_master WHERE type='index' ORDER BY name"
    assert con.execute(q).fetchall() == fresh.execute(q).fetchall()
    # and the store works on it: the 0.2.0 rows are readable, new rows carry hosts
    store = Store(con)
    room = store.get_room("#build")
    assert room is not None and store.members(room.id)
    assert all(m.host == "" for m in store.members(room.id))
    assert all(m.sender_host is None for m in store.history(room.id))


def test_backup_written_before_alter_and_equal(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    original = make_v1(p)
    bak = db.backup_path_for(p)
    assert bak == tmp_path / "switchboard.db.v1.bak"
    seen: list[tuple[str, bool]] = []
    con = db.connect(p)
    # every statement the migration runs, with whether the backup existed by then
    con.set_trace_callback(lambda sql: seen.append((sql, bak.exists())))
    db.migrate(con, backup_to=bak)
    con.set_trace_callback(None)
    con.close()
    alters = [ok for sql, ok in seen if sql.lstrip().upper().startswith(("ALTER", "CREATE INDEX", "CREATE TABLE"))]
    assert alters and all(alters)
    assert (os.stat(bak).st_mode & 0o777) == 0o600
    assert dump(bak) == original  # the v1 database exactly as it was
    assert version(bak) == 1 and version(p) == 2
    # one self-contained file: rollback journal, no -wal or -shm left beside it
    c = sqlite3.connect(bak)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    c.close()
    assert not any(tmp_path.glob("switchboard.db.v1.bak-*"))


def test_backup_is_0600_whatever_the_umask(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    make_v1(p)
    old = os.umask(0o022)
    try:
        db.open_db(p).close()
    finally:
        os.umask(old)
    [bak] = backups(p)
    assert (os.stat(bak).st_mode & 0o777) == 0o600


def test_existing_backup_not_overwritten(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    original = make_v1(p)
    keep = db.backup_path_for(p)
    keep.write_bytes(b"an older backup the owner kept")
    db.open_db(p).close()
    assert keep.read_bytes() == b"an older backup the owner kept"
    new = [b for b in backups(p) if b != keep]
    assert len(new) == 1 and re.fullmatch(r"switchboard\.db\.v1\.bak\.\d+", new[0].name)
    assert (os.stat(new[0]).st_mode & 0o777) == 0o600
    assert dump(new[0]) == original


def test_backup_name_taken_twice_gets_a_counter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "switchboard.db"
    make_v1(p)
    monkeypatch.setattr(db.time, "time", lambda: 1_790_000_000.0)
    db.backup_path_for(p).write_bytes(b"one")
    (tmp_path / "switchboard.db.v1.bak.1790000000").write_bytes(b"two")
    db.open_db(p).close()
    assert (tmp_path / "switchboard.db.v1.bak").read_bytes() == b"one"
    assert (tmp_path / "switchboard.db.v1.bak.1790000000").read_bytes() == b"two"
    assert version(tmp_path / "switchboard.db.v1.bak.1790000000-1") == 1


def test_a_link_at_the_backup_name_is_never_followed(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    original = make_v1(p)
    target = tmp_path / "elsewhere.db"
    db.backup_path_for(p).symlink_to(target)  # dangling: O_CREAT alone would create the target
    db.open_db(p).close()
    assert not target.exists()
    [bak] = [b for b in backups(p) if not b.is_symlink()]
    assert dump(bak) == original


def test_existing_rows_get_empty_host(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    make_v1(p)
    con = db.open_db(p)
    hosts = {r[0] for r in con.execute("SELECT host FROM participants")}
    assert hosts == {""}
    assert {r[0] for r in con.execute("SELECT sender_host FROM messages")} == {None}
    store = Store(con)
    parts = [store.get_participant(r[0]) for r in con.execute("SELECT id FROM participants")]
    assert parts and all(x is not None and x.host == "" for x in parts)
    # the keys every earlier version wrote are the local keys
    assert all(x is not None and x.session_key.startswith(x.harness + ":") for x in parts)


def test_failed_migration_leaves_v1_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "switchboard.db"
    original = make_v1(p)
    bad = list(db.V1_TO_V2)
    bad[2] = "CREATE INDEX participants_host_mcp ON no_such_table(host)"  # after both ALTERs
    monkeypatch.setattr(db, "V1_TO_V2", tuple(bad))
    with pytest.raises(db.SchemaError, match="unchanged, still schema version 1"):
        db.open_db(p)
    assert version(p) == 1
    assert dump(p) == original  # the ALTERs before the failing statement were rolled back too
    [bak] = backups(p)
    assert dump(bak) == original  # the backup stays: it is a good one
    # fixed: the next start migrates, and backs up again under a new name
    monkeypatch.undo()
    con = db.open_db(p)
    assert db.schema_version(con) == 2
    assert len(backups(p)) == 2


def test_a_write_between_backup_and_migration_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The backup must be of the rows that are migrated: a write that lands after
    the copy (another writer: never the broker, which holds its lock) aborts."""
    p = tmp_path / "switchboard.db"
    original = make_v1(p)
    real = db.backup_verified

    def backup_then_write(con: sqlite3.Connection, to: Path) -> tuple[Path, dict[str, int]]:
        out = real(con, to)
        other = sqlite3.connect(p, isolation_level=None)
        other.execute("INSERT INTO events(ts, kind) VALUES(1.0, 'late')")
        other.close()
        return out

    monkeypatch.setattr(db, "backup_verified", backup_then_write)
    with pytest.raises(db.SchemaError, match="changed while its migration backup was made"):
        db.open_db(p)
    assert version(p) == 1
    assert len(dump(p)) == len(original) + 1


def test_a_bad_backup_is_removed_and_nothing_migrates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "switchboard.db"
    original = make_v1(p)
    real = db.row_counts
    calls = {"n": 0}

    def counts(con: sqlite3.Connection, tables: tuple[str, ...] = db.V1_TABLES) -> dict[str, int]:
        calls["n"] += 1
        out = real(con, tables)
        if calls["n"] == 2:  # the copy's counts
            out["messages"] -= 1
        return out

    monkeypatch.setattr(db, "row_counts", counts)
    with pytest.raises(db.SchemaError, match="does not match"):
        db.open_db(p)
    assert version(p) == 1 and dump(p) == original
    assert backups(p) == []  # a copy that failed its check is not left behind as a backup


def test_migration_needs_a_backup_path(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    original = make_v1(p)
    con = db.connect(p)
    with pytest.raises(db.SchemaError, match="needs a backup path"):
        db.migrate(con)
    con.close()
    assert dump(p) == original and backups(p) == []


def test_v3_refused(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    db.open_db(p).close()
    con = sqlite3.connect(p)
    con.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    con.commit()
    con.close()
    with pytest.raises(db.SchemaError, match="schema version 3 is not supported"):
        db.open_db(p)
    assert version(p) == 3 and backups(p) == []


def test_v2_reopens_without_migrating(tmp_path: Path) -> None:
    p = tmp_path / "switchboard.db"
    make_v1(p)
    db.open_db(p).close()
    [bak] = backups(p)
    first = dump(p)
    seen: list[str] = []
    con = db.connect(p)
    con.set_trace_callback(seen.append)
    assert db.migrate(con, backup_to=db.backup_path_for(p)) == 2
    con.set_trace_callback(None)
    con.close()
    assert not [s for s in seen if s.lstrip().upper().startswith(("ALTER", "CREATE", "UPDATE", "INSERT"))]
    assert backups(p) == [bak] and dump(p) == first


# ------------------------------------------------ the broker's start (daemon)
def _run_foreground(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listener_error: str) -> int:
    """``run_foreground`` up to its TCP listener, which fails (so no broker is served)."""
    import errno

    from switchboard.broker import daemon
    from switchboard.config import Config
    from switchboard.paths import Paths

    def no_port(port: int) -> Any:
        raise OSError(errno.EADDRINUSE, listener_error)

    monkeypatch.setattr(daemon, "setup_logging", lambda *a, **k: None)  # keep pytest's log handlers
    monkeypatch.setattr(daemon, "loopback_listener", no_port)
    old = os.umask(0o022)
    try:
        return daemon.run_foreground(Paths.from_home(tmp_path / "home"), Config(), port=0, announce=False)
    finally:
        os.umask(old)


def _home_db(tmp_path: Path) -> Path:
    from switchboard.paths import Paths

    paths = Paths.from_home(tmp_path / "home")
    paths.ensure()
    return paths.db


def test_the_broker_migrates_under_its_lock_before_it_listens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    p = _home_db(tmp_path)
    original = make_v1(p)
    assert _run_foreground(tmp_path, monkeypatch, "port taken") == 1
    assert "port taken" in capsys.readouterr().err
    assert version(p) == 2  # migrated before the listener was even asked for
    [bak] = backups(p)
    assert bak.name == "switchboard.db.v1.bak" and dump(bak) == original


def test_a_second_broker_never_migrates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    """The migration runs under broker.lock: while another broker holds it, nothing
    is copied or changed (two brokers can't migrate one file at once)."""
    import fcntl

    from switchboard.paths import Paths

    p = _home_db(tmp_path)
    original = make_v1(p)
    fd = os.open(Paths.from_home(tmp_path / "home").lockfile, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the running broker
        assert _run_foreground(tmp_path, monkeypatch, "never reached") == 1
        assert "another broker already runs" in capsys.readouterr().err
        assert version(p) == 1 and dump(p) == original and backups(p) == []
    finally:
        os.close(fd)
    assert _run_foreground(tmp_path, monkeypatch, "port taken") == 1  # the lock is free: now it migrates
    assert version(p) == 2 and len(backups(p)) == 1


@pytest.mark.parametrize("case", ["v3", "failed_migration"])
def test_a_refused_database_is_reported_on_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str], case: str) -> None:
    """``switchboard start`` shows the daemon's stderr (``broker.out``) when the broker
    doesn't come up: the reason must be there, not only in ``broker.log``."""
    p = _home_db(tmp_path)
    if case == "v3":
        db.open_db(p).close()
        c = sqlite3.connect(p)
        c.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
        c.commit()
        c.close()
        want = "schema version 3 is not supported"
    else:
        make_v1(p)
        monkeypatch.setattr(db, "V1_TO_V2", (*db.V1_TO_V2[:2], "CREATE INDEX x ON no_such_table(y)"))
        want = "still schema version 1; backup: switchboard.db.v1.bak"
    assert _run_foreground(tmp_path, monkeypatch, "never reached") == 1
    err = capsys.readouterr().err
    assert want in err and "never reached" not in err
