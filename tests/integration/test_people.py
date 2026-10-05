"""People on a hosted broker (issue #61, DESIGN.md §32): the admin's one-time password in the
log, passwords, the admin section's one-time passwords for everyone else, the forced reset,
a passkey instead of a password, removal, and every person being every agent's user.

The broker sits behind ``https://sb.example.com`` as in ``test_passkeys.py``, whose
``Browser`` keeps the Secure cookies by hand.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import FakeClock, InProcBroker, ws_connect
from fakes.fake_agent import FakeAgent
from fakes.fake_authenticator import SoftAuthenticator
from test_passkeys import PUBLIC, RP_ID, Browser, claim_link, events, hosted_broker, stop, token_of
from websockets.exceptions import InvalidStatus

from switchboard.broker.passwords import FREE_FAILURES
from switchboard.broker.people import ONE_TIME_TTL_S

ADMIN_PW = "correct horse battery"
BOB_PW = "bob's own secret 1"


@pytest.fixture
def hosted() -> Iterator[InProcBroker]:
    b = hosted_broker()
    try:
        yield b
    finally:
        stop(b)


@pytest.fixture
def ticking() -> Iterator[tuple[InProcBroker, FakeClock]]:
    clock = FakeClock()
    b = hosted_broker(clock)
    try:
        yield b, clock
    finally:
        stop(b)


def signin(br: Browser, name: str, password: str) -> Any:
    return br.post("/api/signin/password", {"name": name, "password": password})


def set_up(b: InProcBroker, br: Browser | None = None) -> Browser:
    """The admin's first sign-in: the one-time password from the log, then their own."""
    br = br or Browser(b)
    r = signin(br, "admin", token_of(claim_link(b)))
    assert r.status_code == 200 and r.json()["next"] == "setup", r.text
    r = br.post("/api/setup/password", {"password": ADMIN_PW})
    assert r.status_code == 200, r.text
    return br


def add(admin: Browser, name: str) -> dict[str, Any]:
    r = admin.post("/api/people", {"name": name})
    assert r.status_code == 200, r.text
    return r.json()


def join_as_bob(b: InProcBroker, admin: Browser) -> Browser:
    one_time = add(admin, "bob")["password"]
    bob = Browser(b)
    r = signin(bob, "bob", one_time)
    assert r.status_code == 200 and r.json()["next"] == "setup", r.text
    assert bob.post("/api/me/password", {"password": BOB_PW}).status_code == 200
    return bob


# ------------------------------------------------------------------- the admin
def test_the_admin_sets_up_with_the_one_time_password_then_their_own(hosted: InProcBroker) -> None:
    br = Browser(hosted)
    tok = token_of(claim_link(hosted))
    assert signin(br, "admin", "WRONG-ONE0-TIME-PASS").status_code == 403
    assert signin(br, "bob", tok).status_code == 403  # the admin's, and only as the admin
    r = signin(br, "admin", tok.lower().replace("-", " "))  # as typed from the log: any case, any spacing
    assert r.status_code == 200 and r.json() == {
        "ok": True,
        "next": "setup",
        "human": "alice",
        "passkeys": True,
    }
    assert "switchboard_setup" in br.cookies and "switchboard_session" not in br.cookies
    assert br.get("/setup").status_code == 200  # the page to choose their own
    for weak, why in [
        ("short", "at least 10"),
        ("aaaaaaaaaaaa", "more than one or two"),
        ("alice", "at least 10"),
        (tok, "not a one-time one"),
    ]:
        r = br.post("/api/setup/password", {"password": weak})
        assert r.status_code == 400 and why in r.json()["message"], weak
    assert br.post("/api/setup/password", {"password": ADMIN_PW}).status_code == 200
    st = hosted.state
    assert st.claim is None and not st.paths.test_claim_link.exists()
    assert st.store.owner_handle() is not None and st.store.owner_password_hash().startswith("scrypt$")
    me = br.get("/api/me").json()
    assert (me["human"], me["admin"], me["password"], me["passkeys"]) == ("alice", True, True, 0)
    assert events(hosted, "claim") == [{"what": "claim", "via": "web", "with": "password"}]
    # the one-time password is spent; the admin's own works under either name
    assert signin(Browser(hosted), "admin", tok).status_code == 403
    for name in ("alice", "Admin", "ADMIN"):
        r = signin(Browser(hosted), name, ADMIN_PW)
        assert r.status_code == 200 and r.json()["next"] == "app", name
    assert signin(Browser(hosted), "alice", ADMIN_PW + "x").status_code == 403


