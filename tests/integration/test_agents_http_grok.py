"""Grok Build over HTTPS (DESIGN.md §49.15, #851): its MCP client and its plugin's hook script against an
in-process broker.

The MCP requests are the ones Grok Build 1.0.89 sent (``tests/fixtures/payloads/grok/https_mcp.jsonl``):
client ``grok-shell-<server>``, and the session's id in ``X-Switchboard-Session`` from the plugin's
``{{session_id}}`` template. The hooks are the plugin's own script (``plugins/grok/bin/hook``), run as Grok
runs it, with a recorded payload on stdin and the token in its environment; what it prints is what Grok would
read."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import InProcBroker, child_env
from fakes.waiting import wait_for
from test_agents_http import make_token, sse_result

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "payloads" / "grok"
SCRIPT = Path(__file__).resolve().parents[2] / "plugins" / "grok" / "bin" / "hook"
MCP = [json.loads(line) for line in (FIX / "https_mcp.jsonl").read_text().splitlines()]
INIT = next(r for r in MCP if r["method"] == "initialize")
CALL = next(r for r in MCP if r["method"] == "tools/call")
TOKEN = re.compile(r"yk:b(\d+)\.[0-9a-f]{8}")
SID = "00000000-0000-4000-8000-0000000a1001"
OTHER = "00000000-0000-4000-8000-0000000a1009"


def payload(name: str, sid: str = SID, **changes: Any) -> dict[str, Any]:
    """A recorded payload with its session id (both spellings) set, and anything else changed."""
    p = json.loads((FIX / name).read_text())
    p["sessionId"] = p["session_id"] = sid
    p.update(changes)
    return p


class Grok:
    """One Grok Build session's MCP client, as it was recorded: the 2025-11-25 handshake, its client name, and
    the session id header the plugin's entry adds to every request."""

    def __init__(
        self, b: InProcBroker, token: str, sid: str | None = SID, server: str = "switchboard"
    ) -> None:
        self.c = httpx.Client(base_url=b.base, timeout=30.0)
        self.token, self.sid, self.server = token, sid, server
        self.mcp: str | None = None
        self.n = 10

    def headers(self, recorded: dict[str, str]) -> dict[str, str]:
        dropped = ("authorization", "x-probe-session", "mcp-session-id")
        h = {k: v for k, v in recorded.items() if k not in dropped}
        h["authorization"] = f"Bearer {self.token}"
        if self.sid is not None:
            h["x-switchboard-session"] = self.sid
        if self.mcp is not None:
            h["mcp-session-id"] = self.mcp
        return h

    def start(self) -> Grok:
        body = copy.deepcopy(INIT["body"])
        body["params"]["clientInfo"]["name"] = f"grok-shell-{self.server}"
        r = self.c.post("/mcp", json=body, headers=self.headers(INIT["headers"]))
        assert r.status_code == 200, r.text
        self.mcp = r.headers["mcp-session-id"]
        done = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        assert self.c.post("/mcp", json=done, headers=self.headers(CALL["headers"])).status_code == 202
        return self

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        self.n += 1
        body = copy.deepcopy(CALL["body"])
        body["id"] = self.n
        body["params"]["name"] = tool
        body["params"]["arguments"] = arguments
        r = self.c.post("/mcp", json=body, headers=self.headers(CALL["headers"]))
        assert r.status_code == 200, r.text
        return json.loads(sse_result(r)["result"]["content"][0]["text"])


