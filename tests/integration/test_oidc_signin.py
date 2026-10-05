"""Sign in with Google on a hosted broker (#70, DESIGN.md §38), through the real routes, with a
fake issuer in place of Google: only a person the admin added, by the Google email the admin
set for them, gets a session; nobody else does, whatever account they bring."""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import InProcBroker
from fakes.fake_oidc import CLIENT_ID, CLIENT_SECRET, FakeIssuer
from test_passkeys import Browser, hosted_broker, stop
from test_people import add, join_as_bob, set_up


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[InProcBroker, FakeIssuer]]:
    monkeypatch.setenv("SWITCHBOARD_OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("SWITCHBOARD_OIDC_CLIENT_SECRET", CLIENT_SECRET)
    b = hosted_broker()
    issuer = FakeIssuer()
    try:
        assert b.state.oidc is not None
        b.state.oidc.fetch = issuer.fetch
        yield b, issuer
    finally:
        stop(b)


def google_sign_in(
    b: InProcBroker, issuer: FakeIssuer, email: str, br: Browser | None = None
) -> tuple[Browser, Any]:
    """The browser's whole trip: start here, sign in at "Google", come back with the code."""
    br = br or Browser(b)
    r = br.get("/auth/oidc/start")
    assert r.status_code == 303, r.text
    state, code = issuer.authorize(r.headers["location"], email)
    r = br.get("/auth/oidc/callback?" + urllib.parse.urlencode({"state": state, "code": code}))
    return br, r


def set_google(admin: Browser, who: str | int, email: str | None) -> Any:
    return admin.post(f"/api/people/{who}/email", {"email": email})


def test_the_sign_in_page_offers_google_only_when_it_is_set_up(
    google: tuple[InProcBroker, FakeIssuer], monkeypatch: pytest.MonkeyPatch
) -> None:
    b, _ = google
    assert Browser(b).get("/api/auth/state").json()["sso"] == "google"
    monkeypatch.delenv("SWITCHBOARD_OIDC_CLIENT_SECRET")  # a client ID alone turns nothing on
    plain = hosted_broker()
    try:
        assert plain.state.oidc is None
        assert Browser(plain).get("/api/auth/state").json()["sso"] == "coming soon"
        assert Browser(plain).get("/auth/oidc/start").status_code == 404
    finally:
        stop(plain)


def test_a_person_the_admin_added_signs_in_with_their_google_account(google) -> None:
    b, issuer = google
    admin = set_up(b)
    bob = join_as_bob(b, admin)
    bob_id = next(p["id"] for p in admin.get("/api/people").json()["people"] if p["name"] == "bob")
    r = set_google(admin, bob_id, "Bob@Example.com")
    assert r.status_code == 200 and r.json()["person"]["email"] == "bob@example.com"

    br, r = google_sign_in(b, issuer, "bob@example.com")
    assert r.status_code == 200 and 'http-equiv="refresh"' in r.text  # a same-site hop: the cookie is Strict
    cookie = br.cookie_attrs("switchboard_session")
    assert "samesite=strict" in cookie and "secure" in cookie and "httponly" in cookie
    me = br.get("/api/me").json()
    assert me["human"] == "bob" and me["sso"] == "google"
    bob.close()


def test_the_admin_signs_in_with_their_own_google_email(google) -> None:
    b, issuer = google
    admin = set_up(b)
    assert set_google(admin, "owner", "admin@example.com").status_code == 200
    br, r = google_sign_in(b, issuer, "admin@example.com")
    assert r.status_code == 200 and br.get("/api/me").json()["admin"] is True


def test_nobody_else_gets_in(google) -> None:
    """An account nobody here signs in with, an unverified email, a person who was removed,
    and a callback in a browser that didn't start the sign-in all go back to the sign-in page."""
    b, issuer = google
    admin = set_up(b)
    _br, r = google_sign_in(b, issuer, "stranger@example.com")
    assert r.status_code == 303 and r.headers["location"] == "/?sso=not_allowed"

    add(admin, "carol")
    carol_id = next(p["id"] for p in admin.get("/api/people").json()["people"] if p["name"] == "carol")
    set_google(admin, carol_id, "carol@example.com")
    issuer.spoil = {"email_verified": False}
    _br, r = google_sign_in(b, issuer, "carol@example.com")
    assert r.headers["location"] == "/?sso=unverified"
    issuer.spoil = {}

    assert admin.post(f"/api/people/{carol_id}/remove", {}).status_code == 200
    _br, r = google_sign_in(b, issuer, "carol@example.com")
    assert r.headers["location"] == "/?sso=not_allowed"

    stranger = Browser(b)
    start = Browser(b).get("/auth/oidc/start")  # started in another browser
    state, code = issuer.authorize(start.headers["location"], "admin@example.com")
    r = stranger.get("/auth/oidc/callback?" + urllib.parse.urlencode({"state": state, "code": code}))
    assert r.headers["location"] == "/?sso=expired"
    r = stranger.get("/auth/oidc/callback?error=access_denied")
    assert r.headers["location"] == "/?sso=denied"
    assert stranger.get("/api/me").status_code == 401


def test_only_the_admin_sets_google_emails_and_each_is_one_persons(google) -> None:
    b, _ = google
    admin = set_up(b)
    bob = join_as_bob(b, admin)
    bob_id = next(p["id"] for p in admin.get("/api/people").json()["people"] if p["name"] == "bob")
    assert set_google(bob, bob_id, "bob@example.com").status_code == 403  # not the admin
    assert set_google(admin, bob_id, "not an email").status_code == 400
    assert set_google(admin, bob_id, "bob@example.com").status_code == 200
    r = set_google(admin, "owner", "BOB@example.com")
    assert r.status_code == 409 and "already" in r.json()["message"]
    assert set_google(admin, bob_id, None).json()["person"]["email"] is None
    assert set_google(admin, "owner", "bob@example.com").status_code == 200
    bob.close()


def test_a_google_sign_in_retires_the_invites_one_time_password(google) -> None:
    """Someone invited with a one-time password who signs in with Google instead is never asked
    to choose a password, and that one-time password stops working."""
    b, issuer = google
    admin = set_up(b)
    one_time = add(admin, "dave")["password"]
    dave_id = next(p["id"] for p in admin.get("/api/people").json()["people"] if p["name"] == "dave")
    set_google(admin, dave_id, "dave@example.com")
    br, r = google_sign_in(b, issuer, "dave@example.com")
    assert r.status_code == 200 and br.get("/api/me").json()["human"] == "dave"
    assert br.get("/").status_code == 200 and "/static/app.js" in br.get("/").text  # the app, not setup
    r = Browser(b).post("/api/signin/password", {"name": "dave", "password": one_time})
    assert r.status_code == 403
    dave = next(p for p in admin.get("/api/people").json()["people"] if p["name"] == "dave")
    assert dave["sign_in"] == "google"
