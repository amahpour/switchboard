"""Browser sign-in for agents (#386, DESIGN.md §49.9): an agent registers, sends its person's browser to
the consent page, and swaps the code for an agent token. Driven the way Claude Code 2.1.305 and Codex
0.158.0 drive it (a spike against a logging server, 2026-10-21): the discovery documents, a dynamic client
registration with a loopback redirect, PKCE S256, ``state`` and ``resource``, a form-encoded token request.

Most of what is checked here is what must not work: a code that is wrong, late, used twice, or made for
another client; a redirect that was not registered; anything that is not a person's own page approving;
a browser calling the two routes that are for programs."""

from __future__ import annotations

import json
import secrets
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import FakeClock, InProcBroker
from test_agents_http import Codex
from test_passkeys import HOST, PUBLIC, hosted_broker, stop
from test_people import ADMIN_PW, BOB_PW, join_as_bob, set_up

from switchboard.broker import agent_tokens, oauth

REDIRECT = "http://localhost:41234/callback"
CODEX_REDIRECT = "http://127.0.0.1:32989/callback/vnpyx6uWdngt"
DEVICE = "work-laptop"


class Agent:
    """A coding agent's side of the sign-in, with the requests Claude Code and Codex make."""

    def __init__(
        self, c: httpx.Client, base: str, redirect: str = REDIRECT, name: str = "Claude Code (switchboard)"
    ):
        self.c, self.base, self.redirect, self.name = c, base, redirect, name
        self.client_id: str | None = None
        self.verifier = secrets.token_urlsafe(32)  # 43 characters
        self.challenge = oauth.s256(self.verifier)
        self.state = secrets.token_urlsafe(16)

    def register(self, **over: Any) -> httpx.Response:
        body = {
            "client_name": self.name,
            "redirect_uris": [self.redirect],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "application_type": "native",
        }
        body.update(over)
        r = self.c.post("/oauth/register", json={k: v for k, v in body.items() if v is not None})
        if r.status_code == 201:
            self.client_id = r.json()["client_id"]
        return r

    def query(self, **over: str | None) -> str:
        q: dict[str, str | None] = {
            "response_type": "code",
            "client_id": self.client_id,
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
            "redirect_uri": self.redirect,
            "state": self.state,
            "resource": self.base + "/mcp",
        }
        q.update(over)
        return urllib.parse.urlencode({k: v for k, v in q.items() if v is not None})

    def exchange(self, code: str, **over: str | None) -> httpx.Response:
        form: dict[str, str | None] = {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": self.verifier,
            "redirect_uri": self.redirect,
            "resource": self.base + "/mcp",
            "client_id": self.client_id,
        }
        form.update(over)
        return self.c.post("/oauth/token", data={k: v for k, v in form.items() if v is not None})


def code_of(redirect: str, redirect_uri: str = REDIRECT, state: str | None = None) -> str:
    """The code in the address the browser is sent to; the address is the agent's own, with its state."""
    assert redirect.startswith(redirect_uri + "?"), redirect
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(redirect).query)
    if state is not None:
        assert q["state"] == [state]
    return q["code"][0]


def approve(
    broker: InProcBroker, web: httpx.Client, agent: Agent, device: str = DEVICE, **over: str | None
) -> httpx.Response:
    return web.post(
        "/api/oauth/approve",
        json={"query": agent.query(**over), "device": device},
        headers=broker.write_headers(),
    )


def sign_in(broker: InProcBroker, web: httpx.Client, agent: Agent, device: str = DEVICE) -> str:
    """The whole flow for a person already signed in: register, approve, exchange. The access token."""
    assert agent.register().status_code == 201
    r = approve(broker, web, agent, device)
    assert r.status_code == 200, r.text
    code = code_of(r.json()["redirect"], agent.redirect, agent.state)
    t = agent.exchange(code)
    assert t.status_code == 200, t.text
    return str(t.json()["access_token"])


@pytest.fixture
def agent(broker: InProcBroker) -> Iterator[Agent]:
    c = httpx.Client(base_url=broker.base, timeout=10.0)
    try:
        yield Agent(c, broker.base)
    finally:
        c.close()


