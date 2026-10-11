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
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock, InProcBroker, ws_connect
from fakes.fake_agent import FakeAgent
from fakes.fake_authenticator import SoftAuthenticator
from test_passkeys import PUBLIC, RP_ID, Browser, claim_link, events, hosted_broker, stop, token_of
from websockets.exceptions import InvalidStatus

from switchboard.broker.auth import WebOrigin
from switchboard.broker.passwords import FREE_FAILURES
from switchboard.broker.people import ONE_TIME_TTL_S
from switchboard.config import Config
from switchboard.models import NAME_REUSE_S

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


async def test_an_agent_cannot_join_as_a_person_or_take_their_name_prefix(hosted: InProcBroker) -> None:
    admin = await asyncio.to_thread(set_up, hosted)
    await asyncio.to_thread(admin.post, "/api/rooms", {"name": "#build"})
    await asyncio.to_thread(add, admin, "bob")
    async with FakeAgent(hosted.home, "name-clash") as agent:
        for name in ("bob", "bobby"):
            reply = await agent.join("#build", name)
            assert reply["code"] == "name_reserved", reply
        assert (await agent.join("#build", "helper"))["ok"]
        await asyncio.to_thread(admin.post, "/api/rooms/build/say", {"text": "@bob please check"})
        got = await agent.read("#build")
        assert "from=alice kind=human to_you=no" in got["text"], got
        assert "to_you=yes" not in got["text"], got


async def test_a_person_cannot_take_an_agents_live_or_recent_name(
    ticking: tuple[InProcBroker, FakeClock],
) -> None:
    broker, clock = ticking
    admin = await asyncio.to_thread(set_up, broker)
    await asyncio.to_thread(admin.post, "/api/rooms", {"name": "#build"})
    async with FakeAgent(broker.home, "person-clash") as agent:
        assert (await agent.join("#build", "bob"))["ok"]
        for active in (True, False):
            reply = await asyncio.to_thread(admin.post, "/api/people", {"name": "bob"})
            assert reply.status_code == 400 and "agent" in reply.json()["message"], reply.text
            if active:
                await agent.leave("#build")
        clock.advance(NAME_REUSE_S + 1)
        admin = Browser(broker)
        assert (await asyncio.to_thread(signin, admin, "alice", ADMIN_PW)).status_code == 200
        assert (await asyncio.to_thread(admin.post, "/api/people", {"name": "bob"})).status_code == 200


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
    assert r.status_code == 403 and r.json()["message"] == "wrong email or password"
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
        ("here", "reserved"),  # issue #111: broadcast mentions are reserved names too
        ("everyone", "reserved"),
        ("all", "reserved"),
        ("channel", "reserved"),
        ("humans", "reserved"),  # issue #138: the @humans mention is a reserved name too
        ("people", "reserved"),
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


