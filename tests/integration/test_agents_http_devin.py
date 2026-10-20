"""Devin over HTTPS (#394, DESIGN.md §49.7): checked IDs from its input-rewrite hook."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import httpx
from conftest import InProcBroker
from test_agents_http import make_token, sse_result

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "payloads" / "devin"
ROWS = [json.loads(x) for x in (FIX / "https_mcp.jsonl").read_text().splitlines()]
META = next(x["_meta"] for x in ROWS if x["kind"] == "tools/call")
PRE = json.loads((FIX / "https_PreToolUse_2.json").read_text())
SCRIPT = Path(__file__).resolve().parents[2] / "plugins" / "devin" / "bin" / "hook"


def test_devin_hook_routes_two_sessions_on_one_token(broker: InProcBroker) -> None:
    """Each rewritten call carries its own hook IDs, and missing or crossed reports are refused.
    This fails if the generic rmcp clientInfo is mistaken for a session identity."""
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker)
    c = httpx.Client(base_url=broker.base, timeout=20)
    headers = {
        "authorization": f"Bearer {token}",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "mcp-protocol-version": "2026-07-28",
    }

    def call(tool: str, args: dict[str, object]) -> dict[str, object]:
        body = {
            "jsonrpc": "2.0",
            "id": 40,
            "method": "tools/call",
            "params": {"_meta": META, "name": tool, "arguments": args},
        }
        r = c.post("/mcp", json=body, headers={**headers, "mcp-method": "tools/call", "mcp-name": tool})
        assert r.status_code == 200, r.text
        return json.loads(sse_result(r)["result"]["content"][0]["text"])

    def hook(sid: str, use: str, args: dict[str, object], tool: str = "join") -> dict[str, object]:
        payload = {k: v for k, v in PRE.items() if not k.startswith("_")}
        payload.update(
            session_id=sid, tool_use_id=use, tool_name=f"mcp__switchboard_https__{tool}", tool_input=args
        )
        r = subprocess.run(
            [str(SCRIPT), "pre"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=10,
            env={**os.environ, "SWITCHBOARD_URL": broker.base, "SWITCHBOARD_AGENT_TOKEN": token},
        )
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)["hookSpecificOutput"]["updatedInput"]

    a, b = "session-one", "session-two"
    first = hook(a, "call-a", {"room": "#build", "screen_name": "one"})
    second = hook(b, "call-b", {"room": "#build", "screen_name": "two"})
    assert call("join", first)["ok"]
    assert call("join", second)["ok"]
    assert call("who", {"room": "#build"})["ok"] is False
    crossed = hook(a, "call-cross", {"room": "#build"}, "who")
    crossed["switchboard_session_id"] = b
    assert call("who", crossed)["ok"] is False
    assert call("who", hook(b, "call-good", {"room": "#build"}, "who"))["ok"]
    c.close()
