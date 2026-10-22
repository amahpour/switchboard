"""Agent tokens in the web API (#382, DESIGN.md §49.1): each person makes, lists and revokes their
own; a token is shown once and kept only as its hash; a device name is one live token's, and never
a dial-in machine's too; on a hosted broker, making one needs a fresh password or passkey check."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from conftest import InProcBroker
from test_passkeys import Browser, hosted_broker, stop
from test_people import ADMIN_PW, add, join_as_bob, set_up, signin

from switchboard.broker import agent_tokens

MAKE = "/api/agent-tokens"


@pytest.fixture
def hosted() -> Iterator[InProcBroker]:
    b = hosted_broker()
    try:
        yield b
    finally:
        stop(b)


def post(c: httpx.Client, b: InProcBroker, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
    return c.post(path, json=body or {}, headers=b.write_headers())


def test_the_owner_makes_lists_and_revokes_a_token_on_a_desktop_broker(broker: InProcBroker) -> None:
    """A desktop's owner (signed in with a login link, no password to re-check) makes a token,
    sees it once, then only its listing; revoking it takes it off the list. Fails if the listing
    or the events table ever holds the token or its hash, if the name isn't made canonical, or if
    a revoked token can be revoked again."""
    c = broker.web_client()
    assert c.get(MAKE).json() == {"tokens": [], "max": agent_tokens.MAX_PER_PERSON}
    r = post(c, broker, MAKE, {"device": "  Work-Laptop "})
    assert r.status_code == 200, r.text
    made = r.json()
    assert made["device"] == "work-laptop" and agent_tokens.TOKEN_RE.fullmatch(made["token"])
    assert r.headers["cache-control"] == "no-store"
    listed = c.get(MAKE).json()["tokens"]
    assert listed == [
        {
            **{k: made[k] for k in ("id", "device", "created_at", "last_used_at")},
            "via": "token",
            "client": None,
        }
    ]
    st = broker.state
    row = st.store.agent_token_for_device("work-laptop")
    assert row is not None and row.token_hash == agent_tokens.token_hash(made["token"])
    dump = "\n".join(st.store.con.iterdump())
    assert made["token"] not in dump and made["token"] not in c.get(MAKE).text
    assert row.token_hash not in c.get(MAKE).text

    again = post(c, broker, MAKE, {"device": "work-laptop"})
    assert again.status_code == 409 and "revoke it first" in again.json()["message"]
    r = post(c, broker, f"{MAKE}/{made['id']}/revoke")
    assert r.status_code == 200 and r.json() == {"ok": True, "device": "work-laptop", "ended": 0}
    assert c.get(MAKE).json()["tokens"] == []
    assert st.store.agent_token_by_hash(row.token_hash) is None
    assert post(c, broker, f"{MAKE}/{made['id']}/revoke").status_code == 404
    logged = [r[0] for r in st.store.con.execute("SELECT data FROM events WHERE kind='agent_tokens'")]
    assert len(logged) == 2 and all(made["token"] not in d and row.token_hash not in d for d in logged)


@pytest.mark.parametrize("device", ["", "1laptop", "lap top", "x" * 25, "laptop!", None, 7])
def test_a_device_name_is_a_host_name(broker: InProcBroker, device: Any) -> None:
    """The device becomes its agents' host (§27.6), so it follows a host name's rule. Fails if any
    other name is taken."""
    c = broker.web_client()
    r = post(c, broker, MAKE, {"device": device})
    assert r.status_code == 400 and "work-laptop" in r.json()["message"]
    assert c.get(MAKE).json()["tokens"] == []


def test_nothing_without_a_session_and_nothing_by_a_bad_id(broker: InProcBroker) -> None:
    """The three routes need a signed-in session; a revoke of anything but a live token of one's
    own is 'no such agent token'. Fails if a request with no session gets a list or a token."""
    anon = httpx.Client(base_url=broker.base, timeout=10.0)
    assert anon.get(MAKE).status_code == 401
    assert post(anon, broker, MAKE, {"device": "laptop"}).status_code == 401
    assert post(anon, broker, f"{MAKE}/1/revoke").status_code == 401
    c = broker.web_client()
    for bad in ("0", "abc", "-1", "1" * 30):
        assert post(c, broker, f"{MAKE}/{bad}/revoke").status_code == 404, bad


def test_at_most_twenty_live_tokens_a_person(broker: InProcBroker) -> None:
    """A cap keeps a stolen session from minting tokens without end. Fails if the 21st is made,
    or if revoking one doesn't make room again."""
    c = broker.web_client()
    ids = []
    for i in range(agent_tokens.MAX_PER_PERSON):
        r = post(c, broker, MAKE, {"device": f"box-{i}"})
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    r = post(c, broker, MAKE, {"device": "one-more"})
    assert r.status_code == 409 and "revoke one" in r.json()["message"]
    assert post(c, broker, f"{MAKE}/{ids[0]}/revoke").status_code == 200
    assert post(c, broker, MAKE, {"device": "one-more"}).status_code == 200


