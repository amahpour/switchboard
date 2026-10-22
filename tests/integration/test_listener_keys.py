"""Listener keys (#488, DESIGN.md §49.11): what Claude Code's helper scripts present when the agent signed
in through the browser. The key is made on the machine and only its sha256 travels, in a header of the
signed-in connection's requests; the key itself is a bearer for the listener and the session report of one
session and for nothing else, and it dies with its sign-in. Driven with what Claude Code 2.1.307 sends
(``tests/fixtures/mcp_http/``) plus the header the plugin's ``bin/headers`` adds."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from conftest import InProcBroker
from test_agents_http import make_token
from test_agents_http_claude import DISCOVER, HOOK, SESSION_1, SESSION_2, Claude
from test_agents_http_wake import (
    claude_hook,
    listen,
    parked,
    participant,
    session_hook,
    until,
)

from switchboard.broker import agent_http, agent_tokens, listener_keys

KEY = "sbl_" + "0123456789abcdef" * 4
KEY_2 = "sbl_" + "fedcba9876543210" * 4
HEADER = listener_keys.HEADER.decode()


class SignedIn(Claude):
    """A Claude Code session whose MCP connection is signed in and carries the helper's header, the sha256
    of its key, on every request (what ``headersHelper`` makes it do)."""

    def __init__(
        self, b: InProcBroker, token: str, session: str, key: str = KEY, header: str | None = None
    ) -> None:
        super().__init__(b, token, session)
        self.key = key
        self.header = header or listener_keys.key_hash(key)  # or the hash a real helper printed

    def post(self, body: dict[str, Any], recorded: dict[str, str], name: str | None = None) -> httpx.Response:
        h = {**recorded, "authorization": f"Bearer {self.token}", HEADER: self.header}
        if name is not None:
            h["mcp-name"] = name
        return self.c.post("/mcp", json=body, headers=h)


@pytest.fixture
def build(broker: InProcBroker) -> InProcBroker:
    broker.call("room.create", {"name": "#build"})
    return broker


def stored(b: InProcBroker, key: str = KEY) -> Any:
    """The key's row and its token, as the store has them, or None."""
    return b.on_loop(b.state.store.listener_key, listener_keys.key_hash(key))


def signed_in_member(b: InProcBroker, key: str = KEY) -> tuple[SignedIn, str]:
    """A signed-in Claude Code session that joined #build as claude-1 (its hooks and calls carry the header)."""
    token = make_token(b, "laptop")
    claude = SignedIn(b, token, SESSION_1, key)
    assert claude.call("join", room="#build", screen_name="claude-1")["ok"]
    claude.call("read", room="#build")
    return claude, token


# ------------------------------------------------------------------------------------- claiming
def test_a_signed_in_request_that_carries_a_key_hash_claims_the_key(build: InProcBroker) -> None:
    """The header on a request with a live agent token (the browser sign-in) is how the key gets to the
    server: it is recorded against that token, with no session yet, and only its hash. Fails if the claim
    isn't made, or the server keeps anything but the hash."""
    token = make_token(build, "laptop")
    assert SignedIn(build, token, SESSION_1).discover().status_code == 200
    row, tok = stored(build)
    assert (row.key_hash, row.session_id, tok.device) == (listener_keys.key_hash(KEY), None, "laptop")
    assert KEY not in json.dumps(
        build.on_loop(lambda: [dict(r) for r in build.state.store.con.execute("SELECT * FROM listener_keys")])
    )


def test_nobody_can_claim_a_key_without_a_live_token(build: InProcBroker) -> None:
    """There is no unauthenticated way to make the server accept a key: a request with no token, a wrong
    one, a revoked one, a token in the wrong place or a browser's Origin claims nothing, so nobody can claim
    a key ahead of the real connection. Fails if any of them leaves a row."""
    made = build.on_loop(agent_tokens.make, build.state, None, "laptop", "alice")
    token, hdr = made["token"], {listener_keys.HEADER.decode(): listener_keys.key_hash(KEY)}
    base = {**DISCOVER["headers"], **hdr}
    for extra in (
        {},
        {"authorization": "Bearer sbat_" + "x" * 43},
        {"authorization": f"Bearer {token}", "origin": build.origin},
        {"authorization": f"Bearer {KEY}"},  # a key is not a token
    ):
        r = httpx.post(f"{build.base}/mcp", json=DISCOVER["body"], headers={**base, **extra}, timeout=10)
        assert r.status_code in (401, 403), extra
    build.on_loop(agent_tokens.revoke, build.state, None, made["id"], "alice")
    r = httpx.post(
        f"{build.base}/mcp", json=DISCOVER["body"], headers={**base, "authorization": f"Bearer {token}"}
    )
    assert r.status_code == 401
    assert stored(build) is None


