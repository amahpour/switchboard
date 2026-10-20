"""Cursor over HTTPS (#394, DESIGN.md §49.3): a reported hook ID travels in the call input."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
from conftest import InProcBroker
from test_agents_http import Codex, make_token, sse_result

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "payloads" / "cursor"
SCRIPT = Path(__file__).resolve().parents[2] / "plugins" / "cursor" / "bin" / "hook"
START = json.loads((FIX / "https_sessionStart_1.json").read_text())
SID_A = START["session_id"]
SID_B = "00000000-0000-4000-8000-000000000099"


def test_cursor_input_hook_routes_two_conversations_and_rejects_unmatched_calls(broker: InProcBroker) -> None:
    """An input ID must match the same token's one-use hook report, so simultaneous conversations
    on one device cannot swap calls or claim each other's membership."""
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker)
    c = Codex(broker, token, client="Cursor")
    assert c.start().status_code == 200
    assert c.tools() == sorted(
        f"switchboard_{name}"
        for name in (
            "join",
            "leave",
            "who",
            "claim",
            "say",
            "read",
            "wait",
            "pass",
            "away",
            "review",
            "status",
        )
    )

    def call(sid: str, use: str, tool: str, **args: object) -> dict[str, object]:
        body = {
            "jsonrpc": "2.0",
            "id": 40,
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": {**args, "switchboard_session_id": sid, "switchboard_tool_use_id": use},
            },
        }
        r = c.c.post(
            "/mcp",
            json=body,
            headers=c.headers(
                {
                    "accept": "application/json, text/event-stream",
                    "content-type": "application/json",
                    "mcp-protocol-version": "2025-06-18",
                }
            ),
        )
        assert r.status_code == 200, r.text
        return json.loads(sse_result(r)["result"]["content"][0]["text"])

    def report(sid: str, use: str, tool: str) -> httpx.Response:
        return c.c.post(
            "/mcp/pretool",
            json={
                "harness": "cursor",
                "event": "preToolUse",
                "session_id": sid,
                "tool_use_id": use,
                "tool_name": tool,
            },
            headers={"authorization": f"Bearer {token}"},
        )

    assert report(SID_A, "use-a", "switchboard_join").status_code == 200
    assert call(SID_A, "use-a", "switchboard_join", room="#build", screen_name="one")["ok"]
    assert report(SID_B, "use-b", "switchboard_join").status_code == 200
    assert call(SID_B, "use-b", "switchboard_join", room="#build", screen_name="two")["ok"]
    assert call(SID_A, "missing", "switchboard_who", room="#build")["ok"] is False
    assert report(SID_A, "use-c", "switchboard_who").status_code == 200
    assert call(SID_B, "use-c", "switchboard_who", room="#build")["ok"] is False
    assert report(SID_B, "use-d", "switchboard_who").status_code == 200
    assert call(SID_B, "use-d", "switchboard_who", room="#build")["ok"]

    # The command hook names the same session at Stop and SessionEnd.
    for event in ("stop", "sessionEnd"):
        r = c.c.post(
            "/mcp/event",
            json={"harness": "cursor", "event": event, "session_id": SID_A, "wait_s": 0},
            headers={"authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text


def test_cursor_command_hook_inserts_the_reported_ids(broker: InProcBroker) -> None:
    """The actual command hook sends its report before printing updated_input; the server can
    then check a tool call without inferring which conversation is active."""
    token = make_token(broker)
    payload = {
        "conversation_id": SID_A,
        "session_id": SID_A,
        "hook_event_name": "preToolUse",
        "tool_name": "MCP:switchboard_who",
        "tool_use_id": "use-script",
        "tool_input": {"room": "#build"},
    }
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "pre"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=10,
        env={**os.environ, "SWITCHBOARD_URL": broker.base, "SWITCHBOARD_AGENT_TOKEN": token},
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["updated_input"] == {
        "room": "#build",
        "switchboard_session_id": SID_A,
        "switchboard_tool_use_id": "use-script",
    }