def test_the_admin_may_take_a_passkey_instead(hosted: InProcBroker) -> None:
    br = Browser(hosted)
    assert signin(br, "admin", token_of(claim_link(hosted))).json()["next"] == "setup"
    auth = SoftAuthenticator(RP_ID, PUBLIC)
    r = br.post("/api/setup/begin")  # no token: the ceremony the sign-in started
    assert r.status_code == 200, r.text
    r = br.post("/api/setup/finish", {"credential": auth.register(r.json()["options"]), "name": "Laptop"})
    assert r.status_code == 200, r.text
    assert hosted.state.store.owner_password_hash() is None and hosted.state.store.passkey_count() == 1
    me = br.get("/api/me").json()
    assert (me["admin"], me["password"], me["passkeys"]) == (True, False, 1)
    # no password to sign in with, until they choose one in their settings
    assert signin(Browser(hosted), "admin", ADMIN_PW).status_code == 403
    assert br.post("/api/me/password", {"password": ADMIN_PW}).status_code == 200  # fresh from the claim
    assert signin(Browser(hosted), "alice", ADMIN_PW).json()["next"] == "app"


# -------------------------------------------------------------------- people
def test_a_teammate_joins_with_a_one_time_password(hosted: InProcBroker) -> None:
    admin = set_up(hosted)
    got = add(admin, "Bob")
    one_time = got["password"]
    assert got["person"]["name"] == "bob" and got["person"]["sign_in"] == "one-time"
    assert got["invite"] == (
        f"You're invited to switchboard: {PUBLIC}\nSign in as bob with the one-time password"
        f" {one_time} (it works for 7 days).\nRight after, you choose your own password, or a"
        " passkey."
    )
    bob = Browser(hosted)
    r = signin(bob, "BOB", one_time.lower().replace("-", ""))
    assert r.status_code == 200 and r.json() == {
        "ok": True,
        "next": "setup",
        "human": "bob",
        "passkeys": True,
    }
    # on the one-time password, the only thing to do is to choose their own
    page = bob.get("/")
    assert page.status_code == 200 and "/static/setup.js" in page.text
    for path in ("/api/me", "/api/rooms", "/api/machines", "/api/people"):
        assert bob.get(path).status_code == 401, path
    assert bob.post("/api/rooms/build/say", {"text": "hi"}).status_code == 401
    with pytest.raises((OSError, InvalidStatus)):
        ws_connect(hosted, bob.cookies["switchboard_session"], origin=PUBLIC, host="sb.example.com")
    assert bob.post("/api/me/password", {"password": "bob"}).status_code == 400
    assert bob.post("/api/me/password", {"password": BOB_PW}).status_code == 200
    me = bob.get("/api/me").json()
    assert (me["human"], me["admin"], me["password"]) == ("bob", False, True)
    assert bob.get("/api/people").status_code == 403  # the admin section is the admin's
    assert bob.post("/api/people", {"name": "carol"}).status_code == 403
    # the one-time password is gone; their own works
    assert signin(Browser(hosted), "bob", one_time).status_code == 403
    assert signin(Browser(hosted), "bob", BOB_PW).json()["next"] == "app"
    people = admin.get("/api/people").json()["people"]
    assert [(p["name"], p["admin"], p["sign_in"]) for p in people] == [
        ("alice", True, "admin"),
        ("bob", False, "password"),
    ]


def test_everyone_signed_in_is_equal_but_for_the_admin_section(hosted: InProcBroker) -> None:
    admin = set_up(hosted)
    admin.post("/api/rooms", {"name": "#build"})
    bob = join_as_bob(hosted, admin)
    r = bob.post("/api/rooms/build/say", {"text": "hello from bob"})
    assert r.status_code == 200
    [msg] = [m for m in bob.get("/api/rooms/build/messages").json()["messages"] if m["kind"] == "chat"]
    assert (msg["from"], msg["sender_kind"]) == ("bob", "human")
    r = bob.post("/api/rooms/build/command", {"text": "/pause"})
    assert r.status_code == 200 and r.json()["ok"]
    notices = [
        m["text"] for m in admin.get("/api/rooms/build/messages").json()["messages"] if m["kind"] == "notice"
    ]
    assert any(t.startswith("bob paused the room") for t in notices), notices
    # machines: anyone signed in, with a fresh check (bob just signed in)
    r = bob.post("/api/machines/pair", {"name": "bob-laptop"})
    assert r.status_code == 200, r.text
    members = bob.get("/api/rooms/build/members").json()
    assert members["human"] == "bob" and members["people"] == ["alice", "bob"]


