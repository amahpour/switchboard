"""Agents over HTTPS (#382, DESIGN.md §49.2, §49.3): the broker's ``/mcp`` endpoint, driven with
what Codex 0.158.0 sends (``tests/fixtures/mcp_http/codex_*.json``, recorded in #380's spike): an
``initialize`` that opens an MCP session, then tool calls that name their thread in ``_meta``.
"""

from __future__ import annotations

import copy
import itertools
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import InProcBroker
from test_passkeys import HOST as HOSTED
from test_passkeys import hosted_broker, stop

from switchboard.broker import agent_tokens
from switchboard.mcp.server import TOOL_NAMES

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_http"
INIT = json.loads((FIX / "codex_initialize.json").read_text())
CALL = json.loads((FIX / "codex_tools_call.json").read_text())
EXIT_FIX = Path(__file__).resolve().parents[1] / "fixtures" / "payloads" / "codex"
THREAD_A = "00000000-0000-4000-8000-0000000000a1"
THREAD_B = "00000000-0000-4000-8000-0000000000a2"


def make_token(b: InProcBroker, device: str = "laptop", person: int | None = None) -> str:
    return b.on_loop(agent_tokens.make, b.state, person, device, "alice")["token"]


def sse_result(r: httpx.Response) -> dict[str, Any]:
    """The JSON-RPC message of a response: the body, or the data of its one SSE event."""
    if r.headers.get("content-type", "").startswith("application/json"):
        return r.json()
    data = [line[5:].strip() for line in r.text.splitlines() if line.startswith("data:")]
    assert data, r.text
    return json.loads(data[-1])


class Codex:
    """One Codex process's MCP client, as the fixtures recorded it: one session, many threads."""

    def __init__(self, b: InProcBroker, token: str | None, *, client: str = "codex-mcp-client") -> None:
        self.c = httpx.Client(base_url=b.base, timeout=30.0)
        self.token = token
        self.client = client
        self.sid: str | None = None
        self.ids = itertools.count(10)

    def headers(self, recorded: dict[str, str]) -> dict[str, str]:
        h = dict(recorded)
        if self.token is not None:
            h["authorization"] = f"Bearer {self.token}"
        if self.sid is not None:
            h["mcp-session-id"] = self.sid
        return h

    def start(self) -> httpx.Response:
        body = copy.deepcopy(INIT["body"])
        body["params"]["clientInfo"]["name"] = self.client
        r = self.c.post("/mcp", json=body, headers=self.headers(INIT["headers"]))
        if r.status_code == 200:
            self.sid = r.headers["mcp-session-id"]
            done = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            assert self.c.post("/mcp", json=done, headers=self.headers(CALL["headers"])).status_code == 202
        return r

    def raw(self, tool: str, thread: str | None, **arguments: Any) -> httpx.Response:
        body = copy.deepcopy(CALL["body"])
        body["id"] = next(self.ids)
        body["params"]["name"] = tool
        body["params"]["arguments"] = arguments
        meta = body["params"]["_meta"]
        if thread is None:
            del meta["threadId"]
        else:
            meta["threadId"] = meta["sessionId"] = thread
            meta["x-codex-turn-metadata"]["thread_id"] = thread
        return self.c.post("/mcp", json=body, headers=self.headers(CALL["headers"]))

    def call(self, tool: str, thread: str | None, **arguments: Any) -> dict[str, Any]:
        r = self.raw(tool, thread, **arguments)
        assert r.status_code == 200, (r.status_code, r.text)
        msg = sse_result(r)
        assert "result" in msg, msg
        return json.loads(msg["result"]["content"][0]["text"])

    def tools(self) -> list[str]:
        body = {"jsonrpc": "2.0", "id": next(self.ids), "method": "tools/list", "params": {}}
        r = self.c.post("/mcp", json=body, headers=self.headers(CALL["headers"]))
        return sorted(t["name"] for t in sse_result(r)["result"]["tools"])

    def delete(self, recorded: dict[str, Any]) -> httpx.Response:
        """Send the last request in a recorded graceful Codex exit, with this client's token."""
        last = [e for e in recorded["server_events"] if e.get("method") == "DELETE"]
        assert len(last) == 1 and last[0]["path"] == "/mcp"
        return self.c.delete("/mcp", headers=self.headers(last[0]["headers"]))


