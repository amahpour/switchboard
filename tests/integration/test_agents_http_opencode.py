"""OpenCode V2 over HTTPS (#513, DESIGN.md §49.12), using requests recorded from 2.1.4."""

from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from conftest import InProcBroker
from test_agents_http import FIX, Codex, make_token, members, sse_result
from test_agents_http_wake import TOKEN_IN, parked, participant, until

from switchboard.adapters import adapter_for, build_adapters
from switchboard.broker import agent_tokens
from switchboard.config import Config
from switchboard.mcp.server import TOOL_NAMES

INIT = json.loads((FIX / "opencode_initialize.json").read_text())
CALLS = [
    json.loads((FIX / name).read_text())
    for name in ("opencode_tools_call.json", "opencode_codemode_tools_call.json")
]
SESSION_A = "ses_00000000000000000000000001"
SESSION_B = "ses_00000000000000000000000002"
STATUS_PATH = "/mcp/status"


class OpenCode(Codex):
    """One MCP connection serving several OpenCode sessions, with either recorded call shape."""

    def __init__(self, b: InProcBroker, token: str, call: dict[str, Any] = CALLS[0]) -> None:
        super().__init__(b, token, client="opencode")
        self.recorded_call = call

    def start(self) -> httpx.Response:
        r = self.c.post("/mcp", json=INIT["body"], headers=self.headers(INIT["headers"]))
        assert sse_result(r)["result"]["protocolVersion"] == "2025-11-25"
        self.sid = r.headers["mcp-session-id"]
        done = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        assert (
            self.c.post("/mcp", json=done, headers=self.headers(self.recorded_call["headers"])).status_code
            == 202
        )
        return r

    def raw(self, tool: str, thread: str | None, **arguments: Any) -> httpx.Response:
        body = copy.deepcopy(self.recorded_call["body"])
        body["id"] = next(self.ids)
        body["params"]["name"] = tool
        body["params"]["arguments"] = arguments
        meta = body["params"]["_meta"]
        if thread is None:
            meta.pop("ai.opencode/sessionID", None)
        else:
            meta["ai.opencode/sessionID"] = thread
        return self.c.post("/mcp", json=body, headers=self.headers(self.recorded_call["headers"]))


def report(b: InProcBroker, token: str, event: str, sid: str = SESSION_A, **fields: Any) -> httpx.Response:
    return httpx.post(
        f"{b.base}{STATUS_PATH}",
        headers={"authorization": f"Bearer {token}"},
        json={"event": event, "session_id": sid, **fields},
        timeout=10,
    )


def listen(b: InProcBroker, token: str, sid: str = SESSION_A, **headers: str) -> httpx.Response:
    return httpx.post(
        f"{b.base}/mcp/listen",
        headers={
            "authorization": f"Bearer {token}",
            "x-switchboard-session": sid,
            "x-switchboard-harness": "opencode",
            "x-switchboard-wait": "60",
            **headers,
        },
        timeout=70,
    )


@pytest.fixture
def build(broker: InProcBroker) -> InProcBroker:
    broker.call("room.create", {"name": "#build"})
    return broker


def joined(b: InProcBroker) -> tuple[OpenCode, str]:
    token = make_token(b)
    c = OpenCode(b, token)
    c.start()
    assert c.call("join", SESSION_A, room="#build", screen_name="opencode-1")["ok"]
    return c, token