def test_making_one_on_a_hosted_broker_needs_a_fresh_check(hosted: InProcBroker) -> None:
    """A token is access, so a hosted broker asks for a password or passkey check in the last five
    minutes first, as pairing a machine does (§31.6); listing and revoking don't. Fails if a
    session that hasn't just confirmed it's them can make one."""
    admin = set_up(hosted)
    hosted.state.passkey_checks.clear()  # the set-up's own check, gone
    r = admin.post(MAKE, {"device": "laptop"})
    assert r.status_code == 403 and r.json()["error"] == "reauth"
    assert admin.get(MAKE).json()["tokens"] == []
    assert admin.post("/api/signin/check", {"password": ADMIN_PW}).status_code == 200
    r = admin.post(MAKE, {"device": "laptop"})
    assert r.status_code == 200, r.text
    hosted.state.passkey_checks.clear()
    assert admin.post(f"{MAKE}/{r.json()['id']}/revoke").status_code == 200


def test_people_have_their_own_tokens_and_lose_them_when_removed(hosted: InProcBroker) -> None:
    """Bob sees and revokes only his tokens; a device name taken by the admin is taken for him
    too; removing bob revokes his. Fails if a person sees or revokes another's token, or a removed
    person's token still authenticates."""
    admin = set_up(hosted)
    bob = join_as_bob(hosted, admin)
    assert admin.post("/api/signin/check", {"password": ADMIN_PW}).status_code == 200
    assert bob.post("/api/signin/check", {"password": "bob's own secret 1"}).status_code == 200
    mine = admin.post(MAKE, {"device": "laptop"}).json()
    r = bob.post(MAKE, {"device": "laptop"})
    assert r.status_code == 409 and r.json()["message"] == "laptop is taken: pick another name"
    his = bob.post(MAKE, {"device": "bob-desk"}).json()
    assert [t["device"] for t in admin.get(MAKE).json()["tokens"]] == ["laptop"]
    assert [t["device"] for t in bob.get(MAKE).json()["tokens"]] == ["bob-desk"]
    assert bob.post(f"{MAKE}/{mine['id']}/revoke").status_code == 404
    assert admin.post(f"{MAKE}/{his['id']}/revoke").status_code == 404

    st = hosted.state
    pid = st.store.person_named("bob").id
    assert admin.post(f"/api/people/{pid}/remove").status_code == 200
    assert st.store.agent_token_by_hash(agent_tokens.token_hash(his["token"])) is None
    assert st.store.agent_token_by_hash(agent_tokens.token_hash(mine["token"])) is not None


def test_a_person_still_on_a_one_time_password_gets_nothing(hosted: InProcBroker) -> None:
    """Someone who hasn't chosen their own password yet may only do that (§32). Fails if they
    can list or make agent tokens first."""
    admin = set_up(hosted)
    one_time = add(admin, "bob")["password"]
    bob = Browser(hosted)
    assert signin(bob, "bob@example.com", one_time).json()["next"] == "setup"
    assert bob.get(MAKE).status_code == 401
    assert bob.post(MAKE, {"device": "desk"}).status_code == 401