async def test_every_person_is_every_agents_user(hosted: InProcBroker) -> None:
    admin = await asyncio.to_thread(set_up, hosted)
    await asyncio.to_thread(admin.post, "/api/rooms", {"name": "#build"})
    bob = await asyncio.to_thread(join_as_bob, hosted, admin)
    async with FakeAgent(hosted.home, "ka") as agent:
        j = await agent.join("#build", "helper")
        assert (
            j["ok"] and "Your users are alice and bob (kind=human): each of them is your user." in j["text"]
        )
        r = await asyncio.to_thread(bob.post, "/api/rooms/build/say", {"text": "please run the tests"})
        assert r.status_code == 200
        got = await agent.read("#build")
        assert got["ok"]
        text = got["text"]
        assert "1 message from bob (your user, relayed by switchboard)" in text, text
        assert "from=bob kind=human" in text and "to_you=yes" in text and "please run the tests" in text
        who = await agent.who("#build")
        assert "alice and bob (your users, kind=human)" in who["text"]


def test_the_admin_gives_a_new_one_time_password(hosted: InProcBroker) -> None:
    admin = set_up(hosted)
    bob = join_as_bob(hosted, admin)
    r = admin.post("/api/people/2/password")
    assert r.status_code == 404  # people ids start at 1
    [pid] = [p["id"] for p in admin.get("/api/people").json()["people"] if p["name"] == "bob"]
    r = admin.post(f"/api/people/{pid}/password")
    assert r.status_code == 200 and r.json()["person"]["sign_in"] == "one-time"
    fresh = r.json()["password"]
    assert bob.get("/api/me").status_code == 401  # signed out everywhere
    assert signin(Browser(hosted), "bob", BOB_PW).status_code == 403  # the old password is gone
    again = Browser(hosted)
    assert signin(again, "bob", fresh).json()["next"] == "setup"
    assert again.post("/api/me/password", {"password": "a newer secret 2"}).status_code == 200
    assert events(hosted, "session")[-1]["person"] == "bob"


def test_the_admin_removes_someone(hosted: InProcBroker) -> None:
    admin = set_up(hosted)
    admin.post("/api/rooms", {"name": "#build"})
    bob = join_as_bob(hosted, admin)
    bob.post("/api/rooms/build/say", {"text": "hello from bob"})
    [pid] = [p["id"] for p in admin.get("/api/people").json()["people"] if p["name"] == "bob"]
    r = admin.post(f"/api/people/{pid}/remove")
    assert r.status_code == 200 and r.json() == {"ok": True, "sessions": 1, "machines": []}
    assert bob.get("/api/me").status_code == 401
    r = signin(Browser(hosted), "bob", BOB_PW)
    assert r.status_code == 403 and r.json()["message"] == "wrong name or password"
    assert [p["name"] for p in admin.get("/api/people").json()["people"]] == ["alice"]
    [msg] = [m for m in admin.get("/api/rooms/build/messages").json()["messages"] if m["kind"] == "chat"]
    assert msg["from"] == "bob"  # their messages keep their name
    assert admin.post(f"/api/people/{pid}/remove").status_code == 404
    # the name is free again: someone new can have it
    assert add(admin, "bob")["person"]["id"] != pid


def test_one_time_passwords_expire_after_a_week(ticking: tuple[InProcBroker, FakeClock]) -> None:
    b, clock = ticking
    admin = set_up(b)
    one_time = add(admin, "bob")["password"]
    clock.advance(ONE_TIME_TTL_S + 1)
    r = signin(Browser(b), "bob", one_time)
    assert r.status_code == 403 and r.json()["error"] == "expired"
    admin = Browser(b)  # the admin's own session ran out in that week too
    assert signin(admin, "admin", ADMIN_PW).status_code == 200
    people = admin.get("/api/people").json()["people"]
    assert people[1]["sign_in"] == "expired"


def test_wrong_passwords_slow_down(ticking: tuple[InProcBroker, FakeClock]) -> None:
    b, clock = ticking
    set_up(b)
    br = Browser(b)
    for _ in range(FREE_FAILURES):
        assert signin(br, "alice", "not it at all").status_code == 403
    r = signin(br, "alice", ADMIN_PW)  # even the right one waits
    assert r.status_code == 429 and "wait 31 s" in r.json()["message"]
    assert signin(br, "nobody", "not it at all").status_code == 403  # other names are not held up
    clock.advance(31.0)
    assert signin(br, "alice", ADMIN_PW).status_code == 200