@pytest.mark.parametrize("recorded", CALLS, ids=["native", "codemode"])
def test_two_sessions_join_say_read_and_wait(build: InProcBroker, recorded: dict[str, Any]) -> None:
    """Both recorded call shapes identify two members on one token, including a concurrent wait."""
    c = OpenCode(build, make_token(build), recorded)
    c.start()
    for sid, name in ((SESSION_A, "opencode-1"), (SESSION_B, "opencode-2")):
        assert c.call("join", sid, room="#build", screen_name=name)["ok"]
    assert members(build, "#build") == {
        "opencode-1": ("opencode", "laptop"),
        "opencode-2": ("opencode", "laptop"),
    }
    p = participant(build, "opencode-1")
    assert p.session_key == f"opencode@laptop:{SESSION_A}"
    assert build.on_loop(build.state.engine.adapter, p).tier(p)[0] == "opencode:https"
    assert c.call("say", SESSION_A, room="#build", text="hello from OpenCode")["ok"]
    assert "hello from OpenCode" in c.call("read", SESSION_B, room="#build")["text"]
    c.call("read", SESSION_A, room="#build")
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(c.call, "wait", SESSION_A, room="#build", timeout_s=30)
        build.call("human.say", {"room": "#build", "text": "@opencode-1 wait works"})
        got = pending.result(30)
    assert got["ok"] and "wait works" in got["text"]
    listing = json.loads((FIX / "opencode_tools_list.json").read_text())["body"]
    r = c.c.post("/mcp", json=listing, headers=c.headers(recorded["headers"]))
    assert sorted(t["name"] for t in sse_result(r)["result"]["tools"]) == sorted(TOOL_NAMES)


def test_missing_session_fails_clearly_and_revocation_closes_every_door(build: InProcBroker) -> None:
    """No fallback to an MCP connection or another session; revoked tokens reach no OpenCode route."""
    c, token = joined(build)
    got = c.call("join", None, room="#build", screen_name="opencode-2")
    assert not got["ok"] and "ai.opencode/sessionID" in got["error"]
    assert set(members(build, "#build")) == {"opencode-1"}
    row = build.on_loop(build.state.store.agent_token_by_hash, agent_tokens.token_hash(token))
    build.on_loop(agent_tokens.revoke, build.state, None, row.id, "alice")
    assert c.raw("who", SESSION_A, room="#build").status_code == 401
    assert report(build, token, "session.execution.started").status_code == 401
    assert listen(build, token).status_code == 401


def test_status_and_listener_share_token_origin_method_and_field_guards(build: InProcBroker) -> None:
    """The new POST-only status door accepts only its fixed fields/events, never cookies or browser
    requests."""
    c, token = joined(build)
    body = {"session_id": SESSION_A, "event": "session.execution.started"}
    url = f"{build.base}{STATUS_PATH}"
    assert httpx.post(url, json=body).status_code == 401
    assert httpx.post(url, json=body, headers={"authorization": "Bearer sbat_" + "x" * 43}).status_code == 401
    assert (
        httpx.post(
            url, json=body, headers={"authorization": f"Bearer {token}", "origin": build.origin}
        ).status_code
        == 403
    )
    assert httpx.get(url, headers={"authorization": f"Bearer {token}"}).status_code == 405
    for fields in (
        {"other": "secret"},
        {"event": "unknown"},
        {"session_id": ""},
        {"session_id": 3},
        {"session_id": "x" * 129},
        {"tool_response": {"secret": "data"}},
    ):
        assert (
            httpx.post(url, json={**body, **fields}, headers={"authorization": f"Bearer {token}"}).status_code
            == 400
        )
    assert (
        httpx.post(url, content="x" * 4097, headers={"authorization": f"Bearer {token}"}).status_code == 413
    )
    assert httpx.post(url, content="{", headers={"authorization": f"Bearer {token}"}).status_code == 400
    assert listen(build, token, **{"x-switchboard-harness": "unknown"}).status_code == 400
    assert participant(build, "opencode-1").status != "offline"


def test_idle_wake_is_the_brokers_envelope_with_peer_text_stubbed(build: InProcBroker) -> None:
    """The existing Stop park delivers once to OpenCode; peer text is a read() stub, then the turn
    confirms it."""
    c, token = joined(build)
    c.call("read", SESSION_A, room="#build")
    report(build, token, "session.execution.succeeded").raise_for_status()
    p = participant(build, "opencode-1")
    peer = OpenCode(build, token)
    peer.start()
    assert peer.call("join", SESSION_B, room="#build", screen_name="opencode-2")["ok"]
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(listen, build, token)
        until(lambda: parked(build, p.id))
        assert peer.call("say", SESSION_B, room="#build", text="@opencode-1 peer-canary")["ok"]
        r = pending.result(30)
    assert r.status_code == 200 and "read(" in r.text and "peer-canary" not in r.text, r.text
    bid = int(TOKEN_IN.search(r.text).group(1))
    assert report(build, token, "session.execution.started").json() == {}
    assert build.on_loop(build.state.store.get_batch, bid).state == "confirmed"
    assert participant(build, "opencode-1").status == "busy"
    for again in ("0", "1"):
        assert listen(build, token, **{"x-switchboard-again": again}).status_code == 409


