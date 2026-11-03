"""Admins on a hosted broker (#569, DESIGN.md §51).

The owner makes bob an admin. Bob can then do everything the owner can, make dave an
admin and take it away, and change the owner's email and names. Nobody can remove the
owner or take the owner's admin away. A person whose admin is taken away is refused on
the next request. Each change is a people event and a notice that names who did it, and
the people frame's admins follows.

Already covered, so not repeated as the point of a test here: a person who was never an
admin is refused ``GET`` and ``POST /api/people``
(``test_people.py::test_a_teammate_joins_with_a_one_time_password``),
``GET /api/private-rooms``
(``test_private_rooms.py::test_the_admin_sees_names_and_people_and_a_removed_person_leaves``),
avatar upload and reset
(``test_agent_icons_web.py::test_a_person_who_is_not_the_admin_sees_the_icons_and_cannot_change_them``),
and a desktop ``GET /api/people`` (``test_people.py::test_the_desktop_has_no_people``).
"""

from __future__ import annotations

import json
import time
from typing import Any

from conftest import InProcBroker
from fakes import pictures
from test_people import (
    ADMIN_PW,
    BOB_PW,
    Browser,
    add,
    join_as_bob,
    set_up,
    signin,
)
from test_public_url import ws as public_ws

pytest_plugins = ("test_people",)

DAVE_PW = "dave's own secret 1"
PNG = pictures.png(8, 8, (20, 40, 60, 255))


def _frames(sock: Any, pred: Any) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        frame = json.loads(sock.recv(timeout=10))
        if pred(frame):
            return frame
    raise AssertionError("no matching frame")


def _fresh(broker: InProcBroker) -> None:
    broker.on_loop(lambda: broker.state.passkey_checks.clear())


def _check(who: Browser, password: str) -> None:
    assert who.post("/api/signin/check", {"password": password}).status_code == 200


def _icon(who: Browser, method: str, path: str, body: bytes | None = None) -> Any:
    headers = who.headers(True)
    if body is None:
        headers.pop("Content-Type", None)
    else:
        headers["Content-Type"] = "image/png"
    response = who.c.request(method, path, content=body, headers=headers)
    who.take(response)
    return response


def _join(broker: InProcBroker, admin: Browser, name: str, password: str) -> Browser:
    one_time = add(admin, name)["password"]
    who = Browser(broker)
    signed = signin(who, f"{name}@example.com", one_time)
    assert signed.status_code == 200 and signed.json()["next"] == "setup", signed.text
    assert who.post("/api/me/password", {"password": password}).status_code == 200
    return who


