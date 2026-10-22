"""Browser sign-in for agents, the rules (#386, DESIGN.md §49.9): which redirect URIs can be registered, how
an authorize request is checked, a code's life, PKCE, and the cap on what anyone can register. No broker:
the routes and the flow around them are in ``tests/integration/test_oauth_signin.py``."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import FakeClock

from switchboard import db
from switchboard.broker import oauth
from switchboard.broker.auth import WebOrigin
from switchboard.store import Store

VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"  # RFC 7636 appendix B
CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
REDIRECT = "http://localhost:41234/callback"


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> Store:
    return Store(db.open_db(tmp_path / "t.db"), clock)


@pytest.fixture
def state(store: Store, clock: FakeClock) -> Any:
    """Just what the rules read of the broker: the store, where browsers reach it, the codes."""
    return SimpleNamespace(
        store=store, web_origin=WebOrigin.local(7777), clock=clock, oauth=oauth.OAuthState(clock)
    )


def registered(state: Any, *uris: str) -> str:
    return state.store.oauth_client_add(
        "sbc_test12345", "Codex", list(uris or (REDIRECT,)), 500, 1e9
    ).client_id


def query(**over: str | None) -> list[tuple[str, str]]:
    q: dict[str, str | None] = {
        "response_type": "code",
        "client_id": "sbc_test12345",
        "redirect_uri": REDIRECT,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": "abc",
        "resource": "http://switchboard.localhost:7777/mcp",
    }
    q.update(over)
    return [(k, v) for k, v in q.items() if v is not None]


def test_pkce_s256_matches_the_rfc_vector() -> None:
    """The challenge is the unpadded base64url of the verifier's SHA-256 (RFC 7636 appendix B). Fails if
    padding, the alphabet or the hash change, which would make every real client's exchange fail."""
    assert oauth.s256(VERIFIER) == CHALLENGE


@pytest.mark.parametrize(
    "uri",
    [
        "http://localhost:1/callback",
        "http://127.0.0.1:65535/callback/vnpyx6uWdngt",
        "http://[::1]:8080/cb",
    ],
)
def test_loopback_redirects_with_a_port_and_a_path_can_be_registered(uri: str) -> None:
    """What Claude Code (``http://localhost:<port>/callback``) and Codex (``http://127.0.0.1:<port>/callback/<id>``)
    register. Fails if either is refused."""
    assert oauth.redirect_problem(uri) is None


@pytest.mark.parametrize(
    "uri",
    [
        "https://localhost:1/cb",  # not http
        "http://evil.example:80/cb",
        "http://localhost.evil.example:1/cb",
        "http://127.0.0.1.evil.example:1/cb",
        "http://localhost@evil.example:1/cb",  # user info: the host is evil.example
        "http://evil.example@localhost:1/cb",
        "http://localhost/cb",  # no port
        "http://localhost:0/cb",
        "http://localhost:70000/cb",
        "http://localhost:1",  # no path
        "http://localhost:1/cb?x=1",
        "http://localhost:1/cb#f",
        "http://localhost:1/c b",
        "http://localhost:1/c\\b",
        "http://0.0.0.0:1/cb",  # all interfaces is not loopback
        "http://10.0.0.5:1/cb",
        "myapp://callback",
        "http://[::2]:1/cb",
        "",
        None,
        7,
        "http://localhost:1/" + "a" * 200,
    ],
)
def test_nothing_but_a_loopback_redirect_can_be_registered(uri: Any) -> None:
    """A link someone sends must not be able to deliver a code to another host, so the only redirects a
    client can register are the machine's own loopback. Fails if any other shape is accepted."""
    assert oauth.redirect_problem(uri) is not None


def test_a_clients_name_is_one_short_printable_line() -> None:
    """The name is the client's own claim, shown on the consent page and in Settings. Fails if a control
    character, a newline or a very long name gets through, or if a missing one has no fallback."""
    assert oauth.clean_name("Claude Code (switchboard)") == "Claude Code (switchboard)"
    assert oauth.clean_name("  a\n\tb\x07c  ") == "a b c"
    assert len(oauth.clean_name("x" * 500)) == oauth.NAME_MAX
    assert oauth.clean_name(None) == oauth.clean_name("") == oauth.clean_name(" \n ") == "an agent"


