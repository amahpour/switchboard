"""A hosted broker's owner (issue #41, DESIGN.md §31): the claim link in the log, the claim
with a passkey, signing in with one, adding one, the reset, and where none of it applies.

The broker sits behind ``https://sb.example.com`` as the proxy forwards it (tests/integration/
test_public_url.py): plain http to its port with the browser's Host and Origin, cookies
returned by hand since they are Secure. The browser's authenticator is
``fakes.fake_authenticator.SoftAuthenticator``: real ES256 registrations and assertions, with
every field bendable. In test mode the current claim link is also in ``run/test-claim-link``.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import FakeClock, InProcBroker, SubprocBroker, make_tmp_home
from cryptography.hazmat.primitives.asymmetric import ec
from fakes.fake_authenticator import Excluded, SoftAuthenticator

from switchboard.broker import app as broker_app
from switchboard.broker.auth import SESSION_TTL_S, WebOrigin
from switchboard.broker.passkeys import CEREMONY_TTL_S, CLAIM_GRACE_S, CLAIM_TTL_S, FRESH_CHECK_S

PUBLIC = "https://sb.example.com"
HOST = "sb.example.com"
RP_ID = "sb.example.com"
LINK_RE = re.compile(r"^https://sb\.example\.com/setup#t=([0-9A-Z]{4}(?:-[0-9A-Z]{4}){3})$")  # §32.4


# ------------------------------------------------------------------ helpers
class Browser:
    """One browser at the public URL: it keeps every cookie the broker sets (they are Secure,
    so a jar on a plain-http client would drop them) and sends the write headers."""

    def __init__(self, b: InProcBroker, host: str = HOST, origin: str = PUBLIC):
        self.c = httpx.Client(
            base_url=f"http://127.0.0.1:{b.port}",
            timeout=10.0,
            follow_redirects=False,
            limits=httpx.Limits(keepalive_expiry=1.0),
        )
        self.host, self.origin = host, origin
        self.cookies: dict[str, str] = {}
        self.set_cookies: list[str] = []  # every Set-Cookie seen, raw

    def close(self) -> None:
        self.c.close()

    def headers(self, write: bool) -> dict[str, str]:
        h = {"Host": self.host}
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if write:
            h.update({"Origin": self.origin, "X-Switchboard": "1", "Content-Type": "application/json"})
        return h

    def take(self, r: httpx.Response) -> None:
        for sc in r.headers.get_list("set-cookie"):
            self.set_cookies.append(sc)
            name, _, value = sc.split(";")[0].partition("=")
            if "max-age=0" in sc.lower() or value in ("", '""'):
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value

    def get(self, path: str) -> httpx.Response:
        r = self.c.get(path, headers=self.headers(False))
        self.take(r)
        return r

    def post(self, path: str, body: dict[str, Any] | None = None, **extra: str) -> httpx.Response:
        h = self.headers(True)
        for k, v in extra.items():
            if v is None:
                h.pop(k, None)
            else:
                h[k] = v
        r = self.c.post(path, json=body if body is not None else {}, headers=h)
        self.take(r)
        return r

    def cookie_attrs(self, name: str) -> list[str]:
        """The attributes of the last Set-Cookie for ``name``, lowercased."""
        for sc in reversed(self.set_cookies):
            if sc.split("=", 1)[0] == name:
                return [p.strip().lower() for p in sc.split(";")[1:]]
        raise AssertionError(f"no Set-Cookie for {name}")


def hosted_broker(clock: Any = None, url: str = PUBLIC) -> InProcBroker:
    home = make_tmp_home()
    return InProcBroker(home, web_origin=WebOrigin.parse(url), clock=clock).start()


def stop(b: InProcBroker) -> None:
    b.stop()
    shutil.rmtree(b.home, ignore_errors=True)


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


def claim_link(b: InProcBroker) -> str:
    return b.paths.test_claim_link.read_text().strip()


def token_of(link: str, origin: str = "https://sb.example.com") -> str:
    m = LINK_RE.match(link.replace(origin, "https://sb.example.com", 1))
    assert m, link
    return m.group(1)


def events(b: InProcBroker, what: str) -> list[dict[str, Any]]:
    evs = b.on_loop(lambda: b.state.store.recent_events(kinds=["login"], limit=200))
    return [e.data for e in evs if e.data.get("what") == what]


def claim(
    b: InProcBroker,
    br: Browser | None = None,
    auth: SoftAuthenticator | None = None,
    name: str = "MacBook Pro",
) -> tuple[Browser, SoftAuthenticator, dict[str, Any]]:
    """The whole claim, as the page does it: begin with the link's token, create, finish."""
    br = br or Browser(b)
    auth = auth or SoftAuthenticator(RP_ID, PUBLIC)
    r = br.post("/api/setup/begin", {"token": token_of(claim_link(b))})
    assert r.status_code == 200, r.text
    cred = auth.register(r.json()["options"])
    r = br.post("/api/setup/finish", {"credential": cred, "name": name})
    assert r.status_code == 200, r.text
    return br, auth, r.json()