def members(b: InProcBroker, room: str) -> dict[str, tuple[str, str]]:
    st = b.state
    r = b.on_loop(st.store.get_room, room)
    return {
        m.name: (m.harness, b.on_loop(st.store.get_participant, m.participant_id).host)
        for m in b.on_loop(st.store.members, r.id)
    }


def status(b: InProcBroker, name: str) -> str:
    room = b.on_loop(b.state.store.get_room, "#build")
    member = next(m for m in b.on_loop(b.state.store.members, room.id) if m.name == name)
    return b.on_loop(b.state.store.get_participant, member.participant_id).status


@pytest.mark.parametrize("exit_name", ["quit", "ctrl_c"])
def test_recorded_codex_delete_ends_its_threads_but_keeps_memberships(
    build: InProcBroker, exit_name: str
) -> None:
    """The recorded /quit and Ctrl-C DELETE has no thread ID. The server must remember each
    thread from its tool calls, take both offline, and keep their memberships for resume. A
    SIGKILL trace has no DELETE, so its thread stays as it was until a later signal."""
    graceful = json.loads((EXIT_FIX / f"mcp_exit_{exit_name}.json").read_text())
    killed = json.loads((EXIT_FIX / "mcp_exit_sigkill.json").read_text())
    assert all(e.get("method") != "DELETE" for e in killed["server_events"])
    codex = Codex(build, make_token(build, "laptop"))
    assert codex.start().status_code == 200
    for thread, name in ((THREAD_A, "codex-1"), (THREAD_B, "codex-2")):
        assert codex.call("join", thread, room="#build", screen_name=name)["ok"]
        assert codex.call("hook", thread, event="Stop", session_id=thread) == {}
        assert status(build, name) == "idle"
    assert codex.delete(graceful).status_code == 200
    assert status(build, "codex-1") == status(build, "codex-2") == "offline"
    assert set(members(build, "#build")) == {"codex-1", "codex-2"}
    resumed = Codex(build, codex.token)
    assert resumed.start().status_code == 200
    assert resumed.call("hook", THREAD_A, event="UserPromptSubmit", session_id=THREAD_A) == {}
    assert status(build, "codex-1") == "busy"
    assert set(members(build, "#build")) == {"codex-1", "codex-2"}


def test_delete_requires_owner_and_does_not_end_a_thread_served_by_newer_session(
    build: InProcBroker,
) -> None:
    """A wrong token or unknown ID cannot end anyone. A resumed thread belongs to its newer
    MCP session, so a late DELETE of the old one takes only the still-old thread offline."""
    recorded = json.loads((EXIT_FIX / "mcp_exit_quit.json").read_text())
    token = make_token(build, "laptop")
    old = Codex(build, token)
    assert old.start().status_code == 200
    for thread, name in ((THREAD_A, "codex-1"), (THREAD_B, "codex-2")):
        assert old.call("join", thread, room="#build", screen_name=name)["ok"]
        assert old.call("hook", thread, event="Stop", session_id=thread) == {}
    other = Codex(build, make_token(build, "desk"))
    other.sid = old.sid
    assert other.delete(recorded).status_code == 404
    assert status(build, "codex-1") == status(build, "codex-2") == "idle"
    unknown = Codex(build, token)
    unknown.sid = "00000000-0000-4000-8000-000000000099"
    assert unknown.delete(recorded).status_code == 404
    assert status(build, "codex-1") == status(build, "codex-2") == "idle"
    resumed = Codex(build, token)
    assert resumed.start().status_code == 200
    assert resumed.call("who", THREAD_A, room="#build")["ok"]
    assert old.call("who", THREAD_A, room="#build")["ok"]  # a late old call cannot reclaim it
    assert old.delete(recorded).status_code == 200
    assert status(build, "codex-1") == "idle"
    assert status(build, "codex-2") == "offline"
    assert set(members(build, "#build")) == {"codex-1", "codex-2"}