def test_a_passkey_instead_of_a_password(hosted: InProcBroker) -> None:
    admin = set_up(hosted)
    one_time = add(admin, "bob")["password"]
    bob = Browser(hosted)
    assert signin(bob, "bob", one_time).json()["next"] == "setup"
    auth = SoftAuthenticator(RP_ID, PUBLIC)
    r = bob.post("/api/passkeys/begin")
    assert r.status_code == 200, r.text
    opts = r.json()["options"]
    assert opts["publicKey"]["user"]["name"] == "bob"
    r = bob.post("/api/passkeys", {"credential": auth.register(opts), "name": "Bob's phone"})
    assert r.status_code == 200 and r.json()["passkeys"] == 1
    assert bob.get("/api/me").json()["password"] is False  # no password: the passkey instead
    assert signin(Browser(hosted), "bob", one_time).status_code == 403
    again = Browser(hosted)
    r = again.post("/api/passkey/begin")
    r = again.post("/api/passkey/finish", {"credential": auth.get(r.json()["options"])})
    assert r.status_code == 200 and r.json()["human"] == "bob"
    assert again.get("/api/me").json()["human"] == "bob"
    row = [p for p in admin.get("/api/people").json()["people"] if p["name"] == "bob"][0]
    assert (row["sign_in"], row["passkeys"]) == ("passkey", 1)


def test_names_people_can_have(hosted: InProcBroker) -> None:
    admin = set_up(hosted)
    add(admin, "bob")
    for bad, why in [
        ("alice", "reserved"),
        ("admin", "reserved"),
        ("switchboard-2", "reserved"),
        ("system", "reserved"),
        ("Bob", "someone here"),
        ("b@d", "names look like"),
        ("", "names look"),
        ("x" * 25, "names look like"),
    ]:
        r = admin.post("/api/people", {"name": bad})
        assert r.status_code == 400 and why in r.json()["message"], bad


def test_adding_someone_needs_a_fresh_check(ticking: tuple[InProcBroker, FakeClock]) -> None:
    b, clock = ticking
    admin = set_up(b)
    clock.advance(301.0)
    r = admin.post("/api/people", {"name": "bob"})
    assert r.status_code == 403 and r.json()["error"] == "reauth"
    assert admin.post("/api/signin/check", {"password": "not it"}).status_code == 403
    assert admin.post("/api/signin/check", {"password": ADMIN_PW}).status_code == 200
    assert admin.post("/api/people", {"name": "bob"}).status_code == 200
    # removing only takes access away: no check
    clock.advance(301.0)
    [pid] = [p["id"] for p in admin.get("/api/people").json()["people"] if p["name"] == "bob"]
    assert admin.post(f"/api/people/{pid}/remove").status_code == 200


def test_sign_out_everywhere_is_your_own_sessions(hosted: InProcBroker) -> None:
    admin = set_up(hosted)
    bob = join_as_bob(hosted, admin)
    bob2 = Browser(hosted)
    assert signin(bob2, "bob", BOB_PW).status_code == 200
    r = bob.post("/logout", {"all": True})
    assert r.status_code == 200 and r.json()["revoked"] == 2
    assert bob2.get("/api/me").status_code == 401
    assert admin.get("/api/me").status_code == 200  # the admin stays signed in


def test_the_auth_state_offers_sso_as_coming_soon(hosted: InProcBroker) -> None:
    st = Browser(hosted).get("/api/auth/state").json()
    assert (st["password"], st["passkeys_work"], st["sso"]) == (True, True, "coming soon")


def test_the_desktop_has_no_people(broker: InProcBroker) -> None:
    import httpx

    c = httpx.Client(base_url=broker.base)
    h = broker.write_headers()
    r = c.post("/api/signin/password", json={"name": "alice", "password": "x" * 12}, headers=h)
    assert r.status_code == 403 and r.json()["error"] == "no_passwords"
    web = broker.web_client()
    assert web.get("/api/people").status_code == 404
    me = web.get("/api/me").json()
    assert me["human"] == "alice" and me["admin"] is False and me["signin"] is False
    members_ok = web.post("/api/rooms", json={"name": "#build"}, headers=h).status_code == 200
    assert members_ok and web.get("/api/rooms/build/members").json()["people"] == ["alice"]
    assert json.loads(json.dumps(me))  # plain JSON