@pytest.mark.parametrize("idle", ["succeeded", "failed", "interrupted"])
def test_approval_survives_status_and_first_and_later_listener_rounds(build: InProcBroker, idle: str) -> None:
    """A pending approval ends an open park and cannot be cleared by generic status or a listener's Stop."""
    _c, token = joined(build)
    report(build, token, "session.execution.succeeded")
    p = participant(build, "opencode-1")
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(listen, build, token)
        until(lambda: parked(build, p.id))
        assert report(build, token, "permission.asked").json() == {}
        assert pending.result(30).status_code == 409
    for event in (f"session.execution.{idle}", "session.execution.started", "tool.execute.after"):
        assert report(build, token, event).json() == {}
        assert participant(build, "opencode-1").status == "waiting-approval"
    for again in ("0", "1"):
        assert listen(build, token, **{"x-switchboard-again": again}).status_code == 409
        assert participant(build, "opencode-1").status == "waiting-approval"
    assert report(build, token, "permission.cleared").json() == {}
    assert participant(build, "opencode-1").status == "busy"
    assert report(build, token, f"session.execution.{idle}").json() == {}
    assert participant(build, "opencode-1").status == "idle"


@pytest.mark.parametrize("event", ["session.deleted", "location.shutdown"])
def test_end_is_offline_and_other_device_cannot_change_it(build: InProcBroker, event: str) -> None:
    """Offline reports keep membership for resume, and the same session ID on another device changes
    nothing."""
    _c, token = joined(build)
    other = make_token(build, "desktop")
    assert report(build, other, event).json() == {}
    assert participant(build, "opencode-1").status != "offline"
    assert report(build, token, event).json() == {}
    assert participant(build, "opencode-1").status == "offline"
    assert set(members(build, "#build")) == {"opencode-1"}


def test_adapter_is_listening_without_permission_mode_or_hook_context() -> None:
    """OpenCode has no approval mode and no mid-tool context path; its guidance promises only actual
    delivery paths."""
    adapter = adapter_for(build_adapters(Config()), "opencode")
    assert adapter.tier(None)[0] == "opencode:https"
    assert not adapter.reports_approval_mode
    assert adapter.session_key("laptop", SESSION_A) == f"opencode@laptop:{SESSION_A}"
    assert adapter.context_events(None) == frozenset()


@pytest.mark.parametrize("signal", ["listen", "idle_report"])
def test_a_retried_listener_does_not_confirm_a_wake_that_never_started_a_turn(
    build: InProcBroker, signal: str
) -> None:
    """Losing a 200 body before prompt admission must redeliver it.
    Another listen round or idle report is not evidence that the turn ran."""
    c, token = joined(build)
    c.call("read", SESSION_A, room="#build")
    report(build, token, "session.execution.succeeded")
    p = participant(build, "opencode-1")
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(listen, build, token)
        until(lambda: parked(build, p.id))
        build.call("human.say", {"room": "#build", "text": "@opencode-1 missed-body-canary"})
        wake = first.result(30)
        assert wake.status_code == 200
        bid = int(TOKEN_IN.search(wake.text).group(1))
        # Model the lost body: there is no execution.started or tool progress before reconnecting.
        if signal == "idle_report":
            report(build, token, "session.execution.succeeded")
        second = pool.submit(listen, build, token, **{"x-switchboard-again": "1"})
        try:
            until(lambda: build.on_loop(build.state.store.get_batch, bid).state != "offered")
            assert build.on_loop(build.state.store.get_batch, bid).state == "expired"
            retry = second.result(30)
            assert retry.status_code == 200 and "missed-body-canary" in retry.text
        finally:
            report(build, token, "session.execution.started")
            second.result(30)