def test_a_good_request_is_accepted_and_its_state_echoed(state: Any) -> None:
    """The request both clients send (spike, 2026-10-21): the registered client and redirect, PKCE S256,
    ``state`` and ``resource``. Fails if it is refused."""
    registered(state)
    req = oauth.parse_authorize(state, query())
    assert req.client.client_name == "Codex" and req.redirect_uri == REDIRECT
    assert req.state == "abc" and req.code_challenge == CHALLENGE
    assert oauth.parse_authorize(state, query(state=None, resource=None)).state is None


@pytest.mark.parametrize(
    "over",
    [
        {"client_id": "sbc_nobody1234"},
        {"client_id": None},
        {"client_id": "x" * 200},
        {"redirect_uri": "http://localhost:41234/callback/"},
        {"redirect_uri": "http://localhost:41235/callback"},
        {"redirect_uri": "http://127.0.0.1:41234/callback"},
        {"redirect_uri": "HTTP://localhost:41234/callback"},
        {"redirect_uri": "http://localhost:41234/callback "},
        {"redirect_uri": None},
        {"redirect_uri": ""},
    ],
)
def test_an_unknown_client_or_an_unregistered_redirect_is_shown_never_redirected(
    state: Any, over: dict[str, str | None]
) -> None:
    """The redirect is compared as a whole string against what the client registered. Anything else is an
    error for the person, with no ``redirect_uri`` to send it to, because it is not known to be the
    client's. Fails if a near miss (another port, a trailing slash, another case) is accepted or if the
    error would be redirected."""
    registered(state)
    with pytest.raises(oauth.OAuthError) as e:
        oauth.parse_authorize(state, query(**over))
    assert e.value.redirect_uri is None and e.value.code == "invalid_request"


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"response_type": None}, "unsupported_response_type"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge_method": None}, "invalid_request"),
        ({"code_challenge": None}, "invalid_request"),
        ({"code_challenge": "short"}, "invalid_request"),
        ({"code_challenge": CHALLENGE + "="}, "invalid_request"),
        ({"state": "a b"}, "invalid_request"),
        ({"state": "x" * 513}, "invalid_request"),
        ({"resource": "http://switchboard.localhost:7777/other"}, "invalid_target"),
        ({"resource": "http://switchboard.localhost:7777/mcp/"}, "invalid_target"),
        ({"resource": "https://elsewhere.example/mcp"}, "invalid_target"),
    ],
)
def test_a_bad_request_from_a_known_client_is_an_error_the_client_is_told(
    state: Any, over: dict[str, str | None], code: str
) -> None:
    """With the client and redirect known good, everything else wrong is sent back to the agent, so it
    stops waiting: PKCE is required and only S256, and a ``resource`` that is not this server's /mcp
    is refused. Fails if the code is different or the error would not reach the client."""
    registered(state)
    with pytest.raises(oauth.OAuthError) as e:
        oauth.parse_authorize(state, query(**over))
    assert e.value.code == code and e.value.redirect_uri == REDIRECT


def test_a_repeated_or_oversized_parameter_is_refused(state: Any) -> None:
    """Two values for one parameter would let a proxy and the broker read different ones, and a long
    query is not a real request. Fails if either is taken."""
    registered(state)
    with pytest.raises(oauth.OAuthError, match="twice"):
        oauth.parse_authorize(state, [*query(), ("state", "other")])
    with pytest.raises(oauth.OAuthError, match="too long"):
        oauth.parse_authorize(state, query(state="x" * 512, extra="y" * oauth.QUERY_MAX))


