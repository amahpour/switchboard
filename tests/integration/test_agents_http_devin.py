"""Devin over HTTPS (#394, DESIGN.md §49.7): checked IDs from its input-rewrite hook."""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from conftest import InProcBroker
from test_agents_http import make_token, sse_result

from switchboard.envelope import TOKEN_RE

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "payloads" / "devin"
ROWS = [json.loads(x) for x in (FIX / "https_mcp.jsonl").read_text().splitlines()]
META = next(x["_meta"] for x in ROWS if x["kind"] == "tools/call")
PRE = json.loads((FIX / "https_PreToolUse_2.json").read_text())
POST = json.loads((FIX / "https_PostToolUse_4.json").read_text())
SCRIPT = Path(__file__).resolve().parents[2] / "plugins" / "devin" / "bin" / "hook"
SERVER = "mcp__switchboard_https__"


class Devin:
    """Devin's side of one device: its MCP client, and the hook script it runs around each call."""

    def __init__(self, broker: InProcBroker, token: str) -> None:
        self.broker, self.token = broker, token
        self.c = httpx.Client(base_url=broker.base, timeout=60)
        self.headers = {
            "authorization": f"Bearer {token}",
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            "mcp-protocol-version": "2026-07-28",
        }

    def call(self, tool: str, args: dict[str, object]) -> dict[str, object]:
        body = {
            "jsonrpc": "2.0",
            "id": 40,
            "method": "tools/call",
            "params": {"_meta": META, "name": tool, "arguments": args},
        }
        headers = {**self.headers, "mcp-method": "tools/call", "mcp-name": tool}
        r = self.c.post("/mcp", json=body, headers=headers)
        assert r.status_code == 200, r.text
        return json.loads(sse_result(r)["result"]["content"][0]["text"])

    def run_hook(self, kind: str, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
        r = subprocess.run(
            [str(SCRIPT), kind],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=20,
            env={**os.environ, "SWITCHBOARD_URL": self.broker.base, "SWITCHBOARD_AGENT_TOKEN": self.token},
        )
        assert r.returncode == 0, r.stderr
        return r

    def pre(self, sid: str, use: str, args: dict[str, object], tool: str = "join") -> dict[str, object]:
        """The input Devin's PreToolUse hook hands back for a call: the arguments plus its IDs."""
        payload = {k: v for k, v in PRE.items() if not k.startswith("_")}
        payload.update(session_id=sid, tool_use_id=use, tool_name=f"{SERVER}{tool}", tool_input=args)
        return json.loads(self.run_hook("pre", payload).stdout)["hookSpecificOutput"]["updatedInput"]

    def post(self, sid: str, use: str, tool: str, response: object) -> str:
        payload = {k: v for k, v in POST.items() if not k.startswith("_")}
        payload.update(
            session_id=sid,
            tool_use_id=use,
            tool_name=f"{SERVER}{tool}",
            tool_response={"success": True, "output": response, "error": None},
        )
        return self.run_hook("post", payload).stdout


def test_devin_hook_routes_two_sessions_on_one_token(broker: InProcBroker) -> None:
    """Each rewritten call carries its own hook IDs, and missing or crossed reports are refused.
    This fails if the generic rmcp clientInfo is mistaken for a session identity."""
    broker.call("room.create", {"name": "#build"})
    dv = Devin(broker, make_token(broker))
    a, b = "session-one", "session-two"
    first = dv.pre(a, "call-a", {"room": "#build", "screen_name": "one"})
    second = dv.pre(b, "call-b", {"room": "#build", "screen_name": "two"})
    assert dv.call("join", first)["ok"]
    assert dv.call("join", second)["ok"]
    assert dv.call("who", {"room": "#build"})["ok"] is False
    crossed = dv.pre(a, "call-cross", {"room": "#build"}, "who")
    crossed["switchboard_session_id"] = b
    assert dv.call("who", crossed)["ok"] is False
    assert dv.call("who", dv.pre(b, "call-good", {"room": "#build"}, "who"))["ok"]
    dv.c.close()


def test_devin_wait_answer_is_confirmed_by_its_hook_even_when_long(broker: InProcBroker) -> None:
    """Devin's wait loop (§9.5) holds over HTTPS: the answer to wait() is confirmed by that call's
    own PostToolUse report, which the hook sends as batch tokens only, so an answer longer than the
    4 KB a hook request may be is still confirmed. Fails if the broker names the tool differently in
    its two reports (the call is not pinned: another call's report would confirm), or if the hook
    sends the whole answer (413: nothing confirms it)."""
    broker.call("room.create", {"name": "#build"})
    dv = Devin(broker, make_token(broker))
    sid = "session-wait"
    assert dv.call("join", dv.pre(sid, "call-join", {"room": "#build", "screen_name": "dev"}))["ok"]
    waiting = dv.pre(sid, "call-wait", {"room": "#build", "timeout_s": 30}, "wait")
    with ThreadPoolExecutor(1) as pool:
        got = pool.submit(dv.call, "wait", waiting)
        broker.call("human.say", {"room": "#build", "text": "@dev " + "long note " * 395})
        answer = got.result(60)
    assert answer["ok"], answer
    output = json.dumps(answer)
    assert len(output) > 4096, "the answer must be longer than a hook request may be"
    token = TOKEN_RE.search(output)
    assert token is not None
    batch = int(token.group(1))
    st = broker.state
    assert broker.on_loop(st.store.get_batch, batch).state == "offered"
    dv.post(sid, "another-call", "wait", output)  # not the call the PreToolUse report named
    assert broker.on_loop(st.store.get_batch, batch).state == "offered"
    dv.post(sid, "call-wait", "wait", output)
    assert broker.on_loop(st.store.get_batch, batch).state == "confirmed"
    dv.c.close()