def sign_in(b: InProcBroker, br: Browser, auth: SoftAuthenticator, **kw: Any) -> httpx.Response:
    r = br.post("/api/passkey/begin")
    assert r.status_code == 200, r.text
    return br.post("/api/passkey/finish", {"credential": auth.get(r.json()["options"], **kw)})


def exec_login(b: InProcBroker, br: Browser) -> None:
    """A session from `switchboard login` (exec into the container), followed through the proxy."""
    url = b.login_url()
    r = br.get(url.removeprefix(PUBLIC))
    assert r.status_code == 303, r.text


# ------------------------------------------------------------ the claim link
def test_an_unclaimed_hosted_broker_prints_one_claim_link(hosted: InProcBroker) -> None:
    link = claim_link(hosted)
    tok = token_of(link)
    st = hosted.state
    assert st.claim is not None and st.claim.active and st.claim.issued == 1 and st.claim.check(tok)
    assert st.webauthn is not None and st.webauthn.rp_id == RP_ID
    assert hosted.on_loop(st.unclaimed)
    br = Browser(hosted)
    r = br.get("/setup")
    assert (
        r.status_code == 200 and "Choose how you&rsquo;ll sign in" in r.text and "/static/setup.js" in r.text
    )
    assert "setup#t=" not in r.text and tok not in r.text
    assert (
        r.headers["cache-control"] == "no-store"
        and "script-src 'self'" in r.headers["content-security-policy"]
    )
    assert br.get("/api/auth/state").json() == {
        "hosted": True,
        "claimed": False,
        "passkeys": False,
        "claim": True,
        "passkeys_work": True,
        "password": True,
        "sso": "coming soon",
    }
    assert "/static/login.js" in br.get("/").text  # the sign-in page, which login.js then adjusts
    # the claim page needs the public Host, as everything does
    assert Browser(hosted, host=f"127.0.0.1:{hosted.port}").get("/setup").status_code == 421


def test_a_bad_claim_token_is_refused_and_rate_limited(hosted: InProcBroker) -> None:
    br = Browser(hosted)
    tok = token_of(claim_link(hosted))
    for bad in [tok + "X", tok[:-1], tok.replace("-", "")[1:] + "0", "", None, 5, "x" * 300]:
        r = br.post("/api/setup/begin", {"token": bad})
        assert r.status_code == 403 and r.json()["error"] == "bad_claim", bad
    for i in range(20):
        assert br.post("/api/setup/begin", {"token": f"bad{i:040d}"}).status_code == 403
    bad = events(hosted, "bad_claim")
    assert len(bad) == 1 and bad[0]["count"] == 1  # one event per minute, whatever the count
    assert hosted.state.claim.check(tok)  # the real one is untouched
    # the write rules: Origin and X-Switchboard, as for every unsafe method
    assert br.post("/api/setup/begin", {"token": tok}, Origin=None).status_code == 403
    assert br.post("/api/setup/begin", {"token": tok}, **{"X-Switchboard": None}).status_code == 403
    assert br.post("/api/setup/begin", {"token": tok}, Origin="https://evil.com").status_code == 403