def test_people_sign_in_with_their_email_once_they_have_one(hosted: InProcBroker) -> None:
    """#192: an email is who a person is. Once the admin sets one, it signs them in, in any
    case, and their name stops working (so a name, which anyone can guess, isn't a second way
    in); someone without an email yet still signs in by name. ``admin`` always works, and the
    admin's own display name stops once the admin has an email too."""
    admin = set_up(hosted)
    bob = join_as_bob(hosted, admin)
    pid = admin.get("/api/people").json()["people"][1]["id"]
    assert signin(Browser(hosted), "bob", BOB_PW).json()["next"] == "app"  # no email yet: the name
    assert signin(Browser(hosted), "bob@example.com", BOB_PW).status_code == 403
    r = admin.post(f"/api/people/{pid}/email", {"email": " Bob@Example.com "})
    assert r.status_code == 200 and r.json()["person"]["email"] == "bob@example.com", r.text
    for who in ("bob@example.com", "BOB@example.COM"):
        r = signin(Browser(hosted), who, BOB_PW)
        assert r.status_code == 200 and r.json()["human"] == "bob", who
    assert signin(Browser(hosted), "bob", BOB_PW).status_code == 403
    me = bob.get("/api/me")  # a session already signed in carries on, and knows who it signs in as
    assert me.status_code == 200 and me.json()["email"] == "bob@example.com"
    assert admin.get("/api/me").json()["email"] is None
    # the admin: by name until they have an email, by email after, and as admin always
    assert signin(Browser(hosted), "alice", ADMIN_PW).json()["next"] == "app"
    r = admin.post("/api/people/owner/email", {"email": "alice@example.com"})
    assert r.status_code == 200 and r.json()["person"]["email"] == "alice@example.com", r.text
    assert signin(Browser(hosted), "alice", ADMIN_PW).status_code == 403
    for who in ("alice@example.com", "admin"):
        r = signin(Browser(hosted), who, ADMIN_PW)
        assert r.status_code == 200 and r.json()["human"] == "alice", who
    # someone invited with an email set signs in with it and their one-time password, and the
    # Choose page's hidden username (for a password manager) is that email
    carol = add(admin, "carol")
    assert (
        admin.post(f"/api/people/{carol['person']['id']}/email", {"email": "carol@example.com"}).status_code
        == 200
    )
    br = Browser(hosted)
    r = signin(br, "carol@example.com", carol["password"])
    assert r.status_code == 200 and r.json()["next"] == "setup", r.text
    assert br.get("/api/setup/state").json()["email"] == "carol@example.com"
    # an email is one person's: not bob's for the admin, nor nonsense
    assert admin.post("/api/people/owner/email", {"email": "bob@example.com"}).status_code == 409
    assert admin.post(f"/api/people/{pid}/email", {"email": "alice@example.com"}).status_code == 409
    assert admin.post(f"/api/people/{pid}/email", {"email": "bob"}).status_code == 400
    # taking it away puts the name back
    assert admin.post(f"/api/people/{pid}/email", {"email": None}).status_code == 200
    assert signin(Browser(hosted), "bob", BOB_PW).json()["next"] == "app"
    assert signin(Browser(hosted), "bob@example.com", BOB_PW).status_code == 403
    evs = hosted.on_loop(lambda: hosted.state.store.recent_events(kinds=["people"], limit=200))
    got = sorted((e.id, e.data["person"], e.data["set"]) for e in evs if e.data.get("what") == "email")
    assert [g[1:] for g in got] == [("bob", True), ("alice", True), ("carol", True), ("bob", False)]
    assert bob.post(f"/api/people/{pid}/email", {"email": "x@example.com"}).status_code == 403


def test_someone_added_by_email_signs_in_with_it(hosted: InProcBroker) -> None:
    """#192: Add someone takes an email. Their name defaults to the email's local part, the
    invite says to sign in with the email, and it does (with the one-time password); their
    name doesn't. A name can be given instead of the default, and an email that's taken, isn't
    one, or makes no usable name is refused before anyone is added."""
    admin = set_up(hosted)
    r = admin.post("/api/people", {"email": "Bob.Smith+sb@Example.com"})
    assert r.status_code == 200, r.text
    got = r.json()
    assert (got["person"]["name"], got["person"]["email"]) == ("bob-smith-sb", "bob.smith+sb@example.com")
    one_time = got["password"]
    assert got["invite"] == (
        f"You're invited to switchboard: {PUBLIC}\nSign in with bob.smith+sb@example.com and the"
        f" one-time password {one_time} (it works for 7 days).\nRight after, you choose your own"
        " password, or a passkey."
    )
    assert signin(Browser(hosted), "bob-smith-sb", one_time).status_code == 403  # not by name
    r = signin(Browser(hosted), "bob.smith+sb@example.com", one_time)
    assert r.status_code == 200 and r.json()["next"] == "setup", r.text
    # a name of the admin's choosing instead of the default
    r = admin.post("/api/people", {"email": "carol@example.com", "name": "Cee"})
    assert r.status_code == 200 and r.json()["person"]["name"] == "cee", r.text
    # a new one-time password's invite says the email too
    pid = r.json()["person"]["id"]
    r = admin.post(f"/api/people/{pid}/password", {})
    assert r.status_code == 200 and "Sign in with carol@example.com and" in r.json()["invite"], r.text
    # refused, and nobody added
    n = len(admin.get("/api/people").json()["people"])
    for body, code, err in [
        ({"email": "carol@example.com"}, 409, "taken"),  # another person's email
        ({"email": "alice@example.com", "name": "al"}, 200, None),  # (set up below)
        ({"email": "not-an-email"}, 400, "bad_email"),
        ({"email": "42@example.com"}, 400, "bad_name"),  # nothing usable before the @
        ({"email": "dee@example.com", "name": "cee"}, 400, "bad_name"),  # a name that is taken
        ({"email": "dee@example.com", "first_name": "x" * 65}, 400, "bad_name"),
    ]:
        if code == 200:
            assert admin.post("/api/people/owner/email", {"email": "alice@example.com"}).status_code == 200
            body = {"email": "alice@example.com", "name": "al"}
            code, err = 409, "taken"  # the admin's own email
        r = admin.post("/api/people", body)
        assert r.status_code == code and r.json()["error"] == err, (body, r.text)
    assert "choose one for them" in admin.post("/api/people", {"email": "42@example.com"}).json()["message"]
    assert len(admin.get("/api/people").json()["people"]) == n


