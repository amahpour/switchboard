"""A signed-in person's Settings are broker-owned (DESIGN.md §29.2)."""

from __future__ import annotations

import httpx

from conftest import InProcBroker
from test_people import hosted, join_as_bob, set_up


def test_a_person_and_the_admin_have_independent_preferences(hosted: InProcBroker) -> None:
    """The session selects its own record; a person cannot change the admin's choice."""
    admin = set_up(hosted)
    bob = join_as_bob(hosted, admin)
    path = "/api/me/preferences"
    try:
        assert admin.c.put(path, json={"theme": "dark"}, headers=admin.headers(True)).status_code == 200
        assert bob.get("/api/me").json()["preferences"] == {"theme": "system"}
        assert bob.c.put(path, json={"theme": "light"}, headers=bob.headers(True)).status_code == 200
        assert admin.get("/api/me").json()["preferences"] == {"theme": "dark"}
        assert bob.get("/api/me").json()["preferences"] == {"theme": "light"}
        assert 'data-theme="dark"' in admin.get("/").text
        assert 'data-theme="light"' in bob.get("/").text
    finally:
        bob.close()
        admin.close()


def test_theme_is_private_persistent_and_checked(broker: InProcBroker, web: httpx.Client) -> None:
    """The owner's choice follows another browser and the next page load; unsafe or foreign writes fail."""
    assert web.get("/api/me").json()["preferences"] == {"theme": "system"}
    assert 'data-theme="system"' in web.get("/").text
    path = "/api/me/preferences"
    assert web.put(path, json={"theme": "dark"}).status_code == 403
    assert httpx.put(broker.base + path, json={"theme": "dark"}, headers=broker.write_headers()).status_code == 401
    good = broker.write_headers()
    assert web.put(path, json={"theme": "dark"}, headers=good).json() == {"preferences": {"theme": "dark"}}
    assert web.get("/api/me").json()["preferences"] == {"theme": "dark"}
    assert 'data-theme="dark"' in web.get("/").text  # before the browser's first paint
    other = broker.web_client()
    try:
        assert other.get("/api/me").json()["preferences"] == {"theme": "dark"}
        assert 'data-theme="dark"' in other.get("/").text
    finally:
        other.close()
    for payload in ({"theme": "purple"}, {"theme": "light", "person_id": 1}, {}, {"theme": 1}):
        assert web.put(path, json=payload, headers=good).status_code == 400
    assert web.get("/api/me").json()["preferences"] == {"theme": "dark"}