def test_the_claim(hosted: InProcBroker) -> None:
    br = Browser(hosted)
    tok = token_of(claim_link(hosted))
    r = br.post("/api/setup/begin", {"token": tok})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["human"] == "alice"
    pk = got["options"]["publicKey"]
    assert pk["rp"] == {"id": RP_ID, "name": "switchboard"}
    assert pk["user"]["name"] == "alice" and len(pk["user"]["id"]) >= 20
    assert pk["authenticatorSelection"] == {
        "residentKey": "required",
        "requireResidentKey": True,
        "userVerification": "required",
    }
    assert pk["attestation"] == "none" and "excludeCredentials" not in pk
    assert [p["alg"] for p in pk["pubKeyCredParams"]] == [-7, -8, -257]
    assert len(pk["challenge"]) >= 40
    attrs = br.cookie_attrs("switchboard_setup")
    assert {"httponly", "samesite=strict", "secure", "path=/", f"max-age={int(CEREMONY_TTL_S)}"} <= set(attrs)
    assert "switchboard_session" not in br.cookies  # nothing signed in yet

    auth = SoftAuthenticator(RP_ID, PUBLIC)
    cred = auth.register(got["options"])
    r = br.post("/api/setup/finish", {"credential": cred, "name": "  MacBook   Pro "})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "name": "MacBook Pro", "human": "alice"}
    attrs = br.cookie_attrs("switchboard_session")
    assert {"secure", "httponly", "samesite=strict", "path=/", f"max-age={SESSION_TTL_S}"} <= set(attrs)
    assert "switchboard_setup" not in br.cookies  # the ceremony cookie is gone
    # the owner: the handle the options named, and the one passkey
    st = hosted.state
    from fido2.utils import websafe_decode

    assert st.store.owner_handle() == websafe_decode(pk["user"]["id"])
    assert st.store.owner_claimed_at() is not None
    [row] = st.store.passkeys()
    assert (
        row.name == "MacBook Pro"
        and row.sign_count == 0
        and row.aaguid == "01020304-0506-0708-090a-0b0c0d0e0f10"
    )
    assert row.credential_id == auth.last_id and row.last_used_at is None
    # the token is spent, the link is gone, and the page redirects
    assert st.claim is None and not hosted.paths.test_claim_link.exists()
    assert br.post("/api/setup/begin", {"token": tok}).status_code == 403
    r = br.get("/setup")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert br.get("/api/auth/state").json() == {
        "hosted": True,
        "claimed": True,
        "passkeys": True,
        "claim": False,
        "passkeys_work": True,
        "password": True,
        "sso": "coming soon",
    }
    me = br.get("/api/me").json()
    assert me["hosted"] is True and me["passkeys"] == 1 and me["fresh"] is True
    assert "/static/app.js" in br.get("/").text  # signed in
    assert (
        st.store.web_session_via(next(iter(st.store.con.execute("SELECT id_hash FROM web_sessions")))[0])
        == "claim"
    )
    assert events(hosted, "claim") == [{"what": "claim", "via": "web", "passkey": "MacBook Pro"}]


def test_the_claim_ceremony_is_one_browser_at_a_time(hosted: InProcBroker) -> None:
    tok = token_of(claim_link(hosted))
    a, b = Browser(hosted), Browser(hosted)
    r = a.post("/api/setup/begin", {"token": tok})
    assert r.status_code == 200
    options_a = r.json()["options"]
    r = b.post("/api/setup/begin", {"token": tok})
    assert r.status_code == 409 and r.json()["error"] == "busy"  # another browser meanwhile
    # the same browser again: a new ceremony replaces its own
    r = a.post("/api/setup/begin", {"token": tok})
    assert r.status_code == 200
    options_a2 = r.json()["options"]
    assert options_a2["publicKey"]["challenge"] != options_a["publicKey"]["challenge"]
    auth = SoftAuthenticator(RP_ID, PUBLIC)
    # b has no ceremony: finishing is refused
    r = b.post("/api/setup/finish", {"credential": auth.register(options_a2), "name": "x"})
    assert r.status_code == 403 and r.json()["error"] == "no_ceremony"
    # a's answer to the first (replaced) options: the wrong challenge; the ceremony is dropped
    r = a.post("/api/setup/finish", {"credential": auth.register(options_a), "name": "x"})
    assert r.status_code == 400 and "challenge" in r.json()["message"].lower()
    r = a.post("/api/setup/finish", {"credential": auth.register(options_a2), "name": "x"})
    assert r.status_code == 403 and r.json()["error"] == "no_ceremony"
    assert hosted.state.store.passkey_count() == 0 and hosted.on_loop(hosted.state.unclaimed)
    # the token is still good: begin again, and a registration that fails verification
    r = a.post("/api/setup/begin", {"token": tok})
    assert r.status_code == 200
    for kw, why in [
        (dict(origin="https://evil.com"), "origin"),
        (dict(rp_id="evil.com"), "rp id"),
        (dict(uv=False), "verification"),
        (dict(up=False), "present"),
    ]:
        r = a.post("/api/setup/begin", {"token": tok})
        assert r.status_code == 200
        r = a.post("/api/setup/finish", {"credential": auth.register(r.json()["options"], **kw), "name": "x"})
        assert r.status_code == 400 and why in r.json()["message"].lower(), kw
    for bad in [None, "text", {"id": 1}, {"id": "a", "rawId": "b", "response": {}}]:
        assert a.post("/api/setup/begin", {"token": tok}).status_code == 200
        assert a.post("/api/setup/finish", {"credential": bad, "name": "x"}).status_code == 400, bad
    bad = events(hosted, "bad_claim")
    assert bad and bad[0]["count"] == 1
    # then the real thing, in this browser
    claim(hosted, a, auth)
    assert hosted.state.store.passkey_count() == 1


