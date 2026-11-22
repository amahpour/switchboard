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


# -- the first review of #928 ------------------------------------------------------------------------------


class StandIn:
    """A stand-in for the broker at ``/mcp/event`` on 127.0.0.1: it records each request (path, headers,
    body) and answers with ``answer(path, body)``, a status, extra headers and a body."""

    def __init__(self, answer: Any) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.seen: list[tuple[str, dict[str, str], bytes]] = []
        stand_in = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _any(self) -> None:
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n) if n else b""
                stand_in.seen.append((self.path, dict(self.headers), raw))
                status, headers, data = answer(self.path, raw)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_POST = do_GET = do_HEAD = _any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def run_script(url: str, data: Path, stdin: str | bytes, token: str = "sbat_synthetic") -> Any:
    """The plugin's script against ``url`` with a synthetic token; its exit, stdout and stderr."""
    return subprocess.run(
        [str(SCRIPT)],
        input=stdin.encode() if isinstance(stdin, str) else stdin,
        capture_output=True,
        timeout=30,
        env=child_env(
            SWITCHBOARD_URL=url,
            SWITCHBOARD_AGENT_TOKEN=token,
            GROK_PLUGIN_DATA=str(data),
            NO_PROXY="localhost,127.0.0.1",
        ),
    )


def batch_of(b: InProcBroker, text: str) -> Any:
    bid = int(TOKEN.search(text).group(1))
    return b.on_loop(b.state.store.get_batch, bid)


@pytest.mark.parametrize("end", ["StopCancelled", "StopFailure", "Stop"])
def test_an_old_turn_s_end_leaves_a_session_waiting_on_approval_alone(joined: Any, end: str) -> None:
    """F1 (§11): Grok can report an old turn's end after the next turn began. When the next turn has an
    approval prompt open, that late end (StopCancelled, StopFailure, or a Stop of the old prompt) must change
    nothing: the session still waits on its person and no message reaches it. Fails if the late end is read as
    a subagent's hook, whose path (``subagent_tool_ran``) turns a session waiting on approval busy, and the
    next tool hook would then carry the message."""
    b, token, _g, data = joined
    hook(b, token, data, payload("UserPromptSubmit.json", promptId="prompt-new"))
    hook(b, token, data, payload("Notification_permission_prompt.json"))
    assert row(b, "grok-1")["status"] == "waiting-approval"
    b.call("human.say", {"room": "#build", "text": "@grok-1 are you there"})
    late = payload("StopCancelled_permission.json", promptId="prompt-old", hook_event_name=end)
    if end == "Stop":
        late = payload("Stop.json", promptId="prompt-old")
    started = time.monotonic()
    assert hook(b, token, data, late) == ""
    assert time.monotonic() - started < 10 and not parked(b)
    m = row(b, "grok-1")
    assert m["status"] == "waiting-approval" and m["queued"] == 1, m


@pytest.mark.parametrize("end", ["StopCancelled", "StopFailure"])
def test_a_wake_whose_continued_turn_is_cancelled_or_fails_first_goes_back_to_pending(
    joined: Any, end: str
) -> None:
    """F4: a held Stop is answered with a message, and the next thing the continued turn reports is its
    StopCancelled (the person pressed Esc) or StopFailure (the model's request failed): the model never worked
    on it, so the wake is not seen. It goes back to pending, and the next held Stop gets it. Fails if the
    abort is read as a plain Stop, which confirmed the wake."""
    b, token, _g, data = joined
    prompt_id = payload("Stop.json")["promptId"]
    hook(b, token, data, payload("UserPromptSubmit.json", promptId=prompt_id))
    stop = Held(b, token, data, payload("Stop.json"))
    wait_for(lambda: parked(b), 10, what="the Stop hook to be held")
    b.call("human.say", {"room": "#build", "text": "@grok-1 look at the build"})
    printed = json.loads(stop.result())
    assert printed["decision"] == "block"
    abort = payload("StopCancelled_permission.json", promptId=prompt_id, hook_event_name=end)
    assert hook(b, token, data, abort) == ""
    assert batch_of(b, printed["reason"]).state == "expired"
    m = row(b, "grok-1")
    assert m["status"] == "idle" and m["queued"] == 1, m
    hook(b, token, data, payload("UserPromptSubmit.json", promptId="next-turn"))
    again = Held(b, token, data, payload("Stop.json", promptId="next-turn"))
    assert "look at the build" in json.loads(again.result())["reason"]