def test_a_hash_another_token_holds_is_never_taken_over(build: InProcBroker) -> None:
    """Whoever sees a hash (a proxy's log) and holds a token of their own can't move the key to it: the
    first claim stands, and the key still names the first token's device. Fails if a second token takes it."""
    first = make_token(build, "laptop")
    other = make_token(build, "desktop")
    assert SignedIn(build, first, SESSION_1).discover().status_code == 200
    assert SignedIn(build, other, SESSION_2).discover().status_code == 200
    _row, tok = stored(build)
    assert tok.device == "laptop"


@pytest.mark.parametrize("bad", ["", "abc", "A" * 64, "g" * 64, "0" * 63, "0" * 65, KEY])
def test_only_a_sha256_in_hex_is_taken_as_a_key_hash(build: InProcBroker, bad: str) -> None:
    """The header is a claim from outside: only 64 lowercase hex characters are taken, and the key itself
    (which the header must never carry) isn't a hash. Fails if anything else is written."""
    token = make_token(build, "laptop")
    r = httpx.post(
        f"{build.base}/mcp",
        json=DISCOVER["body"],
        headers={**DISCOVER["headers"], "authorization": f"Bearer {token}", HEADER: bad},
    )
    assert r.status_code == 200
    assert (
        build.on_loop(
            lambda: build.state.store.con.execute("SELECT COUNT(*) FROM listener_keys").fetchone()[0]
        )
        == 0
    )


def test_a_token_keeps_at_most_sixteen_keys_the_oldest_go_first(build: InProcBroker) -> None:
    """A connection makes a key each time it starts, so the server keeps the newest few: the seventeenth
    pushes out the one used longest ago. Fails if the table grows without end, or the newest goes."""
    token = make_token(build, "laptop")
    keys = ["sbl_" + f"{i:064x}" for i in range(listener_keys.MAX_PER_TOKEN + 1)]
    for k in keys:
        assert SignedIn(build, token, SESSION_1, k).discover().status_code == 200
    assert stored(build, keys[0]) is None
    assert all(stored(build, k) is not None for k in keys[1:])


# ---------------------------------------------------------------------------------------- scope
def test_the_key_wakes_its_session_through_the_listener(build: InProcBroker) -> None:
    """With a key in place of a token, the listener waits and is answered with the message that wakes the
    session, as with a token. Fails if the listener won't take a key."""
    claude, _token = signed_in_member(build)
    p = participant(build, "claude-1")
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(listen, build, KEY, SESSION_1)
        until(lambda: parked(build, p.id))
        build.call("human.say", {"room": "#build", "text": "@claude-1 time to review"})
        r = waiting.result(30)
    assert r.status_code == 200 and "time to review" in r.text, r.text


def test_the_key_reports_the_end_of_its_session(build: InProcBroker) -> None:
    """``/mcp/session`` takes the key's SessionEnd and SessionStart as it takes a token's: the member goes
    offline. Fails if the route won't take a key."""
    signed_in_member(build)
    assert session_hook(build, KEY, {"event": "SessionStart", "session_id": SESSION_1}).status_code == 200
    r = session_hook(build, KEY, {"event": "SessionEnd", "session_id": SESSION_1, "reason": "logout"})
    assert r.status_code == 200 and r.json() == {}, r.text
    assert participant(build, "claude-1").status == "offline"


def test_the_key_can_call_no_tool_and_reach_nothing_else(build: InProcBroker) -> None:
    """The key is for two routes. Every other door answers an agent token only: the tools (and the hook
    tool, which is how a call is made for a session), the command hooks of the other harnesses, the web.
    Its form can't be smuggled into the token's header either. Fails if any of them lets a key in."""
    signed_in_member(build)
    assert (
        session_hook(build, KEY, {"event": "SessionStart", "session_id": SESSION_1}).status_code == 200
    )  # pins it
    key = {"authorization": f"Bearer {KEY}"}
    r = httpx.post(f"{build.base}/mcp", json=DISCOVER["body"], headers={**DISCOVER["headers"], **key})
    assert r.status_code == 401
    hook = {**HOOK["headers"], **key, "mcp-name": "hook"}
    assert httpx.post(f"{build.base}/mcp", json=HOOK["body"], headers=hook).status_code == 401
    for path in (agent_http.PRETOOL_PATH, agent_http.EVENT_PATH):
        body = {"harness": "codex", "session_id": SESSION_1, "event": "PermissionRequest"}
        assert httpx.post(f"{build.base}{path}", json=body, headers=key).status_code == 401, path
    smuggled = {**DISCOVER["headers"], "x-switchboard-token": KEY}
    assert httpx.post(f"{build.base}/mcp", json=DISCOVER["body"], headers=smuggled).status_code == 401
    web = {"Origin": build.origin, "X-Switchboard": "1", **key}
    assert httpx.get(f"{build.base}/api/agent-tokens", headers=web).status_code in (401, 403)
    assert httpx.get(f"{build.base}/api/rooms", headers=web).status_code in (401, 403)


