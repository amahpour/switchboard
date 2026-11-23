"""``/readyz`` (DESIGN.md §30.3, #953): the probe that reads the database, so a broker that is up but
can't read switchboard.db answers 503 and a platform restarts it, where ``/healthz`` stays 200."""

from __future__ import annotations

import logging
import sqlite3

import httpx
import pytest
from conftest import InProcBroker

from switchboard.store import Store


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
