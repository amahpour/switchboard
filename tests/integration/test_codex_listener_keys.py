"""Codex's locally made key claimed through browser sign-in (DESIGN.md §49.11, §54, #637)."""

from __future__ import annotations

import hashlib
import os

import httpx
from conftest import InProcBroker
from test_agent_plugins import impl  # noqa: F401
from test_agents_http import THREAD_A, THREAD_B, Codex
from test_agents_http_claude import Claude
from test_meeting_codex_host import machine, run_codex_headers  # noqa: F401
from test_meeting_host import Machine, stub  # noqa: F401

from switchboard.broker import agent_tokens, listener_keys


class SignedInCodex(Codex):
    def __init__(self, b: InProcBroker, token: str, key: str) -> None:
        super().__init__(b, token)
        self.key = key

    def headers(self, recorded: dict[str, str]) -> dict[str, str]:
        return {**super().headers(recorded), "X-Switchboard-Listener": listener_keys.key_hash(self.key)}


def test_browser_signed_in_codex_key_hosts_only_for_its_first_thread_and_dies_with_signin(
    broker: InProcBroker,
    machine: Machine,  # noqa: F811
) -> None:
    """No token enters a shell: an authenticated MCP call binds the helper's hash. The key cannot name
    another thread or a Claude session with the same id, call a tool, listen, report, push,
    or survive revoke."""
    b = broker
    made_key = run_codex_headers(machine)
    assert made_key.returncode == 0, made_key.stderr
    key_file = machine.data / f"codex-listener-key-{os.getpid()}"
    key = key_file.read_text().strip()
    for room in ("#build", "#other", "#claude"):
        b.call("room.create", {"name": room})
    made = b.on_loop(agent_tokens.make_signin, b.state, None, "laptop", "alice", "Codex")
    codex = SignedInCodex(b, made["token"], key)
    claude = Claude(b, made["token"], THREAD_A)
    try:
        assert codex.start().status_code == 200
        # Initialize identifies the key's harness before a thread has made any tool call. It
        # must already be refused at Claude's routes, including with the same raw session id.
        with httpx.Client(base_url=b.base, headers={"Authorization": f"Bearer {key}"}) as early:
            r = early.post("/mcp/session", json={"event": "SessionEnd", "session_id": THREAD_A})
            assert (r.status_code, r.json().get("error")) == (403, "wrong_session"), r.text
            r = early.post("/mcp/listen", headers={"X-Switchboard-Session": THREAD_A})
            assert (r.status_code, r.json().get("error")) == (403, "wrong_session"), r.text
            r = early.post("/mcp/meeting/host", json={"room": "#build"})
            assert (r.status_code, r.json().get("error")) == (409, "no_session"), r.text
        assert codex.call("join", THREAD_A, room="#build", screen_name="codex-1")["ok"]
        assert codex.call("join", THREAD_B, room="#other", screen_name="codex-2")["ok"]
        assert claude.call("join", room="#claude", screen_name="claude-1")["ok"]
        with httpx.Client(base_url=b.base, headers={"Authorization": f"Bearer {key}"}) as client:
            body = {"room": "#build", "key_hash": hashlib.sha256(b"call key").hexdigest(), "platform": "zoom"}
            for room in ("#other", "#claude"):
                r = client.post("/mcp/meeting/host", json={**body, "room": room})
                assert (r.status_code, r.json()["error"]) == (403, "not_member"), r.text
            r = client.post("/mcp/meeting/host", json={**body, "session": THREAD_B})
            assert (r.status_code, r.json()["error"]) == (403, "wrong_session"), r.text
            for path in (
                "/mcp",
                "/mcp/pretool",
                "/mcp/event",
                "/mcp/status",
                "/mcp/meeting/push",
                "/mcp/meeting/speech",
                "/mcp/meeting/leave",
            ):
                assert client.post(path, json={}).status_code == 401, path
            r = client.post("/mcp/session", json={"event": "SessionEnd", "session_id": THREAD_A})
            assert (r.status_code, r.json()["error"]) == (403, "wrong_session")
            r = client.post(
                "/mcp/listen", headers={"X-Switchboard-Session": THREAD_A, "X-Switchboard-Wait": "0"}
            )
            assert (r.status_code, r.json()["error"]) == (403, "wrong_session")
            result = machine.run(
                ["start", "--server", b.base, "--data", str(machine.data), "--no-join", "#build"],
                stdin="",
                env=machine.env(SWITCHBOARD_AGENT_TOKEN="", CLAUDE_PID="", CODEX_THREAD_ID=THREAD_A),
            )
            assert result.returncode == 0, result.stderr
            assert key not in result.stdout + result.stderr + machine.everything_a_process_was_given()
            assert machine.compose("up")
            assert machine.run(["stop", "--data", str(machine.data)], stdin="").returncode == 0
            b.on_loop(agent_tokens.revoke, b.state, None, made["id"], "alice")
            r = client.post("/mcp/meeting/host", json=body)
            assert (r.status_code, r.json()["error"]) == (401, "revoked")
    finally:
        codex.c.close()
        claude.c.close()