def test_the_errors_sent_to_a_client_carry_its_state_and_nothing_else_of_ours() -> None:
    """The redirect back to the agent is its own address with ``error``, a description and its own
    ``state``. Fails if ``state`` is dropped (the client could not match the answer) or a second ``?``
    appears."""
    err = oauth.OAuthError("invalid_request", "bad thing", redirect_uri=REDIRECT, state="s1")
    url = oauth.error_redirect(err)
    assert url == REDIRECT + "?error=invalid_request&error_description=bad+thing&state=s1"
    assert oauth.redirect_with(REDIRECT, code="c", state=None) == REDIRECT + "?code=c"


def test_a_code_works_once_and_only_for_sixty_seconds(state: Any, clock: FakeClock) -> None:
    """A code is one try: taken, it is gone, and an old one is gone by itself. Fails if a code can be
    redeemed twice or after its minute."""
    registered(state)
    req = oauth.parse_authorize(state, query())
    assert oauth.CODE_TTL_S == 60.0
    codes = state.oauth
    code = codes.issue(req, None, "laptop", "alice")
    rec = codes.take(code)
    assert rec is not None and rec.device == "laptop" and rec.redirect_uri == REDIRECT
    assert codes.take(code) is None
    late = codes.issue(req, None, "laptop", "alice")
    clock.advance(60.1)
    assert codes.take(late) is None
    assert codes.take("not-a-code") is None


def test_a_code_is_kept_only_as_its_hash(state: Any) -> None:
    """The broker holds codes in memory as sha256, like every other secret. Fails if the code itself
    is a key anywhere in the state."""
    registered(state)
    code = state.oauth.issue(oauth.parse_authorize(state, query()), None, "laptop", "alice")
    assert code not in state.oauth._live and all(code not in repr(r) for r in state.oauth._live.values())
    assert len(code) >= 43


def test_a_spent_code_remembers_its_token_for_ten_minutes(state: Any, clock: FakeClock) -> None:
    """A second use of a spent code is how a stolen copy shows: the token it made is what gets revoked.
    Fails if the token is forgotten at once or never."""
    codes = state.oauth
    codes.spend("a-spent-code-0123456789abcdef", 7)
    assert codes.reused("a-spent-code-0123456789abcdef") == 7
    assert codes.reused("another-code-0123456789abcdef") is None
    clock.advance(oauth.SPENT_KEEP_S + 1)
    assert codes.reused("a-spent-code-0123456789abcdef") is None


def test_live_codes_are_capped(state: Any) -> None:
    """Approving is a person's click, but the table still has a bound. Fails if it grows past the cap or
    forgets the newest instead of the oldest."""
    registered(state)
    req = oauth.parse_authorize(state, query())
    issued = [state.oauth.issue(req, None, "laptop", "alice") for _ in range(oauth.CODES_MAX + 5)]
    assert len(state.oauth._live) == oauth.CODES_MAX
    assert state.oauth.take(issued[0]) is None and state.oauth.take(issued[-1]) is not None


def test_registrations_are_limited_per_minute(state: Any, clock: FakeClock) -> None:
    """Registering needs no sign-in, so anyone who can reach the server can try: sixty a minute, then
    nothing until the minute has passed. Fails if the limit is off, or if it never lets up."""
    assert all(state.oauth.allow_register() for _ in range(oauth.REGISTER_PER_MIN))
    assert not state.oauth.allow_register()
    clock.advance(30)
    assert not state.oauth.allow_register()
    clock.advance(31)
    assert state.oauth.allow_register()


def test_the_client_table_is_capped_and_old_clients_go(store: Store, clock: FakeClock) -> None:
    """Registered clients are only needed for the few minutes a sign-in takes, so the oldest go past a
    cap and any past a day. Fails if the table grows without end, drops the newest, or keeps a stale one."""
    for i in range(7):
        store.oauth_client_add(f"sbc_client{i:04d}", f"c{i}", [REDIRECT], 5, 3600.0)
        clock.advance(1)
    assert store.oauth_client_count() == 5
    assert store.oauth_client("sbc_client0000") is None and store.oauth_client("sbc_client0001") is None
    assert store.oauth_client("sbc_client0006") is not None
    clock.advance(3600)
    store.oauth_client_add("sbc_fresh0000", "fresh", [REDIRECT], 5, 3600.0)
    assert store.oauth_client_count() == 1
