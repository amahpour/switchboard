"""``/readyz`` (DESIGN.md §30.3, #953): the probe that reads the database, so a broker that is up but
can't read switchboard.db answers 503 and a platform restarts it, where ``/healthz`` stays 200."""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

import httpx
import pytest
from conftest import FakeClock, InProcBroker

from switchboard import db
from switchboard.store import PING_BUSY_MS, Store


def _fail(self: Store) -> None:
    raise sqlite3.OperationalError("disk I/O error")


def test_readyz_is_ok_on_a_working_broker_for_get_and_head(web: httpx.Client) -> None:
    """Fails (404) on a broker with no /readyz."""
    r = web.get("/readyz")
    assert (r.status_code, r.text) == (200, "ok\n")
    assert r.headers["cache-control"] == "no-store" and "set-cookie" not in r.headers
    h = web.head("/readyz")
    assert h.status_code == 200 and h.content == b""


def test_readyz_is_503_with_no_detail_when_the_database_cannot_be_read(
    web: httpx.Client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The store's read raising ``disk I/O error`` gives 503 and nothing of the error, while /healthz stays
    ok (what the probe is for); the first failure and the recovery are logged once each, not every probe.
    Fails if the route answers 200 without reading, leaks the text or the exception name, or logs
    per probe."""
    caplog.set_level(logging.INFO, logger="switchboard.web")
    with monkeypatch.context() as m:
        m.setattr(Store, "ping", _fail)
        for _ in range(3):
            r = web.get("/readyz")
            assert r.status_code == 503
            assert "disk" not in r.text.lower() and "sqlite" not in r.text.lower() and "Error" not in r.text
            assert web.head("/readyz").status_code == 503
        assert web.get("/healthz").text == "ok\n"
    assert web.get("/readyz").text == "ok\n"
    assert web.get("/readyz").text == "ok\n"
    msgs = [
        r.getMessage() for r in caplog.records if r.name == "switchboard.web" and "/readyz" in r.getMessage()
    ]
    assert len(msgs) == 2, msgs
    assert not any("disk" in m or "I/O" in m for m in msgs)


def test_readyz_is_503_not_a_500_while_the_store_is_not_open(broker: InProcBroker, web: httpx.Client) -> None:
    """Before the lifespan opens the store (startup, a migration, a restore at start, §57) state.store is
    None: 503, never a 500 from an AttributeError."""
    store = broker.state.store
    broker.state.store = None  # type: ignore[assignment]
    try:
        r = web.get("/readyz")
        assert r.status_code == 503 and r.text == "unavailable\n"
    finally:
        broker.state.store = store
    assert web.get("/readyz").status_code == 200


def _store_on_a_file(path: Path) -> Store:
    store = Store(db.open_db(path), FakeClock())
    store.meta_set("probe", "1")
    store.con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    store.ping()  # the connection is warm, as a running broker's is
    return store


def test_ping_reads_the_file_and_not_the_connections_memory(tmp_path: Path) -> None:
    """The file goes bad under a running broker. A plain SELECT on the warm connection still answers from
    its page cache (asserted, since that is the trap), so a probe made of it says ok through the very
    failure /readyz exists for. Fails if ``Store.ping`` doesn't drop the cache before it reads."""
    path = tmp_path / "switchboard.db"
    store = _store_on_a_file(path)
    os.truncate(path, 0)
    assert tuple(store.con.execute("SELECT 1 FROM meta LIMIT 1").fetchone()) == (1,)
    with pytest.raises(sqlite3.Error):
        store.ping()


def test_ping_waits_briefly_for_a_lock_and_puts_the_timeout_back(tmp_path: Path) -> None:
    """Another process holding the file must not stall the event loop for the connection's 5 s
    ``busy_timeout`` on every probe: the read runs under the shorter wait, and the broker's own calls get
    their 5 s back, after a read that failed too. Fails if the probe keeps the long wait or leaves the
    short one behind."""
    path = tmp_path / "switchboard.db"
    store = _store_on_a_file(path)
    before = store.con.execute("PRAGMA busy_timeout").fetchone()[0]
    assert before > PING_BUSY_MS
    seen: list[str] = []
    store.con.set_trace_callback(seen.append)
    store.ping()
    store.con.set_trace_callback(None)
    read = next(i for i, sql in enumerate(seen) if sql.startswith("SELECT 1 FROM meta"))
    assert f"PRAGMA busy_timeout={PING_BUSY_MS}" in seen[:read]
    assert f"PRAGMA busy_timeout={before}" in seen[read:]
    os.truncate(path, 0)
    with pytest.raises(sqlite3.Error):
        store.ping()
    assert store.con.execute("PRAGMA busy_timeout").fetchone()[0] == before