def test_someone_added_with_a_first_and_last_name(hosted: InProcBroker) -> None:
    """#192, §39.6: Add someone takes a first and a last name with the email. The name in the
    rooms comes from the first name (accents dropped), then with the last name's initial, then
    a digit, as each is taken; the invite greets them by first name; the People sheet, /api/me
    and Members carry the names."""
    admin = set_up(hosted)

    def add_named(first: str, last: str, email: str) -> dict[str, Any]:
        r = admin.post("/api/people", {"first_name": first, "last_name": last, "email": email})
        assert r.status_code == 200, r.text
        return r.json()

    got = add_named(" José ", "Martínez\u200b", "jose@example.com")
    assert (got["person"]["name"], got["person"]["first_name"], got["person"]["last_name"]) == (
        "jose",
        "José",
        "Martínez",
    )
    assert got["invite"].startswith(f"Hi José, you're invited to switchboard: {PUBLIC}\nSign in with jose@")
    assert add_named("Jose", "Mendez", "jm@example.com")["person"]["name"] == "jose-m"
    assert add_named("Jose", "Montoya", "jmo@example.com")["person"]["name"] == "jose2"
    # what the admin types wins over the default
    r = admin.post(
        "/api/people", {"first_name": "Bob", "last_name": "Smith", "email": "b@example.com", "name": "bobby"}
    )
    assert r.json()["person"]["name"] == "bobby"
    # the names reach the session, the People sheet and the room's members
    jose = Browser(hosted)
    assert signin(jose, "jose@example.com", got["password"]).json()["next"] == "setup"
    assert jose.get("/api/setup/state").json()["first_name"] == "José"
    assert jose.post("/api/me/password", {"password": BOB_PW}).status_code == 200
    me = jose.get("/api/me").json()
    assert (me["human"], me["first_name"], me["last_name"]) == ("jose", "José", "Martínez")
    full = admin.get("/api/me").json()["full_names"]
    assert full["jose"] == "José Martínez" and full["jose-m"] == "Jose Mendez" and "alice" not in full


def test_setup_takes_the_admins_email(hosted: InProcBroker) -> None:
    """#192: setup asks the admin for their email, and they sign in with it from then on. A bad
    one is refused before anything is claimed (and before a passkey ceremony is used up), and
    one over the column's 254 characters is a 400, not the database's error."""
    br = Browser(hosted)
    assert signin(br, "admin", token_of(claim_link(hosted))).json()["next"] == "setup"
    long = "a" * 60 + "@" + "b" * 186 + ".example"  # the right shape, but 255 characters
    for bad, code in [("alice", 400), (long, 400), (["a@example.com"], 400)]:
        r = br.post("/api/setup/password", {"password": ADMIN_PW, "email": bad})
        assert r.status_code == code and r.json()["error"] == "bad_email", (bad, r.text)
    assert hosted.state.store.owner_handle() is None  # nothing claimed
    r = br.post("/api/setup/password", {"password": ADMIN_PW, "email": "x@example.com", "first_name": 5})
    assert r.status_code == 400 and r.json()["error"] == "bad_name", r.text  # still nothing claimed
    assert hosted.state.store.owner_handle() is None
    r = br.post(
        "/api/setup/password",
        {"password": ADMIN_PW, "email": " Alice@Example.com ", "first_name": "Alice", "last_name": "Liddell"},
    )
    assert r.status_code == 200, r.text
    assert hosted.state.store.owner_email() == "alice@example.com"
    assert hosted.state.store.owner_names() == ("Alice", "Liddell")
    me = br.get("/api/me").json()
    assert (me["first_name"], me["last_name"]) == ("Alice", "Liddell")
    assert br.get("/api/me").json()["email"] == "alice@example.com"
    assert signin(Browser(hosted), "alice", ADMIN_PW).status_code == 403  # the name stops
    for who in ("alice@example.com", "admin"):
        assert signin(Browser(hosted), who, ADMIN_PW).json()["next"] == "app", who
    # the People card's route has the same limit
    r = br.post("/api/people/owner/email", {"email": long})
    assert r.status_code == 400 and r.json()["error"] == "bad_request", r.text