@pytest.fixture
def hosted() -> Iterator[InProcBroker]:
    b = hosted_broker()
    try:
        yield b
    finally:
        stop(b)


@pytest.fixture
def build(broker: InProcBroker) -> InProcBroker:
    broker.call("room.create", {"name": "#build"})
    return broker


def test_two_codex_threads_join_talk_and_read_as_two_members(build: InProcBroker) -> None:
    """One Codex process, two threads: each is its own member on the token's device, says and
    reads in the room, and is told apart by the thread its calls name. Fails if calls over HTTPS
    don't reach the room, or the two threads collapse into one member."""
    codex = Codex(build, make_token(build, "laptop"))
    assert codex.start().status_code == 200
    a = codex.call("join", THREAD_A, room="#build", screen_name="codex-1")
    b = codex.call("join", THREAD_B, room="#build", screen_name="codex-2")
    assert a["ok"] and b["ok"] and a["screen_name"] == "codex-1" and b["screen_name"] == "codex-2"
    assert members(build, "#build") == {"codex-1": ("codex", "laptop"), "codex-2": ("codex", "laptop")}
    said = codex.call("say", THREAD_A, room="#build", text="hello from thread A")
    assert said["ok"], said
    got = codex.call("read", THREAD_B, room="#build")
    assert got["ok"] and "hello from thread A" in got["text"], got
    assert codex.call("pass", THREAD_B, room="#build")["ok"]
    assert codex.call("leave", THREAD_A, room="#build")["ok"]
    assert set(members(build, "#build")) == {"codex-2"}
    # the room's history shows who said it, from which device
    hist = build.call("room.history", {"room": "#build"})["messages"]
    line = next(m for m in hist if m["text"] == "hello from thread A")
    assert (line["from"], line["host"]) == ("codex-1", "laptop")


def test_a_new_mcp_session_keeps_the_memberships(build: InProcBroker) -> None:
    """Nothing about a membership lives in memory: a second MCP session (a restarted Codex, or
    any session after a broker restart) speaks for the same thread at once, with no join. Fails
    if the endpoint keeps credentials per MCP session."""
    token = make_token(build, "laptop")
    first = Codex(build, token)
    first.start()
    assert first.call("join", THREAD_A, room="#build", screen_name="codex-1")["ok"]
    again = Codex(build, token)
    again.start()
    assert again.sid != first.sid
    said = again.call("say", THREAD_A, room="#build", text="still me")
    assert said["ok"], said
    assert set(members(build, "#build")) == {"codex-1"}


def test_wait_returns_when_a_message_comes(build: InProcBroker) -> None:
    """A wait() over HTTPS is a long request that ends with the message. Fails if a call's wait
    isn't woken by a delivery (its sink keyed wrongly), or the endpoint holds other calls behind
    it."""
    codex = Codex(build, make_token(build, "laptop"))
    codex.start()
    assert codex.call("join", THREAD_A, room="#build", screen_name="codex-1")["ok"]
    assert codex.call("join", THREAD_B, room="#build", screen_name="codex-2")["ok"]
    codex.call("read", THREAD_A, room="#build")
    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(codex.call, "wait", THREAD_A, room="#build", timeout_s=30)
        # the other thread speaks while thread A's call is open: proof the endpoint isn't blocked
        build.call("human.say", {"room": "#build", "text": "@codex-1 your turn"})
        got = waiting.result(30)
    assert got["ok"] and "your turn" in got.get("text", ""), got


