"""The room's own wake budget and hop limit, and a person's defaults, over the real web API
(#131, DESIGN.md §42). ``PUT /api/rooms/{slug}/wake`` is checked, bounded and never reachable
through chat, like ``PUT /api/rooms/{slug}/rules`` (#105); a person's own defaults are copied
into a room they create, and a later edit to either record never changes the other.
"""

from __future__ import annotations

import json

import httpx
from conftest import InProcBroker, cookie_of, ws_connect
from test_people import join_as_bob, set_up

pytest_plugins = ("test_people",)  # makes its hosted fixture available to this module


def test_defaults_copy_into_a_room_and_edits_do_not_change_them(
    broker: InProcBroker, web: httpx.Client
) -> None:
    """The person chooses defaults once; the room they make keeps its own numbers after that."""
    headers = broker.write_headers()
    assert (
        web.put(
            "/api/me/preferences", json={"budget_per_hour": 120, "hop_limit": 20}, headers=headers
        ).status_code
        == 200
    )
    created = web.post("/api/rooms", json={"name": "#wake"}, headers=headers)
    assert created.status_code == 200
    settings = created.json()["room"]["settings"]
    assert (settings["budget_per_hour"], settings["hop_limit"]) == (120, 20)

    # editing the room never changes the person's own defaults
    assert (
        web.put(
            "/api/rooms/wake/wake", json={"budget_per_hour": 10, "hop_limit": 2}, headers=headers
        ).status_code
        == 200
    )
    assert web.get("/api/me").json()["preferences"]["budget_per_hour"] == 120

    # and a later default change never reaches back into the already-created room
    web.put("/api/me/preferences", json={"budget_per_hour": 500, "hop_limit": 40}, headers=headers)
    assert broker.state.store.get_room("#wake").budget_per_hour == 10
    other = web.post("/api/rooms", json={"name": "#other-wake"}, headers=headers)
    other_settings = other.json()["room"]["settings"]
    assert (other_settings["budget_per_hour"], other_settings["hop_limit"]) == (500, 40)


def test_each_person_creates_rooms_from_their_own_defaults(hosted: InProcBroker) -> None:
    """A hosted person's room never copies another person's defaults (as #105's room rules)."""
    admin = set_up(hosted)
    bob = join_as_bob(hosted, admin)
    try:
        admin.c.put("/api/me/preferences", json={"budget_per_hour": 90}, headers=admin.headers(True))
        bob.c.put("/api/me/preferences", json={"hop_limit": 15}, headers=bob.headers(True))
        made_by_bob = bob.post("/api/rooms", {"name": "#bob-room"}).json()["room"]["settings"]
        made_by_admin = admin.post("/api/rooms", {"name": "#admin-room"}).json()["room"]["settings"]
        assert made_by_bob["hop_limit"] == 15 and made_by_bob["budget_per_hour"] == 60  # built-in rate
        assert made_by_admin["budget_per_hour"] == 90 and made_by_admin["hop_limit"] == 6
    finally:
        bob.close()
        admin.close()


def test_a_default_can_be_cleared_back_to_the_broker_default(broker: InProcBroker, web: httpx.Client) -> None:
    """Sending ``null`` un-sets a chosen default; Settings shows where the number comes from."""
    headers = broker.write_headers()
    web.put("/api/me/preferences", json={"budget_per_hour": 200}, headers=headers)
    me = web.get("/api/me").json()
    assert me["preferences"]["budget_per_hour"] == 200
    assert me["wake_defaults"]["budget_per_hour"] == {"value": 200, "source": "your default"}
    web.put("/api/me/preferences", json={"budget_per_hour": None}, headers=headers)
    me = web.get("/api/me").json()
    assert me["preferences"]["budget_per_hour"] is None
    assert me["wake_defaults"]["budget_per_hour"] == {"value": 60, "source": "built-in default"}


def test_wake_is_checked_bounded_and_needs_the_right_headers(broker: InProcBroker, web: httpx.Client) -> None:
    """Unknown fields, out-of-range numbers, no session and no X-Switchboard header are all
    refused, exactly as the rules route (#105) refuses them; nothing is reachable through chat."""
    headers = broker.write_headers()
    web.post("/api/rooms", json={"name": "#wake-cap"}, headers=headers)
    path = "/api/rooms/wake-cap/wake"
    good = {"budget_per_hour": 100, "hop_limit": 10}

    # no session at all
    assert httpx.put(broker.base + path, json=good, headers=broker.write_headers()).status_code == 401
    # a session but no X-Switchboard / wrong Origin (HostOriginGuard)
    assert web.put(path, json=good).status_code == 403
    # unknown or missing fields
    for body in ({}, {"budget_per_hour": 100}, {"hop_limit": 10}, {**good, "extra": 1}):
        assert web.put(path, json=body, headers=headers).status_code == 400
    # out of range, wrong type
    for bad in (
        {"budget_per_hour": -1, "hop_limit": 10},
        {"budget_per_hour": 1_000_001, "hop_limit": 10},
        {"budget_per_hour": 100, "hop_limit": 1001},
        {"budget_per_hour": 100, "hop_limit": -1},
        {"budget_per_hour": "100", "hop_limit": 10},
        {"budget_per_hour": 100, "hop_limit": True},
    ):
        assert web.put(path, json=bad, headers=headers).status_code == 400
    assert broker.state.store.get_room("#wake-cap").budget_per_hour == 60  # untouched by any refusal
    assert web.put(path, json=good, headers=headers).status_code == 200
    assert broker.state.store.get_room("#wake-cap").budget_per_hour == 100


def test_set_room_wake_updates_the_header_chips_live_and_posts_a_notice(
    broker: InProcBroker, web: httpx.Client
) -> None:
    """A saved change reaches an open browser at once, over the websocket, as /budget and
    /hops already do, with the same kind of notice and audit event."""
    headers = broker.write_headers()
    web.post("/api/rooms", json={"name": "#wake-live"}, headers=headers)
    ws = ws_connect(broker, cookie_of(web))
    try:
        ws.send(json.dumps({"t": "hello", "rooms": ["#wake-live"], "after": {}}))
        r = web.put(
            "/api/rooms/wake-live/wake",
            json={"budget_per_hour": 300, "hop_limit": 0},
            headers=headers,
        )
        assert r.status_code == 200
        settings = r.json()["settings"]
        assert (settings["budget_per_hour"], settings["budget_remaining"]) == (300, 300)
        assert settings["hop_limit"] == 0

        seen_room, seen_notice = False, False
        for _ in range(20):
            frame = json.loads(ws.recv(timeout=5.0))
            # the hello reply's own "room" snapshot (pre-update) arrives first; only the one
            # the PUT triggers carries the new rate
            if (
                frame.get("t") == "room"
                and frame.get("room") == "#wake-live"
                and frame.get("settings", {}).get("budget_per_hour") == 300
            ):
                seen_room = True
            if frame.get("t") == "msg" and frame["msg"].get("kind") == "notice":
                text = frame["msg"]["text"]
                if "wake budget" in text or "loop guard" in text:
                    seen_notice = True
            if seen_room and seen_notice:
                break
        assert seen_room and seen_notice
        kinds = {e.kind for e in broker.state.store.recent_events()}
        assert {"budget_set", "hop_limit_set"} <= kinds
    finally:
        ws.close()
