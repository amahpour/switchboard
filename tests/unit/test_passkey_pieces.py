"""The pieces around WebAuthn (DESIGN.md §31.3, §31.4): the claim token, the sealed ceremony
cookie, used challenges, where passkeys can work, names and the sign-count rule."""

from __future__ import annotations

import hashlib

import pytest

from conftest import FakeClock
from switchboard.broker.auth import WebOrigin
from switchboard.broker.passkeys import (
    CEREMONY_TTL_S,
    CLAIM_TTL_S,
    ClaimTokens,
    Sealer,
    UsedChallenges,
    clean_name,
    passkeys_unavailable,
    sign_count_ok,
)


# ------------------------------------------------------------ claim tokens
def test_a_claim_token_is_random_hashed_and_lives_an_hour() -> None:
    clock = FakeClock()
    ct = ClaimTokens(clock)
    assert not ct.active and not ct.check("anything") and ct.expires_in_s() == 0.0
    tok = ct.mint()
    assert len(tok) >= 40 and ct.active and ct.issued == 1
    assert ct._hash == hashlib.sha256(tok.encode()).digest() and tok not in repr(vars(ct))
    assert ct.check(tok) and not ct.check(tok + "x") and not ct.check(tok[:-1]) and not ct.check(None)
    assert not ct.check(123) and not ct.check("short")
    assert ct.expires_in_s() == CLAIM_TTL_S == 3600.0
    clock.advance(3599.0)
    assert ct.check(tok)
    clock.advance(1.0)
    assert not ct.active and not ct.check(tok)


def test_minting_again_kills_the_old_token_and_spending_kills_all() -> None:
    ct = ClaimTokens(FakeClock())
    a = ct.mint()
    b = ct.mint()
    assert not ct.check(a) and ct.check(b) and ct.issued == 2
    ct.bind("cid", {"x": 1})
    ct.spend()
    assert not ct.active and not ct.check(b) and ct.ceremony("cid") is None


def test_the_ceremony_binds_the_token_to_one_browser_for_five_minutes() -> None:
    clock = FakeClock()
    ct = ClaimTokens(clock)
    ct.mint()
    assert not ct.ceremony_busy(None) and ct.ceremony("a") is None
    ct.bind("a", {"fido": 1})
    assert ct.ceremony("a") == {"fido": 1} and ct.ceremony("b") is None and ct.ceremony(None) is None
    assert ct.ceremony_busy(None) and ct.ceremony_busy("b") and not ct.ceremony_busy("a")
    clock.advance(CEREMONY_TTL_S)
    assert ct.ceremony("a") is None and not ct.ceremony_busy("b")  # expired: another browser may start
    ct.bind("b", {"fido": 2})
    ct.drop_ceremony()
    assert ct.ceremony("b") is None and not ct.ceremony_busy("a")
    # a new token drops the ceremony too
    ct.bind("c", {"fido": 3})
    ct.mint()
    assert ct.ceremony("c") is None


# ---------------------------------------------------------------- sealer
def test_the_seal_holds_the_state_and_its_expiry() -> None:
    clock = FakeClock()
    s = Sealer(clock)
    sealed = s.seal({"challenge": "abc", "uv": "required"}, 300.0)
    assert "." in sealed and "abc" not in sealed.split(".")[1]
    assert s.unseal(sealed) == {"challenge": "abc", "uv": "required"}
    clock.advance(299.0)
    assert s.unseal(sealed) is not None
    clock.advance(1.0)
    assert s.unseal(sealed) is None


def test_a_tampered_or_foreign_seal_is_nothing() -> None:
    s = Sealer(FakeClock())
    sealed = s.seal({"challenge": "abc"}, 300.0)
    body, mac = sealed.split(".")
    assert s.unseal(body + "." + mac[:-2] + "AA") is None
    assert s.unseal(body[:-1] + "." + mac) is None
    assert s.unseal(Sealer(FakeClock()).seal({"challenge": "abc"}, 300.0)) is None  # another key
    for bad in (None, 5, "", "nodot", "a.b", "x" * 5000, body + ".", "." + mac):
        assert s.unseal(bad) is None
    # a seal over something that isn't a state object
    import base64
    import hashlib as h
    import hmac
    import json

    raw = json.dumps({"s": ["not", "a", "dict"], "exp": 1e12}).encode()
    m = hmac.new(s.key, raw, h.sha256).digest()
    forged = base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + base64.urlsafe_b64encode(m).decode().rstrip("=")
    assert s.unseal(forged) is None


def test_used_challenges_are_remembered_until_they_expire() -> None:
    clock = FakeClock()
    u = UsedChallenges(clock)
    assert u.add("c1") and not u.add("c1") and len(u) == 1
    clock.advance(CEREMONY_TTL_S - 1)
    assert not u.add("c1")
    clock.advance(1)
    assert u.add("c1") and len(u) == 1  # the old entry is gone; the same bytes as a new challenge


# ------------------------------------------------------------ availability
@pytest.mark.parametrize("url,why", [
    ("https://sb.example.com", None),
    ("https://sb.example.com:8443", None),
    ("http://sb.localhost:7419", None),
    ("http://localhost:7419", None),
    ("http://sb.test", "not a secure context"),
    ("http://127.0.0.1:7419", "not a secure context"),
    ("https://10.0.0.5", "names an IP address"),
])
def test_where_passkeys_can_work(url: str, why: str | None) -> None:
    got = passkeys_unavailable(WebOrigin.parse(url))
    if why is None:
        assert got is None
    else:
        assert got is not None and why in got


def test_the_desktop_has_no_passkeys() -> None:
    o = WebOrigin.local(7419)
    assert not o.public and o.secure_context()
    assert "no public URL" in (passkeys_unavailable(o) or "")
    assert WebOrigin.parse("https://sb.example.com:8443").hostname == "sb.example.com"
    assert WebOrigin.local(7419).hostname == "switchboard.localhost"


# ---------------------------------------------------------------- rules
def test_sign_count_rule() -> None:
    assert sign_count_ok(0, 0)  # an authenticator that never counts
    assert sign_count_ok(0, 1) and sign_count_ok(4, 5) and sign_count_ok(4, 100)
    assert not sign_count_ok(5, 5) and not sign_count_ok(5, 4) and not sign_count_ok(5, 0)


def test_passkey_names_are_one_printable_line() -> None:
    assert clean_name("MacBook Pro") == "MacBook Pro"
    assert clean_name("  two   words \n\t x") == "two words x"
    assert clean_name("a" * 100) == "a" * 40
    assert clean_name("\x00\x07") == "passkey" and clean_name(None) == "passkey" and clean_name(5) == "passkey"
    assert clean_name("", "backup") == "backup"