def test_a_passkey_setup_takes_the_admins_email(hosted: InProcBroker) -> None:
    br = Browser(hosted)
    assert signin(br, "admin", token_of(claim_link(hosted))).json()["next"] == "setup"
    auth = SoftAuthenticator(RP_ID, PUBLIC)
    r = br.post("/api/setup/begin")
    cred = auth.register(r.json()["options"])
    r = br.post("/api/setup/finish", {"credential": cred, "name": "Laptop", "email": "nope"})
    assert r.status_code == 400 and r.json()["error"] == "bad_email", r.text
    r = br.post("/api/setup/finish", {"credential": cred, "name": "Laptop", "email": "alice@example.com"})
    assert r.status_code == 200, r.text  # the bad email didn't use the ceremony up
    assert hosted.state.store.owner_email() == "alice@example.com"


def messages(br: Browser, room: str = "build") -> list[tuple[str, str, str]]:
    """A room's messages as a signed-in person reads them: (kind, from, text)."""
    return [
        (m["kind"], m["from"], m["text"]) for m in br.get(f"/api/rooms/{room}/messages").json()["messages"]
    ]


async def test_a_person_renames_themselves_and_the_agents_follow(hosted: InProcBroker) -> None:
    """#114, §41: bob renames himself in Settings. His session carries on as robert; what he
    said before keeps "bob"; every open room gets "bob is now robert"; the agents' who() and
    their next delivery name robert. With no email yet, robert signs him in and bob doesn't.
    A name that's another person's, an agent's, reserved or malformed is refused, and so is a
    field that isn't one of the three."""
    admin = await asyncio.to_thread(set_up, hosted)
    for room in ("#build", "#ops"):
        await asyncio.to_thread(admin.post, "/api/rooms", {"name": room})
    bob = await asyncio.to_thread(join_as_bob, hosted, admin)
    async with FakeAgent(hosted.home, "ka") as agent:
        assert (await agent.join("#build", "helper"))["ok"]
        await asyncio.to_thread(bob.post, "/api/rooms/build/say", {"text": "before"})
        for name, why in [
            ("alice", "someone here already"),
            ("helper", "an agent here is called helper"),
            ("admin", "reserved"),
            ("everyone", "reserved"),  # issue #111
            ("humans", "reserved"),  # issue #138
            ("Not A Name", "names look like"),
        ]:
            r = await asyncio.to_thread(bob.post, "/api/me/name", {"name": name})
            assert r.status_code == 400 and why in r.json()["message"], (name, r.text)
        r = await asyncio.to_thread(bob.post, "/api/me/name", {"name": "bob", "email": "x@example.com"})
        assert r.status_code == 400, r.text  # only name, first_name and last_name
        r = await asyncio.to_thread(
            bob.post, "/api/me/name", {"name": " Robert ", "first_name": "Robert", "last_name": "Builder"}
        )
        assert r.status_code == 200 and r.json() == {
            "name": "robert",
            "first_name": "Robert",
            "last_name": "Builder",
        }
        me = (await asyncio.to_thread(bob.get, "/api/me")).json()
        assert (me["human"], me["first_name"], me["full_names"]["robert"]) == (
            "robert",
            "Robert",
            "Robert Builder",
        )
        await asyncio.to_thread(bob.post, "/api/rooms/build/say", {"text": "after"})
        chat = [(f, t) for k, f, t in messages(admin) if k == "chat"]
        assert chat == [("bob", "before"), ("robert", "after")]
        for room in ("build", "ops"):
            assert ("notice", "switchboard", "bob is now robert") in messages(admin, room)
        who = await agent.who("#build")
        assert "alice and robert (your users, kind=human)" in who["text"], who["text"]
        got = (await agent.read("#build"))["text"]
        assert "from=robert kind=human" in got and "after" in got, got
    # no email yet: the new name signs him in, the old one doesn't
    assert signin(Browser(hosted), "robert", BOB_PW).json()["next"] == "app"
    assert signin(Browser(hosted), "bob", BOB_PW).status_code == 403
    people_ = admin.get("/api/people").json()["people"]
    assert [p["name"] for p in people_] == ["alice", "robert"]