def hook(b: InProcBroker, token: str, data: Path, p: dict[str, Any], timeout: float = 30) -> str:
    """The plugin's hook as Grok runs it: the payload on stdin, the token in the environment, its data
    directory Grok's; what it prints."""
    r = subprocess.run(
        [str(SCRIPT)],
        input=json.dumps(p),
        text=True,
        capture_output=True,
        timeout=timeout,
        env=child_env(
            SWITCHBOARD_URL=b.base,
            SWITCHBOARD_AGENT_TOKEN=token,
            GROK_PLUGIN_DATA=str(data),
            NO_PROXY="switchboard.localhost,127.0.0.1",
        ),
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


class Held:
    """A Stop hook running in the background, as Grok holds it while the session is idle."""

    def __init__(self, b: InProcBroker, token: str, data: Path, p: dict[str, Any]) -> None:
        self.out: str | None = None
        self.thread = threading.Thread(target=self._run, args=(b, token, data, p), daemon=True)
        self.thread.start()

    def _run(self, b: InProcBroker, token: str, data: Path, p: dict[str, Any]) -> None:
        self.out = hook(b, token, data, p, timeout=60)

    def result(self) -> str:
        self.thread.join(30)
        assert not self.thread.is_alive(), "the held Stop hook did not answer"
        assert self.out is not None
        return self.out


def row(b: InProcBroker, name: str) -> dict[str, Any]:
    who = b.call("room.who", {"room": "#build"})
    return next(m for m in who["members"] if m["name"] == name)


def parked(b: InProcBroker) -> bool:
    return bool(b.on_loop(lambda: list(b.state.engine.sinks.open_sinks())))


@pytest.fixture
def joined(broker: InProcBroker, tmp_path: Path) -> tuple[InProcBroker, str, Grok, Path]:
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker)
    g = Grok(broker, token).start()
    res = g.call("join", room="#build", screen_name="grok-1")
    assert res["ok"], res
    return broker, token, g, tmp_path / "plugin-data"


def test_a_grok_session_joins_with_its_session_header_and_one_without_it_is_refused(
    broker: InProcBroker,
) -> None:
    """Grok's client name is served (before #851 every tool answered ``unsupported``), the session is the one
    its header names, on the token's device, with Grok's tier. A call with no header names no session and is
    refused with what to fix. Two sessions on one device are two members."""
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker)
    g = Grok(broker, token).start()
    res = g.call("join", room="#build", screen_name="grok-1")
    assert res["ok"] and res["tier"] == "grok:https", res
    m = row(broker, "grok-1")
    assert (m["harness"], m["host"], m["tier"]) == ("grok", "laptop", "grok:https"), m
    assert g.call("say", room="#build", text="hello")["ok"]
    two = Grok(broker, token, sid=OTHER).start()
    assert two.call("join", room="#build", screen_name="grok-2")["ok"]
    assert row(broker, "grok-2")["harness"] == "grok"
    # Grok's hooks are commands at /mcp/event: the hidden hook tool, called over its MCP connection (by the
    # model, say), does nothing, not even a held Stop
    assert g.call("hook", event="Stop", session_id=SID, wait_s="600") == {}
    assert not parked(broker)
    none = Grok(broker, token, sid=None).start()
    refused = none.call("join", room="#build", screen_name="grok-3")
    assert refused["ok"] is False and "X-Switchboard-Session" in refused["error"], refused


def test_status_follows_the_hooks_and_a_held_stop_wakes_the_session(joined: Any) -> None:
    """SessionStart and a Stop make it idle, UserPromptSubmit busy; an idle session's Stop hook is held, and a
    person's @mention answers it with ``decision: block`` and the message, which the script prints; the
    continued turn answers in the room and its Stop confirms the wake and is held again. Fails if the script
    or the broker answers a Stop with anything else, or a held Stop is not filled."""
    b, token, g, data = joined
    assert hook(b, token, data, payload("SessionStart.json")) == ""
    assert row(b, "grok-1")["status"] == "idle"
    assert hook(b, token, data, payload("UserPromptSubmit.json")) == ""
    assert row(b, "grok-1")["status"] == "busy"
    stop = Held(b, token, data, payload("Stop.json"))
    wait_for(lambda: parked(b) and row(b, "grok-1")["status"] == "idle", 10, what="the Stop hook to be held")
    msg = b.call("human.say", {"room": "#build", "text": "@grok-1 look at the build"})
    printed = json.loads(stop.result())
    assert set(printed) == {"decision", "reason"} and printed["decision"] == "block", printed
    assert "look at the build" in printed["reason"] and f"id={msg['id']}" in printed["reason"], printed
    assert row(b, "grok-1")["status"] == "busy"
    assert g.call("say", room="#build", text="on it")["ok"]
    again = Held(b, token, data, payload("Stop_continuation.json", promptId=payload("Stop.json")["promptId"]))
    wait_for(lambda: parked(b), 10, what="the continued turn's Stop to be held")
    bid = int(TOKEN.search(printed["reason"]).group(1))
    assert b.on_loop(b.state.store.get_batch, bid).state == "confirmed"
    b.call("human.say", {"room": "#build", "text": "@grok-1 and one more"})
    assert json.loads(again.result())["decision"] == "block"


