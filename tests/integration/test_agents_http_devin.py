"""Devin over HTTPS (#394, DESIGN.md §49.7): checked IDs from its input-rewrite hook."""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from conftest import FakeClock, InProcBroker
from test_agents_http import make_token, members, sse_result
from test_agents_http_wake import participant, until

from switchboard.envelope import TOKEN_RE

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "payloads" / "devin"
ROWS = [json.loads(x) for x in (FIX / "https_mcp.jsonl").read_text().splitlines()]
META = next(x["_meta"] for x in ROWS if x["kind"] == "tools/call")
PRE = json.loads((FIX / "https_PreToolUse_2.json").read_text())
POST = json.loads((FIX / "https_PostToolUse_4.json").read_text())
EXITS = json.loads((FIX / "https_exit_signals.json").read_text())
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


def test_devin_refusal_carries_structured_content_for_the_output_schema(broker: InProcBroker) -> None:
    """Devin goes through the same input check as Cursor. A call with no matching hook report is
    refused with ``structuredContent`` ``{"result": <the text>}`` as well as the text, because every
    tool advertises an ``outputSchema`` and a client may reject a reply without it (#533). Fails if
    the refusal returns text content alone."""
    broker.call("room.create", {"name": "#build"})
    dv = Devin(broker, make_token(broker))
    body = {
        "jsonrpc": "2.0",
        "id": 41,
        "method": "tools/call",
        "params": {"_meta": META, "name": "who", "arguments": {"room": "#build"}},
    }
    headers = {**dv.headers, "mcp-method": "tools/call", "mcp-name": "who"}
    r = dv.c.post("/mcp", json=body, headers=headers)
    assert r.status_code == 200, r.text
    res = sse_result(r)["result"]
    text = res["content"][0]["text"]
    assert json.loads(text)["error"] == "this call has no matching session hook"
    assert res["structuredContent"] == {"result": text}, res
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


def open_waits(broker: InProcBroker, name: str) -> int:
    """How many wait() calls of this member the broker holds open."""
    pid = participant(broker, name).id
    return len([s for s in broker.on_loop(broker.state.engine.sinks.for_participant, pid) if s.open])


def test_a_killed_devin_goes_offline_and_is_back_with_its_next_hook(tmp_home: Path) -> None:
    """A Devin killed in its tmux session (SIGHUP) or by SIGTERM runs no SessionEnd hook (Devin CLI
    3000.11.92, live, #448) and stayed busy in Members for good. Its open wait() loses its connection
    and nothing from the session follows: after a wait round (100 s) and a margin (30 s) of the clock
    it is offline, and its membership stays. A second Devin on the same token that keeps re-arming
    its wait through the same time is never marked offline, and the first is busy again with the next
    hook it sends (a resumed Devin). Fails if a dropped wait leaves a ghost busy, a live Devin is taken
    offline, or the room loses the killed one."""
    # what the recording says a signal does and a normal quit doesn't: no SessionEnd
    by_signal = [c for c in EXITS["cases"] if not c["session_end_hook_ran"]]
    assert [c["how"].split(":")[0].split(" of ")[0] for c in by_signal] == ["tmux kill-session", "kill -TERM"]
    assert [c["how"] for c in EXITS["cases"] if c["session_end_hook_ran"]] == [
        "/exit typed at an idle prompt: a normal quit"
    ]
    clock = FakeClock()
    broker = InProcBroker(tmp_home, clock=clock).start()
    try:
        broker.call("room.create", {"name": "#build"})
        token = make_token(broker)
        gone, live = Devin(broker, token), Devin(broker, token)
        for dv, sid, name in ((gone, "session-gone", "gone"), (live, "session-live", "live")):
            assert dv.call("join", dv.pre(sid, f"join-{name}", {"room": "#build", "screen_name": name}))["ok"]
            dv.run_hook("start", {"session_id": sid, "hook_event_name": "SessionStart"})
        # the killed one: a wait that its client drops, as the kernel does for a process that dies
        doomed = httpx.Client(base_url=broker.base, timeout=httpx.Timeout(10.0, read=1.0))
        dropped = Devin(broker, token)
        dropped.c = doomed
        args = gone.pre("session-gone", "call-wait-1", {"room": "#build", "timeout_s": 100}, "wait")
        with pytest.raises(httpx.ReadTimeout):
            dropped.call("wait", args)  # the client gives up and closes the connection
        until(lambda: open_waits(broker, "gone") == 0)  # the broker saw it go
        doomed.close()
        # the live one re-arms: each round the server answers with a timeout, and it calls again
        for i in range(3):
            args = live.pre("session-live", f"call-live-{i}", {"room": "#build", "timeout_s": 100}, "wait")
            with ThreadPoolExecutor(1) as pool:
                got = pool.submit(live.call, "wait", args)
                until(lambda: open_waits(broker, "live") == 1)
                clock.advance(101)
                answer = got.result(60)
            assert answer["ok"] and answer["status"] == "timeout", answer
            assert participant(broker, "live").status != "offline"
        until(lambda: participant(broker, "gone").status == "offline", timeout=15)
        assert participant(broker, "live").status != "offline"
        assert set(members(broker, "#build")) == {"gone", "live"}  # the room keeps the killed one
        # a resumed Devin: its first hook says it is alive
        gone.pre("session-gone", "call-wait-2", {"room": "#build", "timeout_s": 100}, "wait")
        until(lambda: participant(broker, "gone").status == "busy")
    finally:
        gone.c.close()
        live.c.close()
        broker.stop()