def test_an_admin_can_do_everything_the_owner_can(hosted: InProcBroker) -> None:
    """#569: bob, once an admin, manages people, avatars and private rooms, makes dave an
    admin and takes it away, and sets the owner's email and names. Without a fresh check,
    making an admin is 403 reauth; taking it away is not. The owner's admin and the owner
    cannot be removed. A body that isn't a boolean is 400, and an unknown or removed person
    is 404. Fails if a non-admin is let through, if admin sticks after it is taken away, or
    if the notice, the event or the people frame's admins omit who changed."""
    admin = set_up(hosted)
    assert admin.post("/api/rooms", {"name": "#secret", "private": True}).status_code == 200
    bob = join_as_bob(hosted, admin)
    bob_id = next(p["id"] for p in admin.get("/api/people").json()["people"] if p["name"] == "bob")
    assert admin.get("/api/me").json()["admin"] is True
    assert bob.get("/api/me").json()["admin"] is False

    sock = public_ws(hosted, admin.cookies["switchboard_session"])
    try:
        hello = _frames(sock, lambda f: f.get("t") == "people")
        assert hello["admins"] == ["alice"]
        assert "email" not in hello and "@" not in json.dumps(hello["admins"])

        _fresh(hosted)
        refused = admin.post(f"/api/people/{bob_id}/admin", {"admin": True})
        assert refused.status_code == 403 and refused.json()["error"] == "reauth", refused.text
        for body in ({"admin": "yes"}, {"admin": 1}, {}, {"admin": True, "extra": 1}):
            bad = admin.post(f"/api/people/{bob_id}/admin", body)
            assert bad.status_code == 400 and bad.json()["error"] == "bad_request", body
        assert admin.post("/api/people/999999/admin", {"admin": True}).status_code == 404
        for pid in ("owner", "0"):
            kept = admin.post(f"/api/people/{pid}/admin", {"admin": False})
            assert kept.status_code == 403, pid
            assert kept.json()["message"] == "nobody removes the owner or the owner's admin"
        _check(admin, ADMIN_PW)
        made = admin.post(f"/api/people/{bob_id}/admin", {"admin": True})
        assert made.status_code == 200 and made.json()["person"]["admin"] is True, made.text
        # the notice and the people frame are queued together; either may arrive first
        deadline = time.monotonic() + 10
        note, frame = None, None
        while time.monotonic() < deadline and (note is None or frame is None):
            got = json.loads(sock.recv(timeout=10))
            if got.get("t") == "notice" and got.get("text") == "alice made bob an admin":
                note = got
            if got.get("t") == "people" and got.get("admins") == ["alice", "bob"]:
                frame = got
        assert note is not None and frame is not None
        assert "email" not in frame and "@" not in json.dumps(frame["admins"])
    finally:
        sock.close()

    _check(bob, BOB_PW)  # the check above cleared his, and adding someone needs a fresh one
    listed = {p["name"]: p for p in bob.get("/api/people").json()["people"]}
    assert listed["alice"]["owner"] is True and listed["alice"]["admin"] is True
    assert listed["bob"]["owner"] is False and listed["bob"]["admin"] is True
    assert bob.get("/api/me").json()["admin"] is True
    assert bob.get("/api/me").json()["can_set_icons"] is True
    assert bob.get("/api/private-rooms").json()["rooms"][0]["name"] == "#secret"

    carol = bob.post("/api/people", {"name": "carol", "email": "carol@example.com"})
    assert carol.status_code == 200, carol.text
    carol_id = carol.json()["person"]["id"]
    again = bob.post(f"/api/people/{carol_id}/password", {})
    assert again.status_code == 200 and again.json()["password"], again.text
    mailed = bob.post(f"/api/people/{carol_id}/email", {"email": "carol.ng@example.com"})
    assert mailed.status_code == 200 and mailed.json()["person"]["email"] == "carol.ng@example.com"
    named = bob.post(f"/api/people/{carol_id}/name", {"first_name": "Carol", "last_name": "Ng"})
    assert named.status_code == 200 and named.json()["first_name"] == "Carol", named.text
    assert _icon(bob, "PUT", "/api/agent-icons/codex/light", PNG).status_code == 200
    assert bob.get("/api/agent-icons").json()["can_change"] is True
    assert _icon(bob, "DELETE", "/api/agent-icons/codex").status_code == 200
    assert bob.post(f"/api/people/{carol_id}/remove", {}).status_code == 200
    assert bob.post(f"/api/people/{carol_id}/admin", {"admin": True}).status_code == 404

    dave = _join(hosted, bob, "dave", DAVE_PW)
    dave_id = next(p["id"] for p in bob.get("/api/people").json()["people"] if p["name"] == "dave")
    try:
        _fresh(hosted)
        needs = bob.post(f"/api/people/{dave_id}/admin", {"admin": True})
        assert needs.status_code == 403 and needs.json()["error"] == "reauth"
        _check(bob, BOB_PW)
        made_dave = bob.post(f"/api/people/{dave_id}/admin", {"admin": True})
        assert made_dave.status_code == 200, made_dave.text
        assert dave.get("/api/people").status_code == 200
        assert dave.get("/api/private-rooms").status_code == 200

        _fresh(hosted)
        taken = bob.post(f"/api/people/{dave_id}/admin", {"admin": False})
        assert taken.status_code == 200 and taken.json()["person"]["admin"] is False, taken.text
        assert dave.get("/api/people").status_code == 403
        refused_routes = [
            dave.post("/api/people", {"name": "erin"}),
            dave.post(f"/api/people/{bob_id}/password", {}),
            dave.post(f"/api/people/{bob_id}/remove", {}),
            dave.post(f"/api/people/{bob_id}/email", {"email": "nope@example.com"}),
            dave.post(f"/api/people/{bob_id}/name", {"first_name": "Nope"}),
            dave.post(f"/api/people/{bob_id}/admin", {"admin": True}),
            dave.get("/api/private-rooms"),
            _icon(dave, "PUT", "/api/agent-icons/codex/light", PNG),
            _icon(dave, "DELETE", "/api/agent-icons/codex"),
        ]
        assert [r.status_code for r in refused_routes] == [403] * len(refused_routes)
        assert dave.get("/api/me").json()["admin"] is False
        assert dave.get("/api/me").json()["can_set_icons"] is False

        _check(bob, BOB_PW)
        owner_mail = bob.post("/api/people/owner/email", {"email": "alice@example.com"})
        assert owner_mail.status_code == 200 and owner_mail.json()["person"]["email"] == "alice@example.com"
        owner_name = bob.post("/api/people/owner/name", {"first_name": "Alice", "last_name": "Ng"})
        assert owner_name.status_code == 200, owner_name.text
        assert (owner_name.json()["first_name"], owner_name.json()["last_name"]) == ("Alice", "Ng")
        assert bob.post("/api/people/owner/admin", {"admin": False}).status_code == 403
        # "owner" is not a person id, so remove stays a 404, the same refusal as before
        assert bob.post("/api/people/owner/remove", {}).status_code == 404
        assert admin.get("/api/me").json()["admin"] is True

        erin = bob.post("/api/people", {"name": "erin", "email": "erin@example.com"})
        assert erin.status_code == 200, erin.text
        erin_id = erin.json()["person"]["id"]
        assert bob.post(f"/api/people/{erin_id}/admin", {"admin": True}).status_code == 200
        row = hosted.on_loop(lambda: hosted.state.store.person(erin_id))
        assert row is not None and row.admin is True
        assert bob.post(f"/api/people/{erin_id}/remove", {}).status_code == 200
        gone = hosted.on_loop(lambda: hosted.state.store.person(erin_id))
        assert gone is not None and gone.admin is False and gone.removed_at is not None
        again_erin = bob.post("/api/people", {"name": "erin", "email": "erin@example.com"})
        assert again_erin.status_code == 200 and again_erin.json()["person"]["admin"] is False

        own = bob.post(f"/api/people/{bob_id}/admin", {"admin": False})
        assert own.status_code == 200 and own.json()["person"]["admin"] is False, own.text
        assert bob.get("/api/people").status_code == 403
        assert admin.get("/api/people").status_code == 200
    finally:
        dave.close()
        bob.close()
        admin.close()

    evs = hosted.on_loop(lambda: hosted.state.store.recent_events(kinds=["people"], limit=50))
    changes = [
        (e.data["by"], e.data["person"], e.data["admin"]) for e in evs if e.data.get("what") == "admin"
    ]
    assert ("bob", "bob", False) in changes
    assert ("bob", "dave", False) in changes
    assert ("bob", "dave", True) in changes
    assert ("alice", "bob", True) in changes


def test_a_desktop_broker_has_no_admin_route(broker: InProcBroker) -> None:
    """A desktop broker has one person. The new route is a 404, as the other people routes are."""
    web = broker.web_client()
    try:
        response = web.post("/api/people/owner/admin", json={"admin": False}, headers=broker.write_headers())
        # a missing route is also 404; the body is the people-routes' own refusal
        assert response.status_code == 404 and response.json()["error"] == "not_found", response.text
        assert response.json()["message"] == "people sign in to a hosted switchboard only"
    finally:
        web.close()