def test_every_tool_is_served(build: InProcBroker) -> None:
    """The agent's whole interface is the same over HTTPS as over stdio. Fails if a tool is
    missing from either."""
    codex = Codex(build, make_token(build, "laptop"))
    codex.start()
    assert codex.tools() == sorted(TOOL_NAMES)


def test_no_token_a_bad_one_or_a_browser_gets_nothing(build: InProcBroker) -> None:
    """/mcp answers only an agent token, and never a browser. Fails if a request with no token, a
    wrong one or an Origin gets past the door."""
    token = make_token(build, "laptop")
    for bad in (None, "sbat_" + "x" * 43, "Bearer", token[:-1]):
        r = Codex(build, bad).start()
        assert r.status_code == 401, (bad, r.text)
        assert r.headers["www-authenticate"].startswith("Bearer")
    c = httpx.Client(base_url=build.base, timeout=10.0)
    h = {**INIT["headers"], "authorization": f"Bearer {token}", "origin": build.origin}
    r = c.post("/mcp", json=INIT["body"], headers=h)
    assert r.status_code == 403 and "browser" in r.json()["message"]


def test_a_revoked_token_stops_at_once_and_its_agents_leave(build: InProcBroker) -> None:
    """Revoking a token is immediate: the next call gets 401, and its device's members leave
    every room. Fails if a revoked token still works, or its agents stay in the room."""
    token = make_token(build, "laptop")
    codex = Codex(build, token)
    codex.start()
    assert codex.call("join", THREAD_A, room="#build", screen_name="codex-1")["ok"]
    row = build.on_loop(build.state.store.agent_token_for_device, "laptop")
    out = build.on_loop(agent_tokens.revoke, build.state, None, row.id, "alice")
    assert out == {"ok": True, "device": "laptop", "ended": 1}
    assert members(build, "#build") == {}
    assert codex.raw("say", THREAD_A, room="#build", text="still here?").status_code == 401
    hist = build.call("room.history", {"room": "#build"})["messages"]
    assert any(m["text"] == "left (laptop token revoked)" for m in hist)


def test_an_mcp_session_is_its_own_tokens(build: InProcBroker) -> None:
    """A session opened with one token can't be used with another: the other device's agents
    can't ride it. Fails if a session id works with any live token."""
    mine = Codex(build, make_token(build, "laptop"))
    mine.start()
    theirs = Codex(build, make_token(build, "desk"))
    theirs.sid = mine.sid
    r = theirs.raw("join", THREAD_B, room="#build", screen_name="codex-9")
    assert r.status_code == 404
    assert members(build, "#build") == {}


def test_a_codex_call_without_its_thread_is_refused(build: InProcBroker) -> None:
    """Codex names its thread on every call; without one, nothing can say which session it is.
    Fails if such a call joins anyone."""
    codex = Codex(build, make_token(build, "laptop"))
    codex.start()
    got = codex.call("join", None, room="#build", screen_name="codex-1")
    assert not got["ok"] and got["code"] == "bad_request", got
    assert members(build, "#build") == {}


def test_a_client_other_than_codex_is_told_it_isnt_supported_yet(build: InProcBroker) -> None:
    """Only Codex names its session on every call for now; any other client's calls couldn't be
    told apart, so they are refused with a reason. Fails if one joins a room."""
    other = Codex(build, make_token(build, "laptop"), client="some-other-client")
    other.start()
    got = other.call("join", THREAD_A, room="#build", screen_name="other-1")
    assert not got["ok"] and got["code"] == "unsupported" and "Codex" in got["error"], got
    assert members(build, "#build") == {}


def test_the_token_notes_its_last_use(build: InProcBroker) -> None:
    """The token list shows when each was last used. Fails if a call leaves last use empty."""
    codex = Codex(build, make_token(build, "laptop"))
    codex.start()
    assert codex.call("join", THREAD_A, room="#build", screen_name="codex-1")["ok"]
    row = build.on_loop(build.state.store.agent_token_for_device, "laptop")
    assert row.last_used_at is not None