def test_the_ceremony_and_the_token_expire(ticking: tuple[InProcBroker, FakeClock]) -> None:
    b, clock = ticking
    br = Browser(b)
    tok = token_of(claim_link(b))
    r = br.post("/api/setup/begin", {"token": tok})
    assert r.status_code == 200
    auth = SoftAuthenticator(RP_ID, PUBLIC)
    cred = auth.register(r.json()["options"])
    clock.advance(CEREMONY_TTL_S)
    r = br.post("/api/setup/finish", {"credential": cred, "name": "x"})
    assert r.status_code == 403 and r.json()["error"] == "no_ceremony"
    assert br.post("/api/setup/begin", {"token": tok}).status_code == 200  # the token lives an hour
    clock.advance(CLAIM_TTL_S)
    r = br.post("/api/setup/begin", {"token": tok})
    assert r.status_code == 403 and r.json()["error"] == "bad_claim"
    assert not b.state.claim.active
    r = br.get("/setup")
    assert r.status_code == 303  # no live link: nothing to claim with, until the next one is printed
    assert br.get("/api/auth/state").json()["claim"] is False


def test_a_fresh_link_every_hour_while_unclaimed(hosted: InProcBroker) -> None:
    first = claim_link(hosted)
    hosted.on_loop(broker_app._announce_claim, hosted.state)  # what the hourly task does
    second = claim_link(hosted)
    assert second != first and hosted.state.claim.issued == 2
    br = Browser(hosted)
    assert br.post("/api/setup/begin", {"token": token_of(first)}).status_code == 403
    assert br.post("/api/setup/begin", {"token": token_of(second)}).status_code == 200
    claim(hosted, br)
    hosted.on_loop(broker_app._announce_claim, hosted.state)  # claimed: nothing more is printed
    assert not hosted.paths.test_claim_link.exists() and hosted.state.claim is None


# ----------------------------------------------------------------- sign-in
def test_sign_in_with_a_passkey(hosted: InProcBroker) -> None:
    owner, auth, _ = claim(hosted)
    owner.close()
    br = Browser(hosted)  # a new browser: nothing signed in
    assert br.get("/api/me").status_code == 401
    r = br.post("/api/passkey/begin")
    assert r.status_code == 200, r.text
    pk = r.json()["options"]["publicKey"]
    assert pk["rpId"] == RP_ID and pk["userVerification"] == "required" and "allowCredentials" not in pk
    attrs = br.cookie_attrs("switchboard_passkey")
    assert {"httponly", "samesite=strict", "secure", "path=/"} <= set(attrs)
    sealed = br.cookies["switchboard_passkey"]
    assert pk["challenge"] not in sealed.split(".")[1] and "." in sealed
    r = br.post("/api/passkey/finish", {"credential": auth.get(r.json()["options"], counter=3)})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "name": "MacBook Pro", "reauth": False, "human": "alice"}
    assert "secure" in br.cookie_attrs("switchboard_session") and "switchboard_passkey" not in br.cookies
    assert br.get("/api/me").status_code == 200
    row = hosted.state.store.passkey(auth.last_id)
    assert row.sign_count == 3 and row.last_used_at is not None
    sessions = {r[0]: r[1] for r in hosted.state.store.con.execute("SELECT id_hash, via FROM web_sessions")}
    assert sorted(sessions.values()) == ["claim", "passkey:MacBook Pro"]
    assert events(hosted, "session")[0] == {
        "what": "session",
        "via": "passkey",
        "passkey": "MacBook Pro",
        "person": "alice",
    }
    # the same sealed cookie and assertion again: a replay
    br.cookies["switchboard_passkey"] = sealed
    r = br.post("/api/passkey/finish", {"credential": auth.get({"publicKey": pk}, counter=4)})
    assert r.status_code == 403 and r.json()["error"] == "replay"


