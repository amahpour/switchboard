"""Cursor over HTTPS (#394, DESIGN.md §49.3): a reported hook ID travels in the call input."""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from conftest import InProcBroker
from test_agents_http import Codex, make_token, sse_result

from switchboard.envelope import TOKEN_RE

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "payloads" / "cursor"
SCRIPT = Path(__file__).resolve().parents[2] / "plugins" / "cursor" / "bin" / "hook"
START = json.loads((FIX / "https_sessionStart_1.json").read_text())
SID_A = START["session_id"]
SID_B = "00000000-0000-4000-8000-000000000099"
POST = json.loads((FIX / "postToolUse_mcp.json").read_text())


def invoke(c: Codex, tool: str, arguments: dict[str, object]) -> dict[str, object]:
    """One tools/call as Cursor's client makes it; the answer is the tool's own JSON."""
    params = {"name": tool, "arguments": arguments}
    body = {"jsonrpc": "2.0", "id": 40, "method": "tools/call", "params": params}
    headers = c.headers(
        {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            "mcp-protocol-version": "2025-06-18",
        }
    )
    r = c.c.post("/mcp", json=body, headers=headers, timeout=60)
    assert r.status_code == 200, r.text
    return json.loads(sse_result(r)["result"]["content"][0]["text"])


def run_hook(broker: InProcBroker, token: str, kind: str, payload: dict[str, object]) -> str:
    """The plugin's command hook, as Cursor runs it: the payload on stdin, the reply on stdout."""
    r = subprocess.run(
        [str(SCRIPT), kind],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=20,
        env={**os.environ, "SWITCHBOARD_URL": broker.base, "SWITCHBOARD_AGENT_TOKEN": token},
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


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
        return invoke(c, tool, {**args, "switchboard_session_id": sid, "switchboard_tool_use_id": use})

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
    assert call(SID_A, "use-a", "switchboard_who", room="#build")["ok"] is False
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
        [str(SCRIPT), "pre"],
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


def test_cursor_wait_answer_is_confirmed_by_its_post_hook_even_when_long(broker: InProcBroker) -> None:
    """A wait() answer is confirmed by the call's postToolUse report, which the hook sends as batch
    tokens only, so an answer over the 4 KB a hook request may be is still confirmed. Fails if the hook
    sends the whole tool output: the broker refuses it (413) and the answer is never confirmed."""
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker)
    c = Codex(broker, token, client="Cursor")
    assert c.start().status_code == 200

    def pre(use: str, tool: str, args: dict[str, object]) -> dict[str, object]:
        payload = {
            "conversation_id": SID_A,
            "session_id": SID_A,
            "hook_event_name": "preToolUse",
            "tool_name": f"MCP:{tool}",
            "tool_use_id": use,
            "tool_input": args,
        }
        return json.loads(run_hook(broker, token, "pre", payload))["updated_input"]

    joining = pre("use-join", "switchboard_join", {"room": "#build", "screen_name": "cur"})
    assert invoke(c, "switchboard_join", joining)["ok"]
    waiting = pre("use-wait", "switchboard_wait", {"room": "#build", "timeout_s": 30})
    with ThreadPoolExecutor(1) as pool:
        got = pool.submit(invoke, c, "switchboard_wait", waiting)
        broker.call("human.say", {"room": "#build", "text": "@cur " + "long note " * 395})
        answer = got.result(60)
    assert answer["ok"], answer
    text = json.dumps(answer)
    token_in_answer = TOKEN_RE.search(text)
    assert token_in_answer is not None
    batch = int(token_in_answer.group(1))
    store = broker.state.store
    assert broker.on_loop(store.get_batch, batch).state == "offered"
    post = {k: v for k, v in POST.items() if not k.startswith("_")}
    post.update(
        conversation_id=SID_A,
        session_id=SID_A,
        tool_name="MCP:switchboard_wait",
        tool_use_id="use-wait",
        tool_input=waiting,
        tool_output=json.dumps({"content": [{"type": "text", "text": text}], "isError": False}),
    )
    assert len(json.dumps(post)) > 4096, "the report must be longer than a hook request may be"
    run_hook(broker, token, "post", post)
    assert broker.on_loop(store.get_batch, batch).state == "confirmed"