# ------------------------------------------------------------------ discovery
def test_the_discovery_documents_name_this_server_and_the_one_way_in(
    broker: InProcBroker, agent: Agent
) -> None:
    """RFC 9728 and RFC 8414 as Claude Code and Codex read them: the resource is this server's /mcp, the
    issuer its origin, and the only way in is a public client with PKCE S256 and one grant. A 401 from
    /mcp points at the first document. Fails if a client could pick a weaker method, a secret or a second
    grant, or if the 401 doesn't say where to start."""
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        prm = agent.c.get(path)
        assert prm.status_code == 200 and prm.headers["cache-control"] == "no-store"
        assert prm.json() == {
            "resource": broker.base + "/mcp",
            "authorization_servers": [broker.base],
            "bearer_methods_supported": ["header"],
            "resource_name": "switchboard",
        }
    doc = agent.c.get("/.well-known/oauth-authorization-server").json()
    assert doc["issuer"] == broker.base
    assert doc["authorization_endpoint"] == broker.base + "/oauth/authorize"
    assert doc["token_endpoint"] == broker.base + "/oauth/token"
    assert doc["registration_endpoint"] == broker.base + "/oauth/register"
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert doc["token_endpoint_auth_methods_supported"] == ["none"]
    assert doc["grant_types_supported"] == ["authorization_code"]
    assert doc["response_types_supported"] == ["code"]
    r = agent.c.get("/mcp")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == (
        f'Bearer realm="switchboard", resource_metadata="{broker.base}/.well-known/oauth-protected-resource"'
    )


# ------------------------------------------------------------------ the whole flow
def test_an_agent_signs_in_through_the_browser_and_the_token_is_an_agent_token(
    broker: InProcBroker, agent: Agent
) -> None:
    """The flow end to end for a person signed in: register, the consent page's checks, approve, exchange.
    The access token is an ordinary agent token: it works at /mcp, is listed in Settings with the agent's
    name and how it was made, is revoked by the same button, and then stops. Fails if the token is not an
    ``sbat_`` token, comes with an expiry or a refresh token, is stored in the clear, differs between the
    event log and the list, or still works after Revoke."""
    web = broker.web_client()
    assert agent.register().status_code == 201
    q = agent.query()
    page = web.get("/oauth/authorize?" + q)
    assert page.status_code == 200 and "/static/oauth.js" in page.text
    assert (
        agent.client_id is not None and agent.client_id not in page.text
    )  # nothing of the request is reflected
    view = web.get("/api/oauth/consent?" + q)
    assert view.status_code == 200, view.text
    assert view.json() == {
        "client_name": "Claude Code (switchboard)",
        "redirect_to": "localhost:41234",
        "person": "alice",
        "email": None,
        "hosted": False,
        "fresh": True,
        "password": False,
        "passkeys": 0,
        "device_rule": agent_tokens.DEVICE_RULE,
    }

    r = approve(broker, web, agent)
    assert r.status_code == 200, r.text
    code = code_of(r.json()["redirect"], REDIRECT, agent.state)
    assert not web.get("/api/agent-tokens").json()["tokens"]  # approving makes nothing yet
    t = agent.exchange(code)
    assert t.status_code == 200, t.text
    assert t.headers["cache-control"] == "no-store" and t.headers["pragma"] == "no-cache"
    body = t.json()
    assert body.keys() == {"access_token", "token_type"} and body["token_type"] == "Bearer"
    token = body["access_token"]
    assert agent_tokens.TOKEN_RE.fullmatch(token)

    listed = web.get("/api/agent-tokens").json()["tokens"]
    assert len(listed) == 1 and listed[0]["device"] == DEVICE
    assert listed[0]["via"] == "oauth" and listed[0]["client"] == "Claude Code (switchboard)"
    st = broker.state
    row = st.store.agent_token_by_hash(agent_tokens.token_hash(token))
    assert row is not None and row.person_id is None and row.via == "oauth"
    dump = "\n".join(st.store.con.iterdump())
    assert token not in dump and code not in dump and agent.verifier not in dump
    assert token not in web.get("/api/agent-tokens").text
    events = [
        json.loads(r[0]) for r in st.store.con.execute("SELECT data FROM events WHERE kind='agent_tokens'")
    ]
    assert [e["what"] for e in events] == ["approve", "add"]
    assert all(token not in json.dumps(e) and code not in json.dumps(e) for e in events)

    codex = Codex(broker, token)
    assert codex.start().status_code == 200 and "join" in codex.tools()
    revoked = web.post(f"/api/agent-tokens/{listed[0]['id']}/revoke", json={}, headers=broker.write_headers())
    assert revoked.status_code == 200 and revoked.json()["device"] == DEVICE
    again = agent.c.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"authorization": f"Bearer {token}", "accept": "application/json, text/event-stream"},
    )
    assert again.status_code == 401 and "resource_metadata" in again.headers["www-authenticate"]


