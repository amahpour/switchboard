"""Live backup and the daemon's restore boundary (DESIGN.md §57, §12)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from conftest import SubprocBroker

from switchboard import backup, db
from switchboard.paths import Paths
from switchboard.store import Store


def test_writes_commit_between_counts_and_copy_without_invalidating_backup(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deterministic WAL race: writer commits after source counts, before SQLite backup.

    Without a pinned read snapshot the existing helper sees different row counts and fails.
    The event handshake also proves backup doesn't reserve SQLite's writer lock.
    """
    paths = Paths.from_home(tmp_home)
    paths.ensure()
    db.open_db(paths.db).close()
    counted, committed = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            assert counted.wait(10)
            con = db.connect(paths.db)
            try:
                Store(con).create_room("#during", "alice", 60, 6)
            finally:
                con.close()
        except BaseException as e:
            errors.append(e)
        finally:
            committed.set()

    original = db.row_counts
    calls = 0

    def counts(con: sqlite3.Connection, tables: tuple[str, ...] = db.V1_TABLES) -> dict[str, int]:
        nonlocal calls
        result = original(con, tables)
        calls += 1
        if calls == 1:
            counted.set()
            assert committed.wait(10)
            assert not errors
        return result

    monkeypatch.setattr(db, "row_counts", counts)
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        archive = backup.create(paths, tmp_path / "live.tar")
    finally:
        counted.set()
        thread.join(10)
    assert not thread.is_alive() and not errors
    restored = Paths.from_home(tmp_path / "restored")
    backup.restore(restored, archive)
    with db.connect(restored.db, readonly=True) as con:
        assert db.integrity_ok(con) is None
        assert db.row_counts(con)["rooms"] == 0
    with db.connect(paths.db, readonly=True) as con:
        assert db.row_counts(con)["rooms"] == 1


def test_live_cli_backup_restore_lock_and_restore_once_at_start(tmp_home: Path, tmp_path: Path) -> None:
    """The foreground daemon owns the lock; restore runs before the next socket opens."""
    b = SubprocBroker(tmp_home).start()
    try:
        assert b.cli("create", "#before").returncode == 0
        archive = tmp_path / "live.tar"
        r = b.cli("backup", str(archive))
        assert r.returncode == 0, r.stderr
        assert b.cli("say", "#before", "still working").returncode == 0
        r = b.cli("restore", str(archive))
        assert r.returncode == 1 and "home lock" in r.stderr
        assert b.cli("create", "#after").returncode == 0
    finally:
        b.kill()
    paths = b.paths
    paths.restore_dir.mkdir(mode=0o700)
    archive.rename(paths.restore_dir / "live.tar")
    b = SubprocBroker(tmp_home).start()
    try:
        assert "#before" in b.cli("rooms").stdout
        assert "#after" not in b.cli("rooms").stdout
        assert "still working" not in b.cli("tail", "#before", "--no-follow").stdout
        assert b.cli("create", "#second").returncode == 0
    finally:
        b.kill()
    b = SubprocBroker(tmp_home).start()
    try:
        assert "#second" in b.cli("rooms").stdout
        assert (paths.restore_dir / "live.tar.used").exists()
        assert len(list(tmp_home.glob("before-restore-*"))) == 1
    finally:
        b.kill()


def test_startup_refuses_an_invalid_pending_archive(tmp_home: Path) -> None:
    """A bad restore cannot quietly fall back to serving the old database."""
    paths = Paths.from_home(tmp_home)
    paths.ensure()
    paths.restore_dir.mkdir(mode=0o700)
    (paths.restore_dir / "bad.tar").write_bytes(b"not a backup")
    with pytest.raises(RuntimeError, match="pending restore failed"):
        SubprocBroker(tmp_home).start()
    assert not paths.db.exists()


def test_older_schema_archive_is_migrated_by_the_real_daemon(tmp_home: Path, tmp_path: Path) -> None:
    """Restoring a released fixture before listening exercises the image's startup path too."""
    source = Paths.from_home(tmp_path / "old")
    source.ensure()
    fixture = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"
    with sqlite3.connect(source.db) as con:
        con.executescript(fixture.read_text())
    paths = Paths.from_home(tmp_home)
    paths.ensure()
    paths.restore_dir.mkdir(mode=0o700)
    backup.create(source, paths.restore_dir / "old.tar")
    b = SubprocBroker(tmp_home).start()
    try:
        with db.connect(paths.db, readonly=True) as con:
            assert db.schema_version(con) == db.SCHEMA_VERSION
            assert Store(con).meta_get("passkey_rp_id") == "switchboard.localhost"
        assert "did not record" in (tmp_home / "subproc.out").read_text()
        assert list(tmp_home.glob("switchboard.db.v*.bak"))
    finally:
        b.kill()


def test_newer_archive_refuses_start_and_keeps_the_current_db(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid future-version archive cannot displace a broker's current database."""
    paths = Paths.from_home(tmp_home)
    paths.ensure()
    db.open_db(paths.db).close()
    before = paths.db.read_bytes()
    source = Paths.from_home(tmp_path / "future")
    source.ensure()
    with monkeypatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", db.SCHEMA_VERSION + 1)
        db.open_db(source.db).close()
        paths.restore_dir.mkdir(mode=0o700)
        backup.create(source, paths.restore_dir / "future.tar")
    with pytest.raises(RuntimeError, match="schema"):
        SubprocBroker(tmp_home).start()
    assert paths.db.read_bytes() == before
    assert not paths.restore_marker.exists()
