"""``PUT /api/me/notify`` and ``notify`` in ``GET /api/me``: a person's browser-notifications choice (#723).

The broker only keeps the choice (a ``broker_settings`` row); the browser's permission is its own."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker


@pytest.fixture
def broker(tmp_home: Path) -> Iterator[InProcBroker]:
    b = InProcBroker(tmp_home).start()
    try:
        yield b
    finally:
        b.stop()


def test_the_choice_is_off_until_set_and_stored_for_the_person(broker: InProcBroker) -> None:
    """Off by default, on after a PUT, off again after another, and exactly true or false: anything else (a
    string, a number, extra keys, nothing) is refused and changes nothing. Fails if a non-boolean turns it on,
    or the choice isn't in /api/me."""
    web: Any = broker.web_client()
    h = broker.write_headers()
    assert web.get("/api/me").json()["notify"] is False
    assert web.put("/api/me/notify", json={"on": True}, headers=h).json() == {"notify": True}
    assert web.get("/api/me").json()["notify"] is True
    for bad in ({"on": "yes"}, {"on": 1}, {"on": True, "x": 1}, {}):
        assert web.put("/api/me/notify", json=bad, headers=h).status_code == 400, bad
    assert web.get("/api/me").json()["notify"] is True
    assert web.put("/api/me/notify", json={"on": False}, headers=h).json() == {"notify": False}
    assert web.get("/api/me").json()["notify"] is False
    web.close()


def test_the_route_needs_a_signed_in_session_and_the_origin_check(broker: InProcBroker) -> None:
    """No session is 401 and a write without the page's headers is refused. Fails if an unauthenticated
    request can set it."""
    import httpx

    anon = httpx.put(f"{broker.base}/api/me/notify", json={"on": True}, headers=broker.write_headers())
    assert anon.status_code == 401
    web: Any = broker.web_client()
    assert web.put("/api/me/notify", json={"on": True}).status_code in (400, 403)
    web.close()