def test_codex_registers_a_redirect_with_a_random_path_and_it_works(broker: InProcBroker) -> None:
    """Codex 0.158.0 registers ``http://127.0.0.1:<port>/callback/<random>`` and names itself "Codex"; the
    redirect is matched as a whole, path included. Fails if a path segment is dropped or a port is
    treated as a wildcard."""
    c = httpx.Client(base_url=broker.base, timeout=10.0)
    codex = Agent(c, broker.base, CODEX_REDIRECT, "Codex")
    web = broker.web_client()
    assert sign_in(broker, web, codex, "codex-box")
    row = web.get("/api/agent-tokens").json()["tokens"][0]
    assert row["client"] == "Codex" and row["device"] == "codex-box"
    other = Agent(c, broker.base, CODEX_REDIRECT, "Codex")
    other.register()
    assert web.get("/oauth/authorize?" + other.query(redirect_uri=CODEX_REDIRECT + "x")).status_code == 400
    assert (
        web.get(
            "/oauth/authorize?" + other.query(redirect_uri="http://127.0.0.1:32990/callback/vnpyx6uWdngt")
        ).status_code
        == 400
    )
    c.close()


def test_deny_tells_the_agent_and_makes_nothing(broker: InProcBroker, agent: Agent) -> None:
    """Deny sends the agent ``access_denied`` with its own state, so it stops waiting; no code, no token.
    Fails if Deny leaves the agent hanging or leaves anything behind."""
    web = broker.web_client()
    agent.register()
    r = web.post("/api/oauth/deny", json={"query": agent.query()}, headers=broker.write_headers())
    assert r.status_code == 200
    assert r.json()["redirect"] == f"{REDIRECT}?error=access_denied&state={agent.state}"
    assert not broker.state.oauth._live and web.get("/api/agent-tokens").json()["tokens"] == []


# ------------------------------------------------------------------ PKCE and the code
def test_pkce_is_required_and_the_verifier_must_match(broker: InProcBroker, agent: Agent) -> None:
    """No challenge, a ``plain`` one or a short one is an error the agent hears (redirected, with its
    state); a wrong verifier fails the exchange, and burns the code, so a guess is one try. Fails if a
    request without PKCE is taken, or a code survives a wrong verifier."""
    web = broker.web_client()
    agent.register()
    for over in ({"code_challenge": None}, {"code_challenge_method": "plain"}, {"code_challenge": "abc"}):
        r = web.get("/oauth/authorize?" + agent.query(**over))
        assert r.status_code == 303 and r.headers["location"].startswith(
            REDIRECT + "?error=invalid_request"
        ), over
        assert f"state={agent.state}" in r.headers["location"]
    r = approve(broker, web, agent)
    code = code_of(r.json()["redirect"])
    wrong = agent.exchange(code, code_verifier=secrets.token_urlsafe(32))
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant"
    right = agent.exchange(code)  # the code is gone: one try
    assert right.status_code == 400 and right.json()["error"] == "invalid_grant"
    assert web.get("/api/agent-tokens").json()["tokens"] == []
    short = agent.exchange(code, code_verifier="tooshort")
    assert short.status_code == 400 and short.json()["error"] == "invalid_request"


def test_a_code_is_for_its_client_and_its_address(broker: InProcBroker, agent: Agent) -> None:
    """A code stolen from one redirect is no use to another client or at another address (RFC 6749
    §4.1.3). Fails if the exchange doesn't compare them."""
    web = broker.web_client()
    agent.register()
    mine = agent.client_id
    for over in ({"client_id": "sbc_someoneelse1"}, {"redirect_uri": "http://localhost:41235/callback"}):
        code = code_of(approve(broker, web, agent).json()["redirect"])
        r = agent.exchange(code, **over)
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant", over
    assert agent.client_id == mine and web.get("/api/agent-tokens").json()["tokens"] == []
    code = code_of(approve(broker, web, agent).json()["redirect"])
    r = agent.exchange(code, resource="http://switchboard.localhost:1/other")
    assert r.status_code == 400 and r.json()["error"] == "invalid_target"