def test_a_busy_session_hears_a_priority_message_after_any_tool_and_only_as_context(joined: Any) -> None:
    """A mention for a busy session reaches it after its next tool call, a shell command's too: the script
    reports the tool (its name and call id, never its input or output) and prints the broker's context in the
    one shape Grok's model sees. A switchboard tool's report carries only the batch tokens of its answer."""
    b, token, g, data = joined
    hook(b, token, data, payload("UserPromptSubmit.json"))
    b.call("human.say", {"room": "#build", "text": "@grok-1 stop and check the tests"})
    printed = json.loads(hook(b, token, data, payload("PostToolUse_bash.json")))
    assert set(printed) == {"hookSpecificOutput"}, printed
    ctx = printed["hookSpecificOutput"]
    assert ctx["hookEventName"] == "PostToolUse" and "stop and check the tests" in ctx["additionalContext"]
    read = g.call("read", room="#build")
    tokens = " ".join(read.get("tokens") or []) or json.dumps(read)
    own = payload(
        "PostToolUse_mcp_switchboard_unsupported.json",
        tool_response={"type": "MCP", "output": {"OkayOutput": tokens}},
    )
    assert hook(b, token, data, own) == ""
    assert row(b, "grok-1")["status"] == "busy"


def test_an_open_approval_prompt_holds_the_session_and_nothing_is_delivered_to_it(joined: Any) -> None:
    """Grok's permission prompt (a Notification of type ``permission_prompt``) shows the session waiting on
    its person, and the script prints nothing for it. A mention then waits: nothing is offered while the
    prompt is open. Declining ends the turn with StopCancelled, a Stop that is never held, so the session is
    idle and not listening; approving runs the tool, whose hook makes it busy and carries the message. Fails
    if a session waiting on an approval is woken or given context (§7.4, §11)."""
    b, token, _g, data = joined
    hook(b, token, data, payload("UserPromptSubmit.json"))
    assert hook(b, token, data, payload("Notification_permission_prompt.json")) == ""
    assert row(b, "grok-1")["status"] == "waiting-approval"
    b.call("human.say", {"room": "#build", "text": "@grok-1 are you there"})
    assert row(b, "grok-1")["status"] == "waiting-approval" and row(b, "grok-1")["queued"] == 1
    prompt_id = payload("UserPromptSubmit.json")["promptId"]
    started = time.monotonic()
    assert hook(b, token, data, payload("StopCancelled_permission.json", promptId=prompt_id)) == ""
    assert time.monotonic() - started < 10 and not parked(b)
    m = row(b, "grok-1")
    assert m["status"] == "idle" and m["queued"] == 1 and "type in Grok" in (m["parked_reason"] or ""), m
    hook(b, token, data, payload("UserPromptSubmit.json", promptId="p-2"))
    hook(b, token, data, payload("Notification_permission_prompt.json"))
    printed = json.loads(hook(b, token, data, payload("PostToolUse_bash.json")))
    assert "are you there" in printed["hookSpecificOutput"]["additionalContext"]
    assert row(b, "grok-1")["status"] == "busy"


def test_a_late_cancel_of_an_old_turn_leaves_the_new_turn_busy(joined: Any) -> None:
    """Grok can report a cancelled turn after the next turn's UserPromptSubmit (its docs, 1.0.89): the cancel
    names its own prompt, so it is not the current turn's end. Fails if a busy session is shown idle."""
    b, token, _g, data = joined
    hook(b, token, data, payload("UserPromptSubmit.json", promptId="prompt-new"))
    assert row(b, "grok-1")["status"] == "busy"
    hook(b, token, data, payload("StopCancelled_permission.json", promptId="prompt-old"))
    assert row(b, "grok-1")["status"] == "busy"
    hook(b, token, data, payload("StopCancelled_permission.json", promptId="prompt-new"))
    assert row(b, "grok-1")["status"] == "idle"