def test_what_a_sign_in_refuses(hosted: InProcBroker) -> None:
    owner, auth, _ = claim(hosted)
    owner.close()
    br = Browser(hosted)
    # no ceremony cookie
    r = br.post("/api/passkey/finish", {"credential": {}})
    assert r.status_code == 403 and r.json()["error"] == "no_ceremony"
    # a tampered seal
    r = br.post("/api/passkey/begin")
    opts = r.json()["options"]
    good = br.cookies["switchboard_passkey"]
    br.cookies["switchboard_passkey"] = good[:-3] + "xyz"
    r = br.post("/api/passkey/finish", {"credential": auth.get(opts)})
    assert r.status_code == 403 and r.json()["error"] == "no_ceremony"
    br.cookies["switchboard_passkey"] = good
    # a wrong origin, RP id, no user verification, another challenge, an unknown credential
    for kw, why in [
        (dict(origin="https://evil.com"), "origin"),
        (dict(rp_id="evil.com"), "rp id"),
        (dict(uv=False), "verified"),
        (dict(challenge="A" * 43), "challenge"),
        (dict(credential_id=b"\x01" * 32), "unknown credential"),
    ]:
        br.cookies["switchboard_passkey"] = good
        r = br.post("/api/passkey/finish", {"credential": auth.get(opts, **kw)})
        assert (
            r.status_code == 403
            and r.json()["error"] == "bad_credential"
            and why in r.json()["message"].lower()
        ), kw
    # a signature by another key over the right data
    other = SoftAuthenticator(RP_ID, PUBLIC)
    other.keys[auth.last_id] = ec.generate_private_key(ec.SECP256R1())
    other.last_id = auth.last_id
    br.cookies["switchboard_passkey"] = good
    r = br.post("/api/passkey/finish", {"credential": other.get(opts)})
    assert r.status_code == 403 and "signature" in r.json()["message"].lower()
    for bad in [None, "x", {"id": "a"}]:
        br.cookies["switchboard_passkey"] = good
        assert br.post("/api/passkey/finish", {"credential": bad}).status_code == 403, bad
    assert "switchboard_session" not in br.cookies
    bad = events(hosted, "bad_passkey")
    assert bad and bad[0]["count"] == 1
    # after all that, the real one still works with a fresh ceremony
    assert sign_in(hosted, br, auth).status_code == 200


def test_the_sign_count_must_grow(hosted: InProcBroker) -> None:
    owner, auth, _ = claim(hosted)
    owner.close()
    br = Browser(hosted)
    assert sign_in(hosted, br, auth, counter=0).status_code == 200  # never counted: 0 after 0
    assert sign_in(hosted, br, auth, counter=5).status_code == 200
    r = sign_in(hosted, br, auth, counter=5)
    assert r.status_code == 403 and r.json()["error"] == "sign_count"  # equal, non-zero: refused
    assert sign_in(hosted, br, auth, counter=4).status_code == 403
    assert sign_in(hosted, br, auth, counter=0).status_code == 403
    assert sign_in(hosted, br, auth, counter=6).status_code == 200
    assert hosted.state.store.passkey(auth.last_id).sign_count == 6


def test_a_sign_in_ceremony_expires(ticking: tuple[InProcBroker, FakeClock]) -> None:
    b, clock = ticking
    owner, auth, _ = claim(b)
    owner.close()
    br = Browser(b)
    r = br.post("/api/passkey/begin")
    cred = auth.get(r.json()["options"])
    clock.advance(CEREMONY_TTL_S)
    r = br.post("/api/passkey/finish", {"credential": cred})
    assert r.status_code == 403 and r.json()["error"] == "no_ceremony"