def test_a_code_lasts_a_minute_and_a_second_use_revokes_the_token(tmp_home: Path, clock: FakeClock) -> None:
    """Sixty seconds, once. A code used again (a stolen copy, or a client that retried) takes back the token
    it made, and ends that device's sessions (RFC 6749 §10.5). Fails if a code is good at 61 seconds, if
    the second use is merely refused while the first token keeps working, or if the revocation is not
    in the log."""
    broker = InProcBroker(tmp_home, clock=clock).start()
    try:
        c = httpx.Client(base_url=broker.base, timeout=10.0)
        agent = Agent(c, broker.base)
        web = broker.web_client()
        agent.register()
        late = code_of(approve(broker, web, agent).json()["redirect"])
        clock.advance(oauth.CODE_TTL_S + 1)
        r = agent.exchange(late)
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"

        code = code_of(approve(broker, web, agent).json()["redirect"])
        first = agent.exchange(code)
        assert first.status_code == 200
        token = first.json()["access_token"]
        assert Codex(broker, token).start().status_code == 200
        second = agent.exchange(code)
        assert second.status_code == 400 and second.json()["error"] == "invalid_grant"
        assert broker.state.store.agent_token_by_hash(agent_tokens.token_hash(token)) is None
        assert Codex(broker, token).start().status_code == 401
        what = [
            json.loads(r[0])
            for r in broker.state.store.con.execute(
                "SELECT data FROM events WHERE kind='agent_tokens' ORDER BY id"
            )
        ]
        assert what[-1]["what"] == "revoke" and what[-1]["why"] == "its code was used twice"
        assert token not in json.dumps(what)
        c.close()
    finally:
        broker.stop()


# ------------------------------------------------------------------ redirects and clients
def test_only_loopback_redirects_register_and_the_answers_say_what_was_stored(
    broker: InProcBroker, agent: Agent
) -> None:
    """Registration answers with what it kept (a name, the URIs, one grant, no secret) and refuses a
    remote or malformed redirect, a client secret, another response type. Fails if a non-loopback redirect
    could be registered, or if the answer holds a secret."""
    r = agent.register()
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    got = r.json()
    assert got["redirect_uris"] == [REDIRECT] and got["token_endpoint_auth_method"] == "none"
    assert got["grant_types"] == ["authorization_code"] and "client_secret" not in got
    assert got["client_id"].startswith("sbc_") and got["client_name"] == "Claude Code (switchboard)"
    for bad in (
        {"redirect_uris": ["https://evil.example/cb"]},
        {"redirect_uris": ["http://evil.example:80/cb"]},
        {"redirect_uris": [REDIRECT, "http://10.0.0.5:1/cb"]},
        {"redirect_uris": []},
        {"redirect_uris": REDIRECT},
        {"redirect_uris": [REDIRECT] * 6},
        {"redirect_uris": None},
    ):
        r = agent.register(**bad)
        assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri", bad
    for bad in (
        {"token_endpoint_auth_method": "client_secret_basic"},
        {"grant_types": ["implicit"]},
        {"grant_types": ["client_credentials"]},
        {"grant_types": ["refresh_token"]},
        {"response_types": ["token"]},
    ):
        r = agent.register(**bad)
        assert r.status_code == 400 and r.json()["error"] == "invalid_client_metadata", bad
    # other metadata is accepted and ignored, never kept
    r = agent.register(
        client_uri="https://evil.example", logo_uri="https://evil.example/x.png", scope="admin"
    )
    assert r.status_code == 201 and set(r.json()) == set(got)
    row = broker.state.store.oauth_client(agent.client_id or "")
    assert row is not None and "evil" not in json.dumps(row.__dict__)


def test_an_unregistered_redirect_or_an_unknown_client_is_shown_to_the_person_and_never_redirected(
    broker: InProcBroker, agent: Agent
) -> None:
    """The page the person sees for a request that can't be answered to the agent is a 400 with no
    ``Location``, and the consent API says why. Fails if a redirect to an address the client didn't
    register is ever made, or if a request for an unknown client reaches the sign-in."""
    web = broker.web_client()
    agent.register()
    bad = (
        agent.query(redirect_uri="http://localhost:41234/other"),
        agent.query(redirect_uri="http://evil.example:80/callback"),
        agent.query(redirect_uri=None),
        agent.query(client_id="sbc_unknown12345"),
        agent.query(client_id=None),
        agent.query() + "&state=twice",
    )
    for q in bad:
        r = web.get("/oauth/authorize?" + q)
        assert r.status_code == 400 and "location" not in r.headers and "set-cookie" not in r.headers, q
        api = web.get("/api/oauth/consent?" + q)
        assert api.status_code == 400 and api.json()["error"] == "invalid_request", q
        done = web.post(
            "/api/oauth/approve", json={"query": q, "device": "x1"}, headers=broker.write_headers()
        )
        assert done.status_code == 400, q
        denied = web.post("/api/oauth/deny", json={"query": q}, headers=broker.write_headers())
        assert denied.status_code == 400, q
    assert not broker.state.oauth._live