def test_a_key_is_for_one_session_and_a_signed_in_hook_moves_it(build: InProcBroker) -> None:
    """The key's first request pins it to its session (a resumed session starts its listener before any
    hook has run), and any other session is refused with 403 ``wrong_session`` on both routes. A hook of the
    signed-in connection that names another session (a /clear or a resume in the same Claude Code) moves the
    pin, and then the first one is refused. Fails if the key works for a session it isn't for, or can't
    follow its own session."""
    token = make_token(build, "laptop")
    assert SignedIn(build, token, SESSION_1).discover().status_code == 200
    assert stored(build)[0].session_id is None
    assert session_hook(build, KEY, {"event": "SessionStart", "session_id": SESSION_1}).status_code == 200
    assert stored(build)[0].session_id == SESSION_1
    r = session_hook(build, KEY, {"event": "SessionEnd", "session_id": SESSION_2})
    assert r.status_code == 403 and r.json()["error"] == "wrong_session", r.text
    r = listen(build, KEY, SESSION_2)
    assert r.status_code == 403 and r.json()["error"] == "wrong_session", r.text
    second = SignedIn(build, token, SESSION_2)  # the same connection, a new session (/clear)
    claude_hook(second, event="UserPromptSubmit")
    assert stored(build)[0].session_id == SESSION_2
    assert session_hook(build, KEY, {"event": "SessionEnd", "session_id": SESSION_2}).status_code == 200
    r = session_hook(build, KEY, {"event": "SessionEnd", "session_id": SESSION_1})
    assert r.status_code == 403, r.text


def test_a_hook_of_another_connection_does_not_move_a_key(build: InProcBroker) -> None:
    """Which session a key is for is said by the hooks of the connection that carries its hash, never by
    another connection's: a second Claude Code on the same device, with its own key, names its own session.
    Fails if one session's hook moves another's key."""
    token = make_token(build, "laptop")
    one, two = SignedIn(build, token, SESSION_1, KEY), SignedIn(build, token, SESSION_2, KEY_2)
    claude_hook(one, event="UserPromptSubmit")
    claude_hook(two, event="UserPromptSubmit")
    assert stored(build, KEY)[0].session_id == SESSION_1
    assert stored(build, KEY_2)[0].session_id == SESSION_2
    assert session_hook(build, KEY_2, {"event": "SessionEnd", "session_id": SESSION_1}).status_code == 403


def test_a_connection_without_the_header_names_no_session_for_a_key(build: InProcBroker) -> None:
    """A hook on a connection that carries no key hash (a pasted token's, from before the helper) moves
    nothing. Fails if a hook without the header changes a key."""
    token = make_token(build, "laptop")
    assert SignedIn(build, token, SESSION_1).discover().status_code == 200
    plain = Claude(build, token, SESSION_2)
    claude_hook(plain, event="UserPromptSubmit")
    assert stored(build)[0].session_id is None


# --------------------------------------------------------------------------------- the end of a key
def test_the_key_ends_with_its_sign_in(build: InProcBroker) -> None:
    """Revoking the token in Settings ends its keys with it: the next request answers 401 ``revoked``, which
    is what the listener tells the session; a key nobody claimed answers ``unknown_key``. Fails if a key
    outlives its sign-in."""
    made = build.on_loop(agent_tokens.make, build.state, None, "laptop", "alice")
    claude = SignedIn(build, made["token"], SESSION_1)
    assert claude.call("join", room="#build", screen_name="claude-1")["ok"]
    assert session_hook(build, KEY, {"event": "SessionStart", "session_id": SESSION_1}).status_code == 200
    build.on_loop(agent_tokens.revoke, build.state, None, made["id"], "alice")
    r = listen(build, KEY, SESSION_1)
    assert r.status_code == 401 and r.json()["error"] == "revoked", r.text
    r = session_hook(build, KEY, {"event": "SessionEnd", "session_id": SESSION_1})
    assert r.status_code == 401 and r.json()["error"] == "revoked", r.text
    r = listen(build, KEY_2, SESSION_1)
    assert r.status_code == 401 and r.json()["error"] == "unknown_key", r.text


