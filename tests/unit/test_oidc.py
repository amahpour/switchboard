"""The OpenID Connect client behind Sign in with Google (#70, DESIGN.md §38): the start URL,
and every check on the way back. A fake issuer signs real RS256 tokens."""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse

import pytest
from fakes.fake_oidc import CLIENT_ID, CLIENT_SECRET, ISSUER, FakeIssuer, new_key

from switchboard.broker import oidc
from switchboard.broker.oidc import OidcClient, OidcConfig, OidcError

REDIRECT = "https://sb.example.com/auth/oidc/callback"
CFG = OidcConfig(issuer=ISSUER, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def world() -> tuple[OidcClient, FakeIssuer, Clock]:
    clock = Clock()
    issuer = FakeIssuer(now=clock)
    return OidcClient(CFG, fetch=issuer.fetch, now=clock), issuer, clock


def sign_in(
    client: OidcClient, issuer: FakeIssuer, email: str = "Bob@Example.com", binding: str = "b1"
) -> str:
    url, _ = client.start(REDIRECT, binding)
    state, code = issuer.authorize(url, email)
    return client.finish(state, code, REDIRECT, binding)


def test_a_sign_in_returns_the_verified_email_lowercased(world) -> None:
    client, issuer, _ = world
    assert sign_in(client, issuer) == "bob@example.com"


def test_the_start_url_carries_state_nonce_and_a_pkce_challenge(world) -> None:
    client, issuer, _ = world
    url, state = client.start(REDIRECT, "b1")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert url.startswith(ISSUER + "/o/oauth2/v2/auth?")
    assert q["state"] == state and len(state) >= 40 and len(q["nonce"]) >= 40
    assert q["code_challenge_method"] == "S256" and q["redirect_uri"] == REDIRECT
    assert q["scope"] == "openid email" and q["client_id"] == CLIENT_ID
    assert CLIENT_SECRET not in url  # the secret only ever goes to the token endpoint


def test_a_state_works_once_and_only_from_the_browser_that_started_it(world) -> None:
    client, issuer, _ = world
    url, _ = client.start(REDIRECT, "b1")
    state, code = issuer.authorize(url, "bob@example.com")
    with pytest.raises(OidcError) as e:
        client.finish(state, code, REDIRECT, "another-browser")
    assert e.value.code == "expired"
    with pytest.raises(OidcError):  # consumed by the failed try: no second chance
        client.finish(state, code, REDIRECT, "b1")
    with pytest.raises(OidcError) as e:
        client.finish("made-up", "code", REDIRECT, "b1")
    assert e.value.code == "expired"


def test_a_sign_in_left_for_ten_minutes_has_expired(world) -> None:
    client, issuer, clock = world
    url, _ = client.start(REDIRECT, "b1")
    state, code = issuer.authorize(url, "bob@example.com")
    clock.t += oidc.FLOW_TTL_S + 1
    with pytest.raises(OidcError) as e:
        client.finish(state, code, REDIRECT, "b1")
    assert e.value.code == "expired"


@pytest.mark.parametrize(
    "spoil,code",
    [
        ({"key": new_key()}, "token"),  # signed by a key the issuer doesn't publish
        ({"aud": "someone-else"}, "token"),
        ({"iss": "https://evil.example.com"}, "token"),
        ({"nonce": "another"}, "token"),
        ({"exp": 1}, "token"),
        ({"iat": 9_999_999_999}, "token"),
        ({"email_verified": False}, "unverified"),
        ({"email_verified": "true"}, "unverified"),  # a string isn't true
        ({"email": None}, "no_email"),
        ({"alg": "HS256"}, "token"),
    ],
)
def test_an_id_token_that_fails_any_check_is_refused(world, spoil, code) -> None:
    client, issuer, _ = world
    issuer.spoil = spoil
    with pytest.raises(OidcError) as e:
        sign_in(client, issuer)
    assert e.value.code == code


def test_an_unsigned_token_is_refused(world) -> None:
    client, _issuer, _ = world
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "kid": "k1"}).encode()).decode().rstrip("=")
    claims = base64.urlsafe_b64encode(json.dumps({"email": "bob@example.com"}).encode()).decode().rstrip("=")
    with pytest.raises(OidcError) as e:
        client.verify(header + "." + claims + ".", "n")
    assert e.value.code == "token"


def test_the_token_request_proves_pkce(world) -> None:
    """The verifier sent to the token endpoint hashes to the challenge in the start URL:
    the fake issuer refuses the code otherwise, so a stolen code alone is useless."""
    client, issuer, _ = world
    url, _ = client.start(REDIRECT, "b1")
    state, code = issuer.authorize(url, "bob@example.com")
    flow = client._flows[state]
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    digest = hashlib.sha256(flow.verifier.encode()).digest()
    assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == q["code_challenge"]
    assert client.finish(state, code, REDIRECT, "b1") == "bob@example.com"


def test_configuration_needs_both_the_client_id_and_its_secret(tmp_path) -> None:
    assert oidc.from_env({}) is None
    assert oidc.from_env({"SWITCHBOARD_OIDC_CLIENT_ID": "x"}) is None
    cfg = oidc.from_env({"SWITCHBOARD_OIDC_CLIENT_ID": "x", "SWITCHBOARD_OIDC_CLIENT_SECRET": "s3cr3t-value"})
    assert cfg is not None and cfg.issuer == ISSUER and cfg.provider == "google"
    assert "s3cr3t-value" not in repr(cfg)  # a log line or a traceback never shows it
    f = tmp_path / "secret"
    f.write_text("from-a-file\n")
    cfg = oidc.from_env({"SWITCHBOARD_OIDC_CLIENT_ID": "x", "SWITCHBOARD_OIDC_CLIENT_SECRET_FILE": str(f)})
    assert cfg is not None and cfg.client_secret == "from-a-file"
    with pytest.raises(ValueError, match="https"):
        oidc.from_env(
            {"SWITCHBOARD_OIDC_CLIENT_ID": "x", "SWITCHBOARD_OIDC_CLIENT_SECRET": "s",
             "SWITCHBOARD_OIDC_ISSUER": "http://idp.example.com"}
        )  # fmt: skip


def test_the_real_fetch_refuses_plain_http() -> None:
    with pytest.raises(OidcError) as e:
        oidc.http_fetch("GET", "http://accounts.google.com/x", None)
    assert e.value.code == "provider"