def test_other_mistakes_in_a_request_from_a_known_client_are_sent_back_to_it(
    broker: InProcBroker, agent: Agent
) -> None:
    """With a registered redirect, a request that is wrong in some other way is sent to that address with
    the error, the state and nothing else. Fails if the error goes anywhere else or loses its state."""
    web = broker.web_client()
    agent.register()
    for q, code in (
        (agent.query(response_type="token"), "unsupported_response_type"),
        (agent.query(resource="http://switchboard.localhost:1/mcp"), "invalid_target"),
    ):
        r = web.get("/oauth/authorize?" + q)
        assert r.status_code == 303 and r.headers["cache-control"] == "no-store"
        loc = urllib.parse.urlsplit(r.headers["location"])
        assert f"{loc.scheme}://{loc.netloc}{loc.path}" == REDIRECT
        got = urllib.parse.parse_qs(loc.query)
        assert got["error"] == [code] and got["state"] == [agent.state]


# ------------------------------------------------------------------ who may approve
def test_only_a_persons_page_can_approve(broker: InProcBroker, agent: Agent) -> None:
    """Approving is a POST with the person's cookie, the page's Origin and ``X-Switchboard``. A request with
    no session, with only an agent token, from another origin, without the header, as a simple form post or
    as a GET gets no code. Fails if any of them makes a code, which is how a room message, another agent or
    another site could approve."""
    web = broker.web_client()
    agent.register()
    body = {"query": agent.query(), "device": DEVICE}
    anon = httpx.Client(base_url=broker.base, timeout=10.0)
    hand_made = broker.on_loop(agent_tokens.make, broker.state, None, "other-box", "alice")["token"]
    good = broker.write_headers()
    attempts = [
        ("no session", anon.post("/api/oauth/approve", json=body, headers=good)),
        (
            "a token",
            anon.post(
                "/api/oauth/approve", json=body, headers={**good, "Authorization": f"Bearer {hand_made}"}
            ),
        ),
        (
            "another origin",
            web.post("/api/oauth/approve", json=body, headers={**good, "Origin": "https://evil.example"}),
        ),
        ("no origin", web.post("/api/oauth/approve", json=body, headers={"X-Switchboard": "1"})),
        ("no header", web.post("/api/oauth/approve", json=body, headers={"Origin": broker.origin})),
        (
            "a form post",
            web.post(
                "/api/oauth/approve",
                content=json.dumps(body),
                headers={"Origin": broker.origin, "Content-Type": "text/plain"},
            ),
        ),
        ("a GET", web.get("/api/oauth/approve?" + agent.query())),
    ]
    for what, r in attempts:
        assert r.status_code in (401, 403, 405), (what, r.status_code, r.text)
    assert attempts[0][1].status_code == 401 and attempts[1][1].status_code == 401
    assert not broker.state.oauth._live, "something made a code"
    assert web.post("/api/oauth/approve", json=body, headers=good).status_code == 200
    anon.close()


def test_approving_checks_the_request_and_the_device_again(broker: InProcBroker, agent: Agent) -> None:
    """Approve takes the whole query and checks it again, so a request the page never showed can't be
    approved, and the device is held to the rule Make a token holds it to: a host name, free, within the
    cap. Fails if a bad request or name makes a code."""
    web = broker.web_client()
    agent.register()
    h = broker.write_headers()
    for q in (
        agent.query(code_challenge="abc"),
        agent.query(client_id="sbc_unknown12345"),
        "",
        agent.query(redirect_uri=None),
    ):
        r = web.post("/api/oauth/approve", json={"query": q, "device": DEVICE}, headers=h)
        assert r.status_code == 400, q
    for device in ("", "1laptop", "lap top", "x" * 25, None, 7):
        r = approve(broker, web, agent, device=device)  # type: ignore[arg-type]
        assert r.status_code == 400 and "work-laptop" in r.json()["message"], device
    assert web.post("/api/oauth/approve", json={"device": DEVICE}, headers=h).status_code == 400
    assert not broker.state.oauth._live
    sign_in(broker, web, Agent(agent.c, broker.base), "taken-name")
    r = approve(broker, web, agent, device="taken-name")
    assert r.status_code == 409 and "revoke it first" in r.json()["message"]
    assert not broker.state.oauth._live