def test_the_listener_wakes_a_turn_seven_times_and_then_reports_idle(joined: Any) -> None:
    """Grok ends a turn after 8 Stop blocks and fires no Stop after the 8th, so a listener that blocked an 8th
    time would leave the session shown busy for good. The script counts the wakes of one turn (its prompt id,
    reset when ``stopHookActive`` is false): 7 are delivered; at the 8th Stop it is not held, reports idle,
    and prints nothing. A new turn listens again."""
    b, token, g, data = joined
    prompt_id = payload("Stop.json")["promptId"]
    hook(b, token, data, payload("UserPromptSubmit.json", promptId=prompt_id))
    for n in range(7):
        name = "Stop.json" if n == 0 else "Stop_continuation.json"
        stop = Held(b, token, data, payload(name, promptId=prompt_id))
        wait_for(lambda: parked(b), 10, what=f"Stop {n + 1} to be held")
        b.call("human.say", {"room": "#build", "text": f"@grok-1 wake {n + 1}"})
        assert f"wake {n + 1}" in json.loads(stop.result())["reason"]
        assert g.call("say", room="#build", text=f"woke {n + 1}")["ok"]
    b.call("human.say", {"room": "#build", "text": "@grok-1 wake 8"})
    started = time.monotonic()
    assert hook(b, token, data, payload("Stop_continuation.json", promptId=prompt_id)) == ""
    assert time.monotonic() - started < 10 and not parked(b)
    m = row(b, "grok-1")
    assert m["status"] == "idle" and m["queued"] == 1, m
    hook(b, token, data, payload("UserPromptSubmit.json", promptId="next-turn"))
    stop = Held(b, token, data, payload("Stop.json", promptId="next-turn"))
    assert "wake 8" in json.loads(stop.result())["reason"]


def test_a_subagent_s_hooks_and_the_end_of_session_stop_are_not_reported(joined: Any) -> None:
    """A subagent runs under its own session id, with ``subagentType`` (recorded): the script reports none of
    its hooks, even one forged with the parent's id. The observe-only Stop at session end is not reported or
    held (it would hold the exit for an hour); SessionEnd shows the session offline, still a member."""
    b, token, _g, data = joined
    hook(b, token, data, payload("UserPromptSubmit.json"))
    for name in ("PostToolUse_subagent.json", "SessionEnd_subagent.json"):
        assert hook(b, token, data, payload(name)) == "", name  # payload() gives it the parent's id
        assert row(b, "grok-1")["status"] == "busy", name
    assert hook(b, token, data, payload("SessionEnd.json")) == ""
    assert row(b, "grok-1")["status"] == "offline"
    started = time.monotonic()
    assert hook(b, token, data, payload("Stop_session_end.json")) == ""
    assert time.monotonic() - started < 10 and not parked(b)
    assert row(b, "grok-1")["status"] == "offline"


def test_the_script_prints_only_the_shapes_grok_may_read_whatever_the_server_says(tmp_path: Path) -> None:
    """Against a server that answers every report with every decision it could (approve, allow, deny, a
    rewritten input, a block on a tool), the script prints nothing for any recorded event, except a Stop
    block on a Stop and context after a tool, in their exact shapes. And it sends no tool input or output,
    path or prompt text. Fails if the plugin could relay a permission decision, or the person's data."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    answers = [
        {"decision": "approve"},
        {"permission": "allow"},
        {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}},
        {"decision": "block", "reason": "x", "permissionDecision": "allow"},
        {"updatedInput": {"command": "rm -rf /"}},
        {"continue": False},
    ]
    seen: list[dict[str, Any]] = []
    which = {"i": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen.append(body)
            data = json.dumps(answers[which["i"] % len(answers)]).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for p in sorted(FIX.glob("*.json")):
            for i in range(len(answers)):
                which["i"] = i
                r = subprocess.run(
                    [str(SCRIPT)],
                    input=p.read_text(),
                    text=True,
                    capture_output=True,
                    timeout=30,
                    env=child_env(
                        SWITCHBOARD_URL=url,
                        SWITCHBOARD_AGENT_TOKEN="sbat_test",
                        GROK_PLUGIN_DATA=str(tmp_path),
                        NO_PROXY="127.0.0.1",
                    ),
                )
                assert r.returncode == 0 and r.stdout == "", (p.name, answers[i], r.stdout)
    finally:
        server.shutdown()
        server.server_close()
    assert seen, "the script reported nothing"
    allowed = {
        "harness", "event", "session_id", "prompt_id", "permission_mode", "source", "reason",
        "notification_type", "tool_name", "tool_use_id", "tool_response", "stop_hook_active", "wait_s",
    }  # fmt: skip
    for body in seen:
        assert set(body) <= allowed, body
        assert body["harness"] == "grok"
        assert body.get("tool_response", "") == "", body  # no batch tokens in these recordings
        flat = json.dumps(body)
        for private in ("/ws", "/grok-home", "echo hi", "Ping the sb server", "probe echo"):
            assert private not in flat, (private, body)
