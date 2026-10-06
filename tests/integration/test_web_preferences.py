"""A signed-in person's Settings are broker-owned (DESIGN.md §29.2)."""

from __future__ import annotations

import httpx
from conftest import InProcBroker
from test_people import Browser, join_as_bob, set_up

pytest_plugins = ("test_people",)  # makes its hosted fixture available to this module

DEFAULT_PREFS = {
    "theme": "system",
    "text_size": "default",
    "room_rules": "",
    "budget_per_hour": None,
    "hop_limit": None,
}


def test_a_person_and_the_admin_have_independent_preferences(hosted: InProcBroker) -> None:
    """The session selects its own record; a person cannot change the admin's choice."""
    admin = set_up(hosted)
    bob = join_as_bob(hosted, admin)
    path = "/api/me/preferences"
    try:
        assert admin.c.put(path, json={"theme": "dark"}, headers=admin.headers(True)).status_code == 200
        assert bob.get("/api/me").json()["preferences"] == DEFAULT_PREFS
        assert bob.c.put(path, json={"theme": "light"}, headers=bob.headers(True)).status_code == 200
        assert admin.get("/api/me").json()["preferences"] == {**DEFAULT_PREFS, "theme": "dark"}
        assert bob.get("/api/me").json()["preferences"] == {**DEFAULT_PREFS, "theme": "light"}
        assert 'data-theme="dark"' in admin.get("/").text
        assert 'data-theme="light"' in bob.get("/").text
    finally:
        bob.close()
        admin.close()


def test_theme_is_private_persistent_and_checked(broker: InProcBroker, web: httpx.Client) -> None:
    """The owner's choice follows another browser and the next page load; unsafe or foreign writes fail."""
    assert web.get("/api/me").json()["preferences"] == DEFAULT_PREFS
    assert 'data-theme="system"' in web.get("/").text
    path = "/api/me/preferences"
    assert web.put(path, json={"theme": "dark"}).status_code == 403
    assert (
        httpx.put(broker.base + path, json={"theme": "dark"}, headers=broker.write_headers()).status_code
        == 401
    )
    good = broker.write_headers()
    assert web.put(path, json={"theme": "dark"}, headers=good).json() == {
        "preferences": {**DEFAULT_PREFS, "theme": "dark"}
    }
    assert web.get("/api/me").json()["preferences"] == {**DEFAULT_PREFS, "theme": "dark"}
    assert 'data-theme="dark"' in web.get("/").text  # before the browser's first paint
    other = broker.web_client()
    try:
        assert other.get("/api/me").json()["preferences"] == {**DEFAULT_PREFS, "theme": "dark"}
        assert 'data-theme="dark"' in other.get("/").text
    finally:
        other.close()
    for payload in ({"theme": "purple"}, {"theme": "light", "person_id": 1}, {}, {"theme": 1}):
        assert web.put(path, json=payload, headers=good).status_code == 400
    assert web.get("/api/me").json()["preferences"] == {**DEFAULT_PREFS, "theme": "dark"}


def test_text_size_is_saved_per_person_without_changing_the_theme(hosted: InProcBroker) -> None:
    """A size choice follows the signed-in person, leaves their theme alone, and rejects unknown steps."""
    anonymous = Browser(hosted)
    try:
        assert 'data-text-size="default"' in anonymous.get("/").text
    finally:
        anonymous.close()
    admin = set_up(hosted)
    bob = join_as_bob(hosted, admin)
    try:
        path = "/api/me/preferences"
        assert admin.c.put(path, json={"theme": "dark"}, headers=admin.headers(True)).status_code == 200
        changed = admin.c.put(path, json={"text_size": "larger"}, headers=admin.headers(True))
        assert changed.status_code == 200
        assert changed.json()["preferences"] == {**DEFAULT_PREFS, "theme": "dark", "text_size": "larger"}
        assert bob.get("/api/me").json()["preferences"] == DEFAULT_PREFS
        assert 'data-text-size="larger"' in admin.get("/").text
        assert 'data-text-size="default"' in bob.get("/").text
        for value in ("giant", 3, "", None):
            assert (
                admin.c.put(path, json={"text_size": value}, headers=admin.headers(True)).status_code == 400
            )
        assert admin.get("/api/me").json()["preferences"]["text_size"] == "larger"
    finally:
        bob.close()
        admin.close()


def test_budget_and_hop_limit_defaults_are_saved_bounded_and_clearable(
    broker: InProcBroker, web: httpx.Client
) -> None:
    """Each field is an integer in range, or null to go back to the broker's own default
    (#131); the theme and text size next to them are untouched."""
    path = "/api/me/preferences"
    good = broker.write_headers()
    assert web.put(path, json={"budget_per_hour": 120, "hop_limit": 15}, headers=good).json() == {
        "preferences": {**DEFAULT_PREFS, "budget_per_hour": 120, "hop_limit": 15}
    }
    for bad in (
        {"budget_per_hour": -1},
        {"budget_per_hour": 1_000_001},
        {"budget_per_hour": "60"},
        {"budget_per_hour": True},
        {"hop_limit": -1},
        {"hop_limit": 1001},
        {"hop_limit": 6.5},
    ):
        assert web.put(path, json=bad, headers=good).status_code == 400
    assert web.get("/api/me").json()["preferences"]["budget_per_hour"] == 120  # untouched by a refusal
    # null clears a chosen default back to the broker's own
    assert web.put(path, json={"budget_per_hour": None}, headers=good).json() == {
        "preferences": {**DEFAULT_PREFS, "hop_limit": 15}
    }