def test_a_name_taken_between_approval_and_exchange_is_refused_not_overwritten(
    broker: InProcBroker, agent: Agent
) -> None:
    """The device is checked again when the code is swapped, because a token made in between would
    otherwise be replaced or shared. Fails if the exchange makes a second live token for one device."""
    web = broker.web_client()
    agent.register()
    code = code_of(approve(broker, web, agent).json()["redirect"])
    web.post("/api/agent-tokens", json={"device": DEVICE}, headers=broker.write_headers())
    r = agent.exchange(code)
    assert (
        r.status_code == 400
        and r.json()["error"] == "invalid_grant"
        and "revoke it first" in r.json()["error_description"]
    )
    assert [t["via"] for t in web.get("/api/agent-tokens").json()["tokens"]] == ["token"]


def test_twenty_live_tokens_is_the_most_a_person_has_sign_ins_included(
    broker: InProcBroker, agent: Agent
) -> None:
    """Sign-ins count against the same cap as typed tokens, so neither can mint without end. Fails if the
    21st is approved."""
    web = broker.web_client()
    for i in range(agent_tokens.MAX_PER_PERSON):
        assert (
            web.post(
                "/api/agent-tokens", json={"device": f"box-{i}"}, headers=broker.write_headers()
            ).status_code
            == 200
        )
    agent.register()
    r = approve(broker, web, agent)
    assert r.status_code == 409 and "revoke one" in r.json()["message"]


# ------------------------------------------------------------------ the two routes for programs
def test_register_and_token_are_for_programs_a_browser_cannot_call_them(
    broker: InProcBroker, agent: Agent
) -> None:
    """Both refuse any request that has an ``Origin`` (a browser's script always does), so no page can
    register a client or ask for a token, even with the person's cookie. Wrong content types, oversized
    and repeated parameters and the other grants are refused. Fails if either answers a browser."""
    web = broker.web_client()
    h = {"Origin": broker.origin, "X-Switchboard": "1"}
    r = web.post("/oauth/register", json={"redirect_uris": [REDIRECT]}, headers=h)
    assert r.status_code == 403 and r.json()["error"] == "access_denied"
    r = web.post(
        "/oauth/register", json={"redirect_uris": [REDIRECT]}, headers={"Origin": "https://evil.example"}
    )
    assert r.status_code == 403
    r = web.post("/oauth/token", data={"grant_type": "authorization_code"}, headers=h)
    assert r.status_code == 403 and r.json()["error"] == "access_denied"
    assert broker.state.store.oauth_client_count() == 0

    c = agent.c
    assert c.post("/oauth/register", content="{}", headers={"Content-Type": "text/plain"}).status_code == 400
    assert (
        c.post("/oauth/register", content="[]", headers={"Content-Type": "application/json"}).status_code
        == 400
    )
    assert (
        c.post("/oauth/register", content="nope", headers={"Content-Type": "application/json"}).status_code
        == 400
    )
    big = json.dumps({"redirect_uris": [REDIRECT], "client_name": "x" * oauth.BODY_MAX})
    assert (
        c.post("/oauth/register", content=big, headers={"Content-Type": "application/json"}).status_code
        == 413
    )
    assert c.post("/oauth/token", json={"grant_type": "authorization_code"}).status_code == 400
    assert (
        c.post(
            "/oauth/token",
            content="grant_type=x&grant_type=y",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ).json()["error"]
        == "invalid_request"
    )
    assert (
        c.post(
            "/oauth/token", content=b"a=\xff", headers={"Content-Type": "application/x-www-form-urlencoded"}
        ).status_code
        == 400
    )
    assert (
        c.post(
            "/oauth/token",
            content="x=" + "y" * oauth.BODY_MAX,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ).status_code
        == 413
    )
    for form, error in (
        ({"grant_type": "refresh_token", "refresh_token": "x"}, "unsupported_grant_type"),
        ({"grant_type": "client_credentials"}, "unsupported_grant_type"),
        ({"grant_type": "authorization_code"}, "invalid_request"),
        ({"grant_type": "authorization_code", "code": "x", "code_verifier": "y"}, "invalid_request"),
    ):
        r = c.post("/oauth/token", data=form)
        assert r.status_code == 400 and r.json()["error"] == error, form
        assert r.headers["cache-control"] == "no-store" and r.headers["pragma"] == "no-cache"
    r = c.get("/oauth/token")
    assert r.status_code == 405