def test_a_broker_restart_keeps_the_keys(build: InProcBroker) -> None:
    """An idle session's listener has nothing to claim its key again with, so a restart (an upgrade) must
    not forget it. Fails if a key made before a restart is unknown after it."""
    token = make_token(build, "laptop")
    assert SignedIn(build, token, SESSION_1).discover().status_code == 200
    build.restart()
    assert session_hook(build, KEY, {"event": "SessionStart", "session_id": SESSION_1}).status_code == 200


def test_the_listener_says_nothing_of_a_token_in_a_key_s_place(build: InProcBroker) -> None:
    """A token still works at the two routes as before, and a key-shaped value that is not a claimed key
    is ``unknown_key``, a token-shaped one that is not live is ``unauthorized``. Fails if the two kinds of
    credential are mixed up."""
    _claude, token = signed_in_member(build)
    assert listen(build, token, "00000000-0000-4000-8000-0000000000ff").status_code == 409  # a token, as ever
    r = listen(build, KEY_2, SESSION_1)
    assert r.status_code == 401 and r.json()["error"] == "unknown_key"
    r = listen(build, "sbat_" + "x" * 43, SESSION_1)
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    r = listen(build, "sbl_" + "0" * 63, SESSION_1)  # not a key at all
    assert r.status_code == 401


# ------------------------------------------------------------------- a pasted token, in a new place
def mcp_with(b: InProcBroker, **headers: str) -> httpx.Response:
    return httpx.post(
        f"{b.base}/mcp", json=DISCOVER["body"], headers={**DISCOVER["headers"], **headers}, timeout=10
    )


def test_a_pasted_token_may_come_in_the_plugins_header_as_well_as_authorization(build: InProcBroker) -> None:
    """Claude Code starts no browser sign-in for a server with an Authorization header, empty or not, so the
    plugin sends the token typed in its settings as ``X-Switchboard-Token``, and the server takes an agent
    token in either. An empty one (nothing typed) is nothing, a sign-in's Authorization wins over it, and a
    revoked token answers 401 with where to sign in. Fails if the plugin's header isn't taken, or is taken
    when it is empty, wrong or the key."""
    made = build.on_loop(agent_tokens.make, build.state, None, "laptop", "alice")
    token = made["token"]
    assert mcp_with(build, **{"x-switchboard-token": token}).status_code == 200
    assert mcp_with(build, authorization=f"Bearer {token}").status_code == 200  # as ever
    assert mcp_with(build, authorization=f"Bearer {token}", **{"x-switchboard-token": ""}).status_code == 200
    other = make_token(build, "desktop")
    both = mcp_with(build, authorization=f"Bearer {token}", **{"x-switchboard-token": other})
    assert both.status_code == 200
    assert mcp_with(build, **{"x-switchboard-token": ""}).status_code == 401
    assert mcp_with(build, **{"x-switchboard-token": "sbat_" + "x" * 43}).status_code == 401
    assert (
        mcp_with(build, **{"x-switchboard-token": f"Bearer {token}"}).status_code == 401
    )  # the bare token only
    build.on_loop(agent_tokens.revoke, build.state, None, made["id"], "alice")
    r = mcp_with(build, **{"x-switchboard-token": token})
    assert r.status_code == 401 and "resource_metadata" in r.headers["www-authenticate"]
    assert mcp_with(build, **{"x-switchboard-token": token, "origin": build.origin}).status_code in (401, 403)


def test_the_token_header_is_refused_to_a_browser(build: InProcBroker) -> None:
    """``/mcp`` is for agents: a request with an Origin is refused before any credential is read, in
    either header. Fails if the new header lets a browser page call the tools."""
    token = make_token(build, "laptop")
    r = mcp_with(build, **{"x-switchboard-token": token, "origin": "https://evil.example"})
    assert r.status_code == 403


def test_a_pasted_token_reaches_the_listener_and_the_session_route_as_before(build: InProcBroker) -> None:
    """The two routes take a token in either header too, and in ``Authorization`` as the plugin's scripts
    send it. Fails if a pasted-token setup stops working at them."""
    claude, token = signed_in_member(build)
    assert session_hook(build, token, {"event": "SessionStart", "session_id": SESSION_1}).status_code == 200
    r = httpx.post(
        f"{build.base}/mcp/session",
        headers={"x-switchboard-token": token, "content-type": "application/json"},
        content=json.dumps({"event": "SessionStart", "session_id": SESSION_1}),
    )
    assert r.status_code == 200
