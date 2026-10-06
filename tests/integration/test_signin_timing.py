"""Every ``POST /api/signin/password`` attempt costs the same one scrypt check (issue #179,
DESIGN.md §32.4 and §39.3): before this fix, a wrong password for a real person with their own
password cost two (the real hash, then the dummy one, by falling through an ``elif``/``else``);
an empty name cost none (the old ``elif name:`` skipped the whole branch); and the admin with no
password set yet, or any guess while a claim link is open, also cost none, since
``verify_password`` returns at once, with no scrypt at all, when ``stored`` is ``None`` — which
is exactly what the owner's password hash is in both of those. A real attacker can't be handed
a stopwatch in a unit test, so this counts calls to ``verify_password`` instead (never
wall-clock time), and records what each call's ``stored`` argument was: ``None`` means an
instant return with no scrypt, whoever typed what. For every kind of account the route can see,
with a wrong and a right password each, there must be exactly one call, and its ``stored`` must
never be ``None`` — a real hash to check when there's an account that has one, the dummy hash
otherwise.
"""

from __future__ import annotations

from typing import Any

import pytest
from fakes.fake_authenticator import SoftAuthenticator
from test_passkeys import PUBLIC, RP_ID, Browser, claim_link, hosted_broker, stop, token_of
from test_people import BOB_PW, add, set_up, signin

from switchboard.broker import web as web_module


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str | None, bool]]:
    """Every call the route makes to ``verify_password``, as ``(stored, result)``.
    ``web_module.verify_password`` is the name the route itself calls (bound at import time into
    ``switchboard.broker.web``'s own namespace), so patching it there is what actually counts,
    not patching ``switchboard.broker.passwords.verify_password``."""
    calls: list[tuple[str | None, bool]] = []
    real = web_module.verify_password

    def counting(password: Any, stored: str | None) -> bool:
        ok = real(password, stored)
        calls.append((stored, ok))
        return ok

    monkeypatch.setattr(web_module, "verify_password", counting)
    return calls


def assert_one_real_check(calls: list[tuple[str | None, bool]], ok: bool) -> None:
    """Exactly one call, and it was a real scrypt check (never the instant ``None`` return)."""
    assert len(calls) == 1, calls
    stored, got_ok = calls[0]
    assert stored is not None, "verify_password was called with stored=None: no scrypt ran"
    assert got_ok is ok


def test_an_open_claim_costs_one_check_right_or_wrong(counted: list[tuple[str | None, bool]]) -> None:
    """Before a broker is claimed the owner has no password yet, so a wrong guess at the
    one-time password used to fall to ``verify_password(pw, None)`` (an instant return, no
    scrypt), and a right one returned before ``verify_password`` was called at all (0 calls)."""
    b = hosted_broker()
    try:
        tok = token_of(claim_link(b))
        del counted[:]
        assert signin(Browser(b), "admin", "WRONG-ONE0-TIME-PASS").status_code == 403
        assert_one_real_check(counted, ok=False)
        del counted[:]
        r = signin(Browser(b), "admin", tok)
        assert r.status_code == 200 and r.json()["next"] == "setup"
        assert_one_real_check(counted, ok=False)  # the claim token is checked separately, not by scrypt
    finally:
        stop(b)


def test_the_admin_with_no_password_costs_one_real_check(
    counted: list[tuple[str | None, bool]],
) -> None:
    """The admin took a passkey instead of a password at setup (``owner_password_hash() is
    None``): the old code's ``verify_password(pw, None)`` returned at once, with no scrypt at
    all, which is exactly the gap #179 called out."""
    b = hosted_broker()
    try:
        br = Browser(b)
        assert signin(br, "admin", token_of(claim_link(b))).json()["next"] == "setup"
        auth = SoftAuthenticator(RP_ID, PUBLIC)
        r = br.post("/api/setup/begin")
        assert r.status_code == 200, r.text
        r = br.post("/api/setup/finish", {"credential": auth.register(r.json()["options"]), "name": "k"})
        assert r.status_code == 200, r.text
        assert b.state.store.owner_password_hash() is None
        del counted[:]
        assert signin(Browser(b), "alice", "whatever it is").status_code == 403
        assert_one_real_check(counted, ok=False)
    finally:
        stop(b)


def test_a_one_time_password_costs_one_check_right_or_wrong(
    counted: list[tuple[str | None, bool]],
) -> None:
    b = hosted_broker()
    try:
        admin = set_up(b)
        one_time = add(admin, "bob")["password"]
        del counted[:]
        assert signin(Browser(b), "bob", "WRONG-ONE0-TIME-PASS").status_code == 403
        assert_one_real_check(counted, ok=False)
        del counted[:]
        r = signin(Browser(b), "bob", one_time)
        assert r.status_code == 200 and r.json()["next"] == "setup"
        assert_one_real_check(counted, ok=True)
    finally:
        stop(b)


def test_a_persons_own_password_costs_one_check_right_or_wrong(
    counted: list[tuple[str | None, bool]],
) -> None:
    """The bug #179 found: a wrong password for a real person with their own password used to
    cost two checks (the real hash, then the ``elif``'s ``else`` falling to the dummy one)."""
    b = hosted_broker()
    try:
        admin = set_up(b)
        one_time = add(admin, "bob")["password"]
        bob = Browser(b)
        assert signin(bob, "bob", one_time).json()["next"] == "setup"
        assert bob.post("/api/me/password", {"password": BOB_PW}).status_code == 200
        del counted[:]
        assert signin(Browser(b), "bob", BOB_PW + "x").status_code == 403
        assert_one_real_check(counted, ok=False)  # was 2 calls before #179's fix
        del counted[:]
        assert signin(Browser(b), "bob", BOB_PW).json()["next"] == "app"
        assert_one_real_check(counted, ok=True)
    finally:
        stop(b)


def test_a_passkey_only_person_costs_one_check(counted: list[tuple[str | None, bool]]) -> None:
    """``password_hash`` is ``None`` (they chose a passkey instead, §32.4): already one check
    from the dummy-hash fallback before #179, and still exactly one, now also true for every
    other kind of account."""
    b = hosted_broker()
    try:
        admin = set_up(b)
        one_time = add(admin, "bob")["password"]
        bob = Browser(b)
        assert signin(bob, "bob", one_time).json()["next"] == "setup"
        auth = SoftAuthenticator(RP_ID, PUBLIC)
        r = bob.post("/api/passkeys/begin")
        assert r.status_code == 200, r.text
        r = bob.post("/api/passkeys", {"credential": auth.register(r.json()["options"]), "name": "k"})
        assert r.status_code == 200, r.text
        assert b.state.store.person_named("bob").password_hash is None
        del counted[:]
        assert signin(Browser(b), "bob", "whatever it is").status_code == 403
        assert_one_real_check(counted, ok=False)
    finally:
        stop(b)


def test_an_unknown_name_costs_one_check(counted: list[tuple[str | None, bool]]) -> None:
    b = hosted_broker()
    try:
        set_up(b)
        del counted[:]
        assert signin(Browser(b), "nobody-here", "whatever it is").status_code == 403
        assert_one_real_check(counted, ok=False)
    finally:
        stop(b)


def test_an_empty_name_costs_one_check(counted: list[tuple[str | None, bool]]) -> None:
    """The old code's ``elif name:`` skipped the whole branch, and so the dummy check, for an
    empty ``name`` — the one case that cost zero calls whatever the password."""
    b = hosted_broker()
    try:
        set_up(b)
        del counted[:]
        assert signin(Browser(b), "", "whatever it is").status_code == 403
        assert_one_real_check(counted, ok=False)
    finally:
        stop(b)