def test_a_call_can_name_only_its_own_sessions_membership(build: InProcBroker) -> None:
    """Over HTTPS a call names a membership (``m:<id>``) instead of presenting a credential, and
    AgentService takes it only from the session it belongs to. Fails if one thread can speak
    through another thread's membership, even on the same device."""
    from switchboard.broker.agent_http import HttpConn

    codex = Codex(build, make_token(build, "laptop"))
    codex.start()
    assert codex.call("join", THREAD_A, room="#build", screen_name="codex-1")["ok"]
    assert codex.call("join", THREAD_B, room="#build", screen_name="codex-2")["ok"]
    st = build.state
    room = build.on_loop(st.store.get_room, "#build")
    by_name = {m.name: m.membership_id for m in build.on_loop(st.store.members, room.id)}
    from switchboard.broker.agents import AgentConn

    conn = HttpConn(1 << 50, "laptop", AgentConn("codex", "laptop"))
    forged = {"cred": f"m:{by_name['codex-1']}", "thread_id": THREAD_B, "text": "as codex-1?"}
    with pytest.raises(Exception, match="not a member"):
        build.on_loop(st.agents.say, conn, forged)
    own = {"cred": f"m:{by_name['codex-2']}", "thread_id": THREAD_B, "text": "as myself"}
    assert build.on_loop(st.agents.say, conn, own)["posted_id"]


def test_a_tokens_agents_are_its_persons(hosted: InProcBroker) -> None:
    """A device's agents belong to the token's person (§44.3): a private room takes only its own
    people's agents. Fails if bob's agent can join the admin's private room, or the admin's
    can't."""
    from test_people import ADMIN_PW, join_as_bob, set_up

    admin = set_up(hosted)
    join_as_bob(hosted, admin)
    assert admin.post("/api/signin/check", {"password": ADMIN_PW}).status_code == 200
    assert admin.post("/api/rooms", {"name": "#secret", "private": True}).status_code == 200
    bob_id = hosted.on_loop(hosted.state.store.person_named, "bob").id
    bobs = Codex(hosted, make_token(hosted, "bob-box", bob_id))
    mine = Codex(hosted, make_token(hosted, "admin-box", None))
    for c in (bobs, mine):
        c.c = httpx.Client(base_url=f"http://127.0.0.1:{hosted.port}", timeout=30.0, headers={"host": HOSTED})
        assert c.start().status_code == 200, c.start().text
    got = bobs.call("join", THREAD_A, room="#secret", screen_name="codex-b")
    assert not got["ok"] and got["code"] == "not_found", got
    assert mine.call("join", THREAD_B, room="#secret", screen_name="codex-a")["ok"]


def test_removing_a_person_ends_their_agents_over_https(hosted: InProcBroker) -> None:
    """Removing someone revokes their tokens (§49.1), and their devices' agents leave every room
    at once, as on a revoke. Fails if a removed person's agent stays a member, unable to call
    and never leaving."""
    from test_people import ADMIN_PW, join_as_bob, set_up

    admin = set_up(hosted)
    join_as_bob(hosted, admin)
    assert admin.post("/api/signin/check", {"password": ADMIN_PW}).status_code == 200
    assert admin.post("/api/rooms", {"name": "#build"}).status_code == 200
    bob_id = hosted.on_loop(hosted.state.store.person_named, "bob").id
    bobs = Codex(hosted, make_token(hosted, "bob-box", bob_id))
    bobs.c = httpx.Client(base_url=f"http://127.0.0.1:{hosted.port}", timeout=30.0, headers={"host": HOSTED})
    bobs.start()
    assert bobs.call("join", THREAD_A, room="#build", screen_name="codex-b")["ok"]
    assert set(members(hosted, "#build")) == {"codex-b"}
    assert admin.post(f"/api/people/{bob_id}/remove").status_code == 200
    assert members(hosted, "#build") == {}
    assert bobs.raw("say", THREAD_A, room="#build", text="still here?").status_code == 401