def test_the_admin_renames_someone_and_themselves(hosted: InProcBroker) -> None:
    """#114: the admin renames anyone from People (with a fresh check), and themselves; the
    notice says who did it; the admin's new name is theirs in every session, keeps agents from
    joining under it, and outlives a restart."""
    admin = set_up(hosted)
    admin.post("/api/rooms", {"name": "#build"})
    bob = join_as_bob(hosted, admin)
    pid = admin.get("/api/people").json()["people"][1]["id"]
    assert bob.post(f"/api/people/{pid}/name", {"name": "rob"}).status_code == 403  # the admin's
    r = admin.post(f"/api/people/{pid}/name", {"name": "rob"})
    assert r.status_code == 200 and r.json()["name"] == "rob", r.text
    assert ("notice", "switchboard", "bob is now rob (renamed by alice)") in messages(admin)
    assert bob.get("/api/me").json()["human"] == "rob"
    # the admin renames themselves: a name that starts an agent CLI's is refused
    assert "start of an agent" in admin.post("/api/me/name", {"name": "cod"}).json()["message"]
    r = admin.post("/api/me/name", {"name": "ali"})
    assert r.status_code == 200 and r.json()["name"] == "ali", r.text
    assert hosted.state.cfg.human_name == "ali" and hosted.state.engine.cfg.human_name == "ali"
    assert all(a.cfg.human_name == "ali" for a in hosted.state.engine.adapters.values())
    assert admin.get("/api/me").json()["human"] == "ali"
    assert ("notice", "switchboard", "alice is now ali") in messages(admin)
    assert signin(Browser(hosted), "admin", ADMIN_PW).json()["human"] == "ali"
    assert [p["name"] for p in admin.get("/api/people").json()["people"]] == ["ali", "rob"]
    # the admin's check ran out: renaming someone asks again
    hosted.on_loop(lambda: hosted.state.passkey_checks.clear())
    assert admin.post(f"/api/people/{pid}/name", {"name": "bobby"}).json()["error"] == "reauth"
    assert admin.post("/api/me/name", {"name": "alice"}).json()["error"] == "reauth"
    # a restart keeps the admin's new name
    hosted.stop()
    again = InProcBroker(hosted.home, web_origin=WebOrigin.parse(PUBLIC)).start()
    try:
        assert again.state.cfg.human_name == "ali"
    finally:
        again.stop()
        hosted.start()


def test_a_name_set_by_the_environment_isnt_renamed(tmp_home: Path) -> None:
    """SWITCHBOARD_HUMAN_NAME names the owner when switchboard starts (a deployment's manifest):
    Settings shows it but can't change it, and a rename saved before doesn't win over it."""
    b = InProcBroker(
        tmp_home, web_origin=WebOrigin.parse(PUBLIC), cfg=Config(human_name="alice", human_name_from_env=True)
    ).start()
    try:
        admin = set_up(b)
        assert admin.get("/api/me").json()["name_locked"] is True
        r = admin.post("/api/me/name", {"name": "ali", "first_name": "Alice"})
        assert r.status_code == 409 and r.json()["error"] == "locked", r.text
        assert admin.post("/api/me/name", {"first_name": "Alice"}).status_code == 200  # names still change
        b.on_loop(lambda: b.state.store.set_owner_name("ali"))  # as if renamed before the variable was set
    finally:
        b.stop()
    b2 = InProcBroker(
        tmp_home, web_origin=WebOrigin.parse(PUBLIC), cfg=Config(human_name="alice", human_name_from_env=True)
    ).start()
    try:
        assert b2.state.cfg.human_name == "alice"
    finally:
        b2.stop()


def test_the_desktop_human_renames_themselves(broker: InProcBroker) -> None:
    """On a desktop broker the one human renames themselves from Settings, with no sign-in
    check (there's no password to check), and it sticks."""
    web = broker.web_client()
    try:
        r = web.post("/api/me/name", json={"name": "ali"}, headers=broker.write_headers())
        assert r.status_code == 200 and r.json()["name"] == "ali", r.text
        assert web.get("/api/me").json()["human"] == "ali"
        assert broker.state.store.owner_name() == "ali"
    finally:
        web.close()


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