def test_registrations_are_limited_per_minute(tmp_home: Path, clock: FakeClock) -> None:
    """Anyone who can reach the server can register, so there is a limit (sixty a minute); past it the
    answer is 429 with an OAuth error, and it lets up once the minute has passed. Fails if the sixty-first
    is taken, or if the limit never lifts."""
    broker = InProcBroker(tmp_home, clock=clock).start()
    try:
        agent = Agent(httpx.Client(base_url=broker.base, timeout=10.0), broker.base)
        for _ in range(oauth.REGISTER_PER_MIN):
            assert agent.register().status_code == 201
        r = agent.register()
        assert r.status_code == 429 and r.json()["error"] == "temporarily_unavailable"
        clock.advance(61)
        assert agent.register().status_code == 201
        agent.c.close()
    finally:
        broker.stop()


# ------------------------------------------------------------------ signing in first
def test_a_signed_out_browser_comes_back_to_the_request_after_signing_in(
    broker: InProcBroker, agent: Agent
) -> None:
    """A browser with no session gets the page (which says to sign in) and a sealed cookie holding the
    request; signing in lands on ``/``, which sends it back to the same authorize address once. The cookie
    can't be forged into a redirect anywhere else. Fails if the way back is lost, is taken twice, or can
    be pointed at another address."""
    agent.register()
    q = agent.query()
    anon = httpx.Client(base_url=broker.base, timeout=10.0)
    page = anon.get("/oauth/authorize?" + q)
    assert page.status_code == 200
    cookie = page.headers["set-cookie"]
    assert (
        cookie.startswith("switchboard_oauth_next=") and "HttpOnly" in cookie and "SameSite=strict" in cookie
    )
    assert anon.get("/api/oauth/consent?" + q).status_code == 401
    assert (
        "set-cookie" not in anon.get("/").headers or "oauth_next" not in anon.get("/").headers["set-cookie"]
    )

    login = broker.login_url().removeprefix(broker.base)
    assert anon.get(login).status_code == 303
    back = anon.get("/")
    assert back.status_code == 303 and back.headers["location"] == "/oauth/authorize?" + q
    assert (
        "switchboard_oauth_next=" in back.headers["set-cookie"] and "Max-Age=0" in back.headers["set-cookie"]
    )
    assert anon.get("/").status_code == 200  # once
    assert anon.get("/api/oauth/consent?" + q).status_code == 200

    forged = httpx.Client(base_url=broker.base, timeout=10.0, cookies={"switchboard_oauth_next": "x.y"})
    forged.get(broker.login_url().removeprefix(broker.base))
    r = forged.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/"
    sealed = broker.state.sealer.seal({"kind": "somewhere_else", "q": "x"}, 60.0)
    other = httpx.Client(base_url=broker.base, timeout=10.0, cookies={"switchboard_oauth_next": sealed})
    other.get(broker.login_url().removeprefix(broker.base))
    r = other.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/"
    evil = broker.state.sealer.seal({"kind": "oauth_next", "q": "x\r\nSet-Cookie: a=b"}, 60.0)
    crlf = httpx.Client(base_url=broker.base, timeout=10.0, cookies={"switchboard_oauth_next": evil})
    crlf.get(broker.login_url().removeprefix(broker.base))
    r = crlf.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/"
    for c in (anon, forged, other, crlf):
        c.close()


def test_the_consent_page_is_a_static_file_under_the_strict_csp(broker: InProcBroker, agent: Agent) -> None:
    """The page that asks the person is the same file for everyone, with the strict policy (no inline
    script or style, no framing) and nothing of the request in it. Fails if the policy is loosened for it,
    if it can be framed, or if the query is echoed into the HTML."""
    agent.register()
    r = httpx.get(broker.base + "/oauth/authorize?" + agent.query(state="<script>alert(1)</script>"))
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "style-src 'self'" in csp and "unsafe-inline" not in csp
    assert "frame-ancestors 'none'" in csp and r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    assert "alert(1)" not in r.text and (agent.client_id or "x") not in r.text
    assert r.text == (Path(oauth.__file__).parents[1] / "web" / "static" / "oauth.html").read_text()