def test_a_held_stop_whose_hook_dies_is_closed_and_the_next_stop_gets_the_message(joined: Any) -> None:
    """F3: Grok drops a held Stop hook (the person quit, or sent a queued prompt). The broker watches the
    request for its close, as the listener route does, and closes the park at once: a message then is not
    offered to the dead request, and the session's next Stop gets it. Fails if the park stays open until its
    hour is up, and the message goes to nobody."""
    import signal

    b, token, _g, data = joined
    hook(b, token, data, payload("UserPromptSubmit.json"))
    proc = subprocess.Popen(
        [str(SCRIPT)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env(
            SWITCHBOARD_URL=b.base,
            SWITCHBOARD_AGENT_TOKEN=token,
            GROK_PLUGIN_DATA=str(data),
            NO_PROXY="switchboard.localhost,127.0.0.1",
        ),
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload("Stop.json")).encode())
        proc.stdin.close()
        wait_for(lambda: parked(b), 10, what="the Stop hook to be held")
        proc.send_signal(signal.SIGKILL)
        proc.wait(10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
        for f in (proc.stdout, proc.stderr):
            if f is not None:
                f.close()
    wait_for(lambda: not parked(b), 10, what="the dead hook's park to close")
    b.call("human.say", {"room": "#build", "text": "@grok-1 look at the build"})
    m = row(b, "grok-1")
    assert m["queued"] == 1, m
    hook(b, token, data, payload("UserPromptSubmit.json", promptId="next-turn"))
    stop = Held(b, token, data, payload("Stop.json", promptId="next-turn"))
    assert "look at the build" in json.loads(stop.result())["reason"]


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("name", ["PostToolUse_bash.json", "Stop.json"])
def test_the_script_follows_no_redirect_so_the_token_reaches_no_other_address(
    tmp_path: Path, code: int, name: str
) -> None:
    """F2: the script's request carries the token. A server that answers 3xx (to another host and port) is
    not followed: nothing reaches the second listener, and the script prints nothing. Fails if urllib's
    redirect handler is used, which follows a 301, 302 or 303 to a POST as a GET with the Authorization
    header still on it."""
    second = StandIn(lambda path, raw: (200, {"content-type": "application/json"}, b"{}"))
    first = StandIn(lambda path, raw: (code, {"location": f"http://localhost:{second.port}/mcp/event"}, b""))
    try:
        r = run_script(first.url, tmp_path, json.dumps(payload(name)))
    finally:
        first.close()
        second.close()
    assert r.returncode == 0 and r.stdout == b"", r.stderr
    assert first.seen, "the script sent nothing"
    assert first.seen[0][1].get("Authorization") == "Bearer sbat_synthetic"
    assert second.seen == [], second.seen


def blocks_any_stop(path: str, raw: bytes) -> tuple[int, dict[str, str], bytes]:
    body = json.loads(raw)
    if body["event"] == "Stop" and "wait_s" in body:
        reply = {"decision": "block", "reason": "a message"}
    else:
        reply = {}
    return 200, {"content-type": "application/json"}, json.dumps(reply).encode()


def test_a_wake_count_that_cannot_be_kept_means_the_hook_does_not_park(joined: Any, tmp_path: Path) -> None:
    """F5: the 7-wake cap fails closed. The hook writes the count with this wake in it before it parks; if
    that write fails (here the data directory is a file), it does not park, so the broker hands it no message,
    and it reports a plain Stop, the cap reached. The message is delivered later, at the next held Stop. Fails
    if the hook parks first and counts after, as before: the wake is handed out uncounted."""
    b, token, _g, _data = joined
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("")
    hook(b, token, blocked, payload("UserPromptSubmit.json"))
    b.call("human.say", {"room": "#build", "text": "@grok-1 look at the build"})
    started = time.monotonic()
    assert hook(b, token, blocked, payload("Stop.json")) == ""
    assert time.monotonic() - started < 10 and not parked(b)
    m = row(b, "grok-1")
    assert m["status"] == "idle" and m["queued"] == 1, m
    good = tmp_path / "plugin-data"
    hook(b, token, good, payload("UserPromptSubmit.json", promptId="next-turn"))
    stop = Held(b, token, good, payload("Stop.json", promptId="next-turn"))
    assert "look at the build" in json.loads(stop.result())["reason"]


@pytest.mark.parametrize("stored", ["[1]", "3", "null", '"wakes"', '{"prompt_id": "P", "wakes": "2"}'])
def test_a_stored_wake_count_that_is_not_a_count_is_the_cap(tmp_path: Path, stored: str) -> None:
    """F5: a count file that holds valid JSON that is not a count (not an object, or a bad number) is the cap
    reached: the hook does not park, so it takes no message, and reports a plain Stop. Fails if a file that
    isn't an object reads as no wakes yet, as before: the hook parks and is handed a message."""
    import hashlib

    p = payload("Stop_continuation.json")
    sid, prompt_id = p["session_id"], p["promptId"]
    wakes = tmp_path / "wakes"
    wakes.mkdir()
    (wakes / hashlib.sha256(sid.encode()).hexdigest()[:32]).write_text(stored.replace("P", prompt_id))
    server = StandIn(blocks_any_stop)
    try:
        r = run_script(server.url, tmp_path, json.dumps(p))
    finally:
        server.close()
    assert r.returncode == 0 and r.stdout == b"", r.stderr
    [(_path, _headers, raw)] = server.seen
    assert "wait_s" not in json.loads(raw)  # a plain Stop: nothing is held, nothing handed out


def test_the_script_reads_no_stdin_past_its_cap(tmp_path: Path) -> None:
    """F7: an event's stdin past 1 MB is not read as an event: nothing is reported, nothing printed. Fails if
    the script reads stdin whole, however large."""
    server = StandIn(lambda path, raw: (200, {"content-type": "application/json"}, b"{}"))
    big = payload("PostToolUse_bash.json", padding="x" * (1024 * 1024))
    try:
        r = run_script(server.url, tmp_path, json.dumps(big))
        small = run_script(server.url, tmp_path, json.dumps(payload("PostToolUse_bash.json")))
    finally:
        server.close()
    assert r.returncode == 0 and r.stdout == b"", r.stderr
    assert small.returncode == 0
    assert len(server.seen) == 1  # only the small one


def test_the_script_takes_no_reply_past_its_cap(tmp_path: Path) -> None:
    """F7: a broker's answer past 1 MB is no answer: the script prints nothing, even in a shape Grok reads.
    Fails if the reply is read whole before ``json.loads``."""
    huge = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "x" * (1024 * 1024)}}
    server = StandIn(lambda path, raw: (200, {"content-type": "application/json"}, json.dumps(huge).encode()))
    try:
        r = run_script(server.url, tmp_path, json.dumps(payload("PostToolUse_bash.json")))
    finally:
        server.close()
    assert r.returncode == 0 and r.stdout == b"", r.stderr
    assert server.seen