# ------------------------------------------------------------ adding a passkey
def test_the_backup_passkey_right_after_the_claim(hosted: InProcBroker) -> None:
    br, auth, _ = claim(hosted)
    r = br.post("/api/passkeys/begin")
    assert r.status_code == 200, r.text  # the claim's grace: no second check
    pk = r.json()["options"]["publicKey"]
    from fido2.utils import websafe_decode

    assert [websafe_decode(c["id"]) for c in pk["excludeCredentials"]] == [auth.last_id]
    assert websafe_decode(pk["user"]["id"]) == hosted.state.store.owner_handle()
    assert "switchboard_passkey_add" in br.cookies
    with pytest.raises(Excluded):
        auth.register(r.json()["options"])  # the same authenticator: a browser refuses (InvalidStateError)
    phone = SoftAuthenticator(RP_ID, PUBLIC)
    r = br.post("/api/passkeys", {"credential": phone.register(r.json()["options"]), "name": "iPhone"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "name": "iPhone", "passkeys": 2}
    assert "switchboard_passkey_add" not in br.cookies
    assert [p.name for p in hosted.state.store.passkeys()] == ["MacBook Pro", "iPhone"]
    assert events(hosted, "passkey_added") == [
        {"what": "passkey_added", "passkey": "iPhone", "person": "alice"}
    ]
    # the new one signs in
    other = Browser(hosted)
    assert sign_in(hosted, other, phone).json()["name"] == "iPhone"
    # the same credential can't be added twice
    r = br.post("/api/passkeys/begin")
    assert r.status_code == 200
    excluded = [websafe_decode(c["id"]) for c in r.json()["options"]["publicKey"]["excludeCredentials"]]
    assert sorted(excluded) == sorted([auth.last_id, phone.last_id])


def test_adding_a_passkey_needs_a_fresh_passkey_check(ticking: tuple[InProcBroker, FakeClock]) -> None:
    b, clock = ticking
    br, auth, _ = claim(b)
    clock.advance(CLAIM_GRACE_S)
    r = br.post("/api/passkeys/begin")
    assert r.status_code == 403 and r.json()["error"] == "reauth"  # the grace is over, and the check is stale
    assert br.get("/api/me").json()["fresh"] is False
    # a session from a login link (exec into the container) has never passed a check
    ex = Browser(b)
    exec_login(b, ex)
    assert ex.get("/api/me").json()["fresh"] is False
    r = ex.post("/api/passkeys/begin")
    assert r.status_code == 403 and r.json()["error"] == "reauth"
    # the check: the sign-in ceremony from a signed-in browser confirms that session, no new session
    n = b.state.store.web_session_count()
    r = sign_in(b, ex, auth, counter=1)
    assert r.status_code == 200 and r.json()["reauth"] is True
    assert b.state.store.web_session_count() == n
    assert not any(sc.startswith("switchboard_session=") for sc in ex.set_cookies[-2:])
    assert ex.get("/api/me").json()["fresh"] is True
    assert events(b, "passkey_check") == [{"what": "passkey_check", "passkey": "MacBook Pro"}]
    r = ex.post("/api/passkeys/begin")
    assert r.status_code == 200
    phone = SoftAuthenticator(RP_ID, PUBLIC)
    r = ex.post("/api/passkeys", {"credential": phone.register(r.json()["options"]), "name": "key"})
    assert r.status_code == 200 and r.json()["passkeys"] == 2
    clock.advance(FRESH_CHECK_S + 1)
    assert ex.post("/api/passkeys/begin").json()["error"] == "reauth"
    # a bad registration, and an add without its cookie
    assert sign_in(b, ex, auth, counter=2).status_code == 200
    r = ex.post("/api/passkeys/begin")
    key2 = SoftAuthenticator(RP_ID, PUBLIC)
    bad_cred = key2.register(r.json()["options"], origin="https://evil.com")
    r = ex.post("/api/passkeys", {"credential": bad_cred, "name": "x"})
    assert r.status_code == 400
    ex.cookies.pop("switchboard_passkey_add", None)
    r = ex.post("/api/passkeys", {"credential": {}, "name": "x"})
    assert r.status_code == 403 and r.json()["error"] == "no_ceremony"
    assert b.state.store.passkey_count() == 2
    # no session at all
    anon = Browser(b)
    assert anon.post("/api/passkeys/begin").status_code == 401
    assert anon.post("/api/passkeys", {"credential": {}}).status_code == 401


def test_sign_out_everywhere_keeps_the_passkeys(hosted: InProcBroker) -> None:
    br, auth, _ = claim(hosted)
    other = Browser(hosted)
    assert sign_in(hosted, other, auth).status_code == 200
    r = br.post("/logout", {"all": True})
    assert r.status_code == 200 and r.json()["revoked"] == 2
    assert br.get("/api/me").status_code == 401 and other.get("/api/me").status_code == 401
    assert hosted.state.store.passkey_count() == 1
    assert sign_in(hosted, other, auth, counter=1).status_code == 200  # back in with the passkey


def test_exec_login_still_works_after_the_claim(hosted: InProcBroker) -> None:
    claim(hosted)
    br = Browser(hosted)
    exec_login(hosted, br)
    me = br.get("/api/me").json()
    assert me["hosted"] is True and me["passkeys"] == 1 and me["fresh"] is False


# -------------------------------------------------------------------- reset
def test_the_reset_acts_once_per_value(monkeypatch: pytest.MonkeyPatch) -> None:
    b = hosted_broker()
    try:
        br, auth, _ = claim(b)
        st = b.state.store
        from switchboard import db

        with db.tx(st.con):  # a paired and approved machine (PR 2 makes these): back to pending at the reset
            st.con.execute(
                "INSERT INTO link_machines(name, key, key_fp, created_at, approved_at, approved_via)"
                " VALUES('work-laptop', X'00', 'SHA256:x', 1.0, 2.0, 'web')"
            )
        first_token = None
        monkeypatch.setenv("SWITCHBOARD_RESET_OWNER", "2026-09-30")
        b.restart()
        st = b.state.store
        assert st.passkey_count() == 0 and st.owner_handle() is None and st.web_session_count() == 0
        assert tuple(st.con.execute("SELECT approved_at, approved_via FROM link_machines").fetchone()) == (
            None,
            None,
        )
        assert b.state.claim is not None and b.paths.test_claim_link.exists()
        first_token = token_of(claim_link(b))
        assert events(b, "owner_reset") == [
            {"what": "owner_reset", "passkeys": 1, "sessions": 1, "machines_pending": 1, "people": 0}
        ]
        assert br.get("/api/me").status_code == 401  # the old session is gone
        # claimed again by the new owner; the same value at the next start changes nothing
        br2, auth2, _ = claim(b)
        b.restart()
        st = b.state.store
        assert st.passkey_count() == 1 and st.owner_handle() is not None and b.state.claim is None
        assert not b.paths.test_claim_link.exists()
        assert len(events(b, "owner_reset")) == 1
        assert br2.get("/api/me").status_code == 200  # sessions survive a plain restart
        # a new value resets again
        monkeypatch.setenv("SWITCHBOARD_RESET_OWNER", "2026-10-01")
        b.restart()
        st = b.state.store
        assert (
            st.passkey_count() == 0 and b.state.claim is not None and token_of(claim_link(b)) != first_token
        )
        assert len(events(b, "owner_reset")) == 2
        # the variable unset: nothing happens either
        monkeypatch.delenv("SWITCHBOARD_RESET_OWNER")
        claim(b)
        b.restart()
        assert b.state.store.passkey_count() == 1 and len(events(b, "owner_reset")) == 2
    finally:
        stop(b)


# ------------------------------------------------------- where it doesn't apply
@pytest.mark.parametrize(
    "url,why", [("http://sb.test", "not a secure context"), ("https://10.0.0.5", "IP address")]
)
def test_where_passkeys_cant_work_the_password_still_sets_it_up(
    url: str, why: str, caplog: pytest.LogCaptureFixture
) -> None:
    """No passkeys at an IP address or on plain http, but the one-time password in the log
    still sets the broker up, with a password (DESIGN.md §32.4)."""
    b = hosted_broker(url=url)
    try:
        assert b.state.webauthn is None and b.state.claim is not None and b.paths.test_claim_link.exists()
        assert any("passkeys are off" in r.message and why in r.message for r in caplog.records)
        origin = WebOrigin.parse(url)
        br = Browser(b, host=origin.host, origin=origin.origin)
        assert br.get("/setup").status_code == 200
        assert br.get("/api/auth/state").json() == {
            "hosted": True,
            "claimed": False,
            "passkeys": False,
            "claim": True,
            "passkeys_work": False,
            "password": True,
            "sso": "coming soon",
        }
        assert br.post("/api/passkey/begin").json()["error"] == "no_passkeys"
        assert (
            br.post("/api/setup/begin", {"token": token_of(claim_link(b), origin.origin)}).json()["error"]
            == "no_passkeys"
        )
        exec_login_url = b.login_url()
        assert exec_login_url.startswith(origin.origin + "/login?t=")  # the way in
        r = br.get(exec_login_url.removeprefix(origin.origin))
        assert r.status_code == 303
        me = br.get("/api/me").json()
        assert me["hosted"] is False and me["passkeys"] == 0
        assert br.post("/api/passkeys/begin").json()["error"] == "no_passkeys"
    finally:
        stop(b)


def test_the_desktop_has_no_claim_flow(broker: InProcBroker, web: httpx.Client) -> None:
    assert broker.state.webauthn is None and broker.state.claim is None
    assert not broker.paths.test_claim_link.exists()
    c = httpx.Client(base_url=broker.base)
    assert c.get("/setup").status_code == 303
    assert c.get("/api/auth/state").json() == {
        "hosted": False,
        "claimed": False,
        "passkeys": False,
        "claim": False,
        "passkeys_work": False,
        "password": False,
        "sso": "coming soon",
    }
    h = broker.write_headers()
    assert c.post("/api/passkey/begin", json={}, headers=h).status_code == 403
    assert c.post("/api/passkey/finish", json={}, headers=h).status_code == 403
    assert c.post("/api/setup/begin", json={"token": "x" * 40}, headers=h).status_code == 403
    assert c.post("/api/setup/finish", json={}, headers=h).status_code == 403
    me = web.get("/api/me").json()
    assert me["hosted"] is False and me["passkeys"] == 0 and me["fresh"] is False
    assert web.post("/api/passkeys/begin", json={}, headers=h).json()["error"] == "no_passkeys"
    assert web.post("/api/passkeys", json={}, headers=h).json()["error"] == "no_passkeys"
    assert "switchboard login" in c.get("/").text and "/static/login.js" in c.get("/").text


# ------------------------------------------------------------- a real broker
def test_the_claim_link_is_on_stdout_and_nowhere_else(tmp_home: Path) -> None:
    """A real daemon behind a plain-http public URL that is a secure context (``sb.localhost``):
    the claim line goes to stdout once, the token is in no log file, and a restart of the
    unclaimed broker prints a fresh line and kills the old token."""
    b = SubprocBroker(tmp_home, args=["--public-url", "http://sb.localhost"]).start()
    try:
        link = b.paths.test_claim_link.read_text().strip()
        assert link.startswith("http://sb.localhost/setup#t=")
        tok = link.split("#t=", 1)[1]
        out = (tmp_home / "subproc.out").read_text(errors="replace")
        lines = [ln for ln in out.splitlines() if "isn't set up yet" in ln]
        assert lines == [
            f"switchboard isn't set up yet. Sign in at http://sb.localhost as admin with the one-time"
            f" password {tok} (it works once, for 60 min), then choose your own password or passkey."
            f" Or open {link}"
        ]
        r = httpx.post(
            f"http://127.0.0.1:{b.port}/api/setup/begin",
            json={"token": tok},
            headers={"Host": "sb.localhost", "Origin": "http://sb.localhost", "X-Switchboard": "1"},
        )
        assert r.status_code == 200
        assert "secure" not in [p.strip().lower() for p in r.headers["set-cookie"].split(";")]  # plain http
        b.kill()
        logs = [p.read_text(errors="replace") for p in b.paths.logs_dir.iterdir() if p.is_file()]
        assert logs and any("no admin yet: a one-time password is on stdout" in x for x in logs)
        for text in logs:
            assert tok not in text and tok.replace("-", "") not in text and "setup#t=" not in text
        b = SubprocBroker(tmp_home, args=["--public-url", "http://sb.localhost"]).start()
        link2 = b.paths.test_claim_link.read_text().strip()
        assert link2 != link
        out = (tmp_home / "subproc.out").read_text(errors="replace")
        assert len([ln for ln in out.splitlines() if "isn't set up yet" in ln]) == 2
        r = httpx.post(
            f"http://127.0.0.1:{b.port}/api/setup/begin",
            json={"token": tok},
            headers={"Host": "sb.localhost", "Origin": "http://sb.localhost", "X-Switchboard": "1"},
        )
        assert r.status_code == 403
    finally:
        b.kill()