# ------------------------------------------------------------------ a hosted broker
@pytest.fixture
def hosted() -> Iterator[InProcBroker]:
    b = hosted_broker()
    try:
        yield b
    finally:
        stop(b)


def hosted_agent(b: InProcBroker, redirect: str = REDIRECT) -> Agent:
    c = httpx.Client(base_url=f"http://127.0.0.1:{b.port}", headers={"Host": HOST}, timeout=10.0)
    return Agent(c, PUBLIC, redirect)


def test_on_a_hosted_broker_approving_needs_a_fresh_check_like_making_a_token(hosted: InProcBroker) -> None:
    """A sign-in makes an agent token, so the person must have passed a password or passkey check in the
    last five minutes, as Make a token asks (§31.6); the consent page says so beforehand. Fails if a
    session that has not just confirmed it's them can approve, or if the check doesn't carry."""
    admin = set_up(hosted)
    agent = hosted_agent(hosted)
    assert agent.register().status_code == 201
    q = agent.query()
    assert agent.c.get("/.well-known/oauth-protected-resource").json()["resource"] == PUBLIC + "/mcp"
    hosted.state.passkey_checks.clear()  # the set-up's own check, gone
    view = admin.get("/api/oauth/consent?" + q).json()
    assert view["hosted"] is True and view["fresh"] is False and view["password"] is True
    r = admin.post("/api/oauth/approve", {"query": q, "device": DEVICE})
    assert r.status_code == 403 and r.json()["error"] == "reauth" and not hosted.state.oauth._live
    assert admin.post("/api/signin/check", {"password": "not it"}).status_code == 403
    assert admin.post("/api/signin/check", {"password": ADMIN_PW}).status_code == 200
    assert admin.get("/api/oauth/consent?" + q).json()["fresh"] is True
    r = admin.post("/api/oauth/approve", {"query": q, "device": DEVICE})
    assert r.status_code == 200, r.text
    code = code_of(r.json()["redirect"], REDIRECT, agent.state)
    t = agent.exchange(code)
    assert t.status_code == 200, t.text
    assert agent_tokens.TOKEN_RE.fullmatch(t.json()["access_token"])
    agent.c.close()


def test_a_sign_in_is_its_persons_and_goes_with_them(hosted: InProcBroker) -> None:
    """Bob's sign-in is bob's: a token for his device, listed only for him, the device's agents his, and
    the name is taken for the admin too. Removing bob revokes it, and a code made for him just before is
    no good. Fails if a sign-in is attributed to anyone else, or survives its person."""
    admin = set_up(hosted)
    bob = join_as_bob(hosted, admin)
    assert bob.post("/api/signin/check", {"password": BOB_PW}).status_code == 200
    agent = hosted_agent(hosted)
    agent.register()
    r = bob.post("/api/oauth/approve", {"query": agent.query(), "device": "bob-desk"})
    token = agent.exchange(code_of(r.json()["redirect"], REDIRECT)).json()["access_token"]
    st = hosted.state
    pid = st.store.person_named("bob").id
    row = st.store.agent_token_by_hash(agent_tokens.token_hash(token))
    assert row is not None and row.person_id == pid and row.via == "oauth"
    assert st.service.host_person("bob-desk") == pid
    assert [t["device"] for t in bob.get("/api/agent-tokens").json()["tokens"]] == ["bob-desk"]
    assert admin.get("/api/agent-tokens").json()["tokens"] == []
    assert admin.post("/api/signin/check", {"password": ADMIN_PW}).status_code == 200
    other = hosted_agent(hosted, "http://localhost:41300/callback")
    other.register()
    r = admin.post("/api/oauth/approve", {"query": other.query(), "device": "bob-desk"})
    assert r.status_code == 409 and r.json()["message"] == "bob-desk is taken: pick another name"

    pending = bob.post("/api/oauth/approve", {"query": other.query(), "device": "bob-two"})
    assert pending.status_code == 200
    assert admin.post(f"/api/people/{pid}/remove").status_code == 200
    assert st.store.agent_token_by_hash(agent_tokens.token_hash(token)) is None
    gone = other.exchange(code_of(pending.json()["redirect"], "http://localhost:41300/callback"))
    assert gone.status_code == 400 and gone.json()["error"] == "invalid_grant"
    assert bob.get("/api/oauth/consent?" + other.query()).status_code == 401
    agent.c.close()
    other.c.close()