def test_a_huge_tool_response_is_walked_to_a_size_and_depth_never_serialized(tmp_path: Path) -> None:
    """F7: the batch tokens of a switchboard tool's answer are found by walking it to 32 levels and 10000
    values, never by serializing all of it. A token past either limit is not looked for; one within them is;
    and an answer nested past what JSON can parse is no event, not a crash. Fails if ``tool_response`` is
    serialized whole (every token is found, and the deep one crashes the script)."""
    near, deep, far = "yk:b11.0123abcd", "yk:b12.0123abcd", "yk:b13.0123abcd"
    nested: Any = [deep]
    for _ in range(40):
        nested = [nested]
    wide = ["ab"] * 20000
    wide[10] = near
    wide[15000] = far
    server = StandIn(lambda path, raw: (200, {"content-type": "application/json"}, b"{}"))
    try:
        own = payload("PostToolUse_mcp_switchboard_unsupported.json", tool_response={"a": wide, "b": nested})
        r = run_script(server.url, tmp_path, json.dumps(own))
        crash = payload("PostToolUse_mcp_switchboard_unsupported.json")
        text = json.dumps(crash).replace(
            '"tool_response":', '"tool_response": ' + "[" * 100000 + "]" * 100000 + ', "x":'
        )
        r2 = run_script(server.url, tmp_path, text)
    finally:
        server.close()
    assert r.returncode == 0 and r2.returncode == 0, (r.stderr, r2.stderr)
    assert len(server.seen) == 1, server.seen
    sent = json.loads(server.seen[0][2])["tool_response"]
    assert sent == near, sent
