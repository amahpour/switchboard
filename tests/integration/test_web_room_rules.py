"""People own room rules; a creator's saved defaults are copied, then diverge."""

from __future__ import annotations

import httpx
from conftest import InProcBroker
from test_people import join_as_bob, set_up

pytest_plugins = ("test_people",)  # makes its hosted fixture available to this module


def test_defaults_copy_into_a_room_and_edits_do_not_change_them(
    broker: InProcBroker, web: httpx.Client
) -> None:
    """The person chooses defaults once; a room keeps its own later edits."""
    headers = broker.write_headers()
    assert (
        web.put(
            "/api/me/preferences", json={"room_rules": "Work in a worktree."}, headers=headers
        ).status_code
        == 200
    )
    created = web.post("/api/rooms", json={"name": "#rules"}, headers=headers)
    assert created.status_code == 200
    assert created.json()["room"]["rules"] == "Work in a worktree."
    changed = web.put("/api/rooms/rules/rules", json={"text": "Post a PR link."}, headers=headers)
    assert changed.status_code == 200
    assert web.get("/api/me").json()["preferences"]["room_rules"] == "Work in a worktree."
    assert broker.state.store.get_room("#rules").rules_text == "Post a PR link."
    other = web.post("/api/rooms", json={"name": "#other-rules"}, headers=headers)
    assert other.json()["room"]["rules"] == "Work in a worktree."


def test_rules_are_human_only_checked_and_capped(broker: InProcBroker, web: httpx.Client) -> None:
    """No unchecked or oversized write may change either settings record."""
    headers = broker.write_headers()
    web.post("/api/rooms", json={"name": "#rules-cap"}, headers=headers)
    assert web.put("/api/rooms/rules-cap/rules", json={"text": "bad"}).status_code == 403
    for text in ("x" * 2001, 3, None):
        assert web.put("/api/rooms/rules-cap/rules", json={"text": text}, headers=headers).status_code == 400
        assert web.put("/api/me/preferences", json={"room_rules": text}, headers=headers).status_code == 400
    assert (
        web.put("/api/rooms/rules-cap/rules", json={"text": "x" * 2000}, headers=headers).status_code == 200
    )


def test_each_person_creates_rooms_from_their_own_defaults(hosted: InProcBroker) -> None:
    """A hosted person's room never copies another person's defaults."""
    admin = set_up(hosted)
    bob = join_as_bob(hosted, admin)
    try:
        assert (
            admin.c.put(
                "/api/me/preferences", json={"room_rules": "Admin guidance."}, headers=admin.headers(True)
            ).status_code
            == 200
        )
        assert (
            bob.c.put(
                "/api/me/preferences", json={"room_rules": "Bob guidance."}, headers=bob.headers(True)
            ).status_code
            == 200
        )
        made_by_bob = bob.post("/api/rooms", {"name": "#bob-room"}).json()["room"]
        made_by_admin = admin.post("/api/rooms", {"name": "#admin-room"}).json()["room"]
        assert made_by_bob["rules"] == "Bob guidance."
        assert made_by_admin["rules"] == "Admin guidance."
    finally:
        bob.close()
        admin.close()
