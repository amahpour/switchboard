"""Waking agents over HTTPS, and their status from hooks (#383, DESIGN.md §49.4).

Claude Code's listener (``/mcp/listen``, a ``curl`` Stop hook with ``asyncRewake``) and Codex's Stop
hook (a call of the hidden ``hook`` tool) wait on the broker until a message comes; every other hook
reports the session's status, confirms what it saw, and may carry priority context back. Driven with
the requests the two harnesses send (``tests/fixtures/mcp_http/``).
"""

from __future__ import annotations

import copy
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from conftest import InProcBroker
from test_agents_http import CALL as CODEX_CALL
from test_agents_http import THREAD_A, Codex, make_token, members, sse_result
from test_agents_http_claude import HOOK, SESSION_1, Claude

from switchboard.broker import agent_http

TOKEN_IN = re.compile(r"yk:b(\d+)\.([0-9a-f]{8})")


@pytest.fixture
def build(broker: InProcBroker) -> InProcBroker:
    broker.call("room.create", {"name": "#build"})
    return broker


def participant(b: InProcBroker, name: str) -> Any:
    st = b.state
    room = b.on_loop(st.store.get_room, "#build")
    m = next(m for m in b.on_loop(st.store.members, room.id) if m.name == name)
    return b.on_loop(st.store.get_participant, m.participant_id)


def claude_hook(claude: Claude, **fields: Any) -> str:
    """One of Claude Code's mcp_tool hooks, its input template filled in."""
    body = copy.deepcopy(HOOK["body"])
    body["id"] = next(claude.ids)
    body["params"]["arguments"] = {"session_id": claude.session, **fields}
    r = claude.post(body, HOOK["headers"], "hook")
    assert r.status_code == 200, r.text
    return str(sse_result(r)["result"]["content"][0]["text"])


def codex_hook(codex: Codex, thread: str, **fields: Any) -> str:
    """One of Codex's mcp_tool hooks: a call of the hook tool on its MCP session."""
    body = copy.deepcopy(CODEX_CALL["body"])
    body["id"] = next(codex.ids)
    body["params"]["name"] = "hook"
    body["params"]["arguments"] = {"session_id": thread, **fields}
    del body["params"]["_meta"]["threadId"]  # a hook's call is the session's, not a thread's
    r = codex.c.post("/mcp", json=body, headers=codex.headers(CODEX_CALL["headers"]))
    assert r.status_code == 200, r.text
    return str(sse_result(r)["result"]["content"][0]["text"])


def listen(
    b: InProcBroker, token: str, session: str | None, wait: float = 60, **extra: str
) -> httpx.Response:
    """Claude Code's listener: ``curl -sf -X POST`` with its session id, and nothing else."""
    h = {"authorization": f"Bearer {token}", "x-switchboard-wait": str(wait), **extra}
    if session is not None:
        h["x-switchboard-session"] = session
    return httpx.post(f"{b.base}/mcp/listen", headers=h, timeout=wait + 10)


def joined_claude(b: InProcBroker) -> tuple[Claude, str]:
    token = make_token(b, "laptop")
    claude = Claude(b, token, SESSION_1)
    assert claude.call("join", room="#build", screen_name="claude-1")["ok"]
    claude.call("read", room="#build")
    return claude, token


def parked(b: InProcBroker, pid: int) -> bool:
    return bool(b.on_loop(b.state.engine.sinks.parks_for, pid))


def until(check: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "not in time"
        time.sleep(0.02)


def test_an_idle_claude_wakes_on_a_message(build: InProcBroker) -> None:
    """Claude Code's listener waits at Stop and is answered with the message, which is what wakes
    it; the turn that starts confirms it. Fails if an idle session over HTTPS isn't woken by a
    message for it, or the wake is never confirmed."""
    claude, token = joined_claude(build)
    p = participant(build, "claude-1")
    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(listen, build, token, SESSION_1)
        until(lambda: parked(build, p.id))
        assert participant(build, "claude-1").status == "idle"  # the listener is its Stop
        build.call("human.say", {"room": "#build", "text": "@claude-1 time to review"})
        r = waiting.result(30)
    assert r.status_code == 200 and "time to review" in r.text, r.text
    bid = int(TOKEN_IN.search(r.text).group(1))
    assert build.on_loop(build.state.store.get_batch, bid).state == "offered"
    # the turn the wake started: its first hook confirms the follow-up
    assert claude_hook(claude, event="UserPromptSubmit", permission_mode="default") == "{}"
    assert build.on_loop(build.state.store.get_batch, bid).state == "confirmed"
    assert participant(build, "claude-1").status == "busy"


def test_a_newer_listener_or_another_hook_ends_the_old_one_with_nothing(build: InProcBroker) -> None:
    """One listener per session: a second ends the first, and so does any other hook of the session
    (the person typed). An ended listener's answer is a 4xx, which curl -f treats as an error, so the
    hook exits 0 and wakes nothing. Fails if an old listener is left waiting, or ends with a 200."""
    claude, token = joined_claude(build)
    p = participant(build, "claude-1")
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(listen, build, token, SESSION_1)
        until(lambda: parked(build, p.id))
        second = pool.submit(listen, build, token, SESSION_1)
        r = first.result(30)
        assert r.status_code == 409, r.text
        until(lambda: parked(build, p.id))
        claude_hook(claude, event="UserPromptSubmit")
        r = second.result(30)
    assert r.status_code == 409, r.text


def test_the_listener_takes_only_a_token_and_a_session(build: InProcBroker) -> None:
    """The listener is behind the same door as /mcp, and says nothing to a request it can't place.
    Fails if it answers a browser, no token, or no session with anything but an error."""
    _claude, token = joined_claude(build)
    assert listen(build, "sbat_" + "x" * 43, SESSION_1).status_code == 401
    assert listen(build, token, None).status_code == 400
    assert listen(build, token, SESSION_1, origin=build.origin).status_code == 403
    r = httpx.get(f"{build.base}/mcp/listen", headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 405
    # a session that never joined: nothing to wait for
    assert listen(build, token, "00000000-0000-4000-8000-0000000000ff").status_code == 409


def test_an_idle_codex_wakes_when_its_stop_hook_is_answered(build: InProcBroker) -> None:
    """Codex's Stop hook waits (listening mode, decision 2), and its answer is decision: block with
    the message, so Codex carries on with it; its next hook confirms. Fails if the Stop hook isn't
    held, or answers in any other shape."""
    codex = Codex(build, make_token(build, "laptop"))
    codex.start()
    assert codex.call("join", THREAD_A, room="#build", screen_name="codex-1")["ok"]
    codex.call("read", THREAD_A, room="#build")
    p = participant(build, "codex-1")
    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(codex_hook, codex, THREAD_A, event="Stop", wait_s="60")
        until(lambda: parked(build, p.id))
        build.call("human.say", {"room": "#build", "text": "@codex-1 one more thing"})
        reply = json.loads(waiting.result(30))
    assert set(reply) == {"decision", "reason"} and reply["decision"] == "block", reply
    assert "one more thing" in reply["reason"]
    bid = int(TOKEN_IN.search(reply["reason"]).group(1))
    assert codex_hook(codex, THREAD_A, event="Stop") == "{}"  # the continued turn ended
    assert build.on_loop(build.state.store.get_batch, bid).state == "confirmed"


def test_a_stop_without_wait_s_only_reports_idle(build: InProcBroker) -> None:
    """Only a Stop hook that says how long it may wait is held; a plain one reports the session
    idle and answers at once. Fails if every Stop is held (a hook config without wait_s would hang)."""
    codex = Codex(build, make_token(build, "laptop"))
    codex.start()
    assert codex.call("join", THREAD_A, room="#build", screen_name="codex-1")["ok"]
    assert codex_hook(codex, THREAD_A, event="UserPromptSubmit", turn_id="t-1") == "{}"
    assert participant(build, "codex-1").status == "busy"
    assert codex_hook(codex, THREAD_A, event="Stop", turn_id="t-1") == "{}"
    assert participant(build, "codex-1").status == "idle"
    pid = participant(build, "codex-1").id
    held = build.on_loop(
        lambda: build.state.store.con.execute(
            "SELECT COUNT(*) FROM events WHERE kind='park' AND participant_id=?", (pid,)
        ).fetchone()[0]
    )
    assert held == 0  # answered at once, never held
    assert codex_hook(codex, THREAD_A, event="SessionEnd") == "{}"
    assert participant(build, "codex-1").status == "offline"


def test_hooks_report_the_approval_mode(build: InProcBroker) -> None:
    """Members shows an approvals-off session as such: over HTTPS the mode comes with each hook.
    Fails if a session over HTTPS stays "unknown" whatever its hooks say."""
    claude, _ = joined_claude(build)
    claude_hook(claude, event="UserPromptSubmit", permission_mode="bypassPermissions")
    assert participant(build, "claude-1").approval_mode == "bypass"
    claude_hook(claude, event="UserPromptSubmit", permission_mode="default")
    assert participant(build, "claude-1").approval_mode == "prompting"


def test_a_busy_claude_gets_a_priority_message_as_hook_context(build: InProcBroker) -> None:
    """While Claude Code works, a message for it rides back on its next PostToolUse hook, as context
    in the hook script's own shape (§7.3). Fails if the message waits for the turn to end, or comes
    back in any other shape."""
    claude, _ = joined_claude(build)
    claude_hook(claude, event="UserPromptSubmit")
    build.call("human.say", {"room": "#build", "text": "@claude-1 stop and check the tests"})
    out = json.loads(claude_hook(claude, event="PostToolUse", tool_name="Bash", tool_use_id="toolu_x1"))
    assert set(out) == {"hookSpecificOutput"}, out
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse" and "check the tests" in hso["additionalContext"]


def test_a_tool_result_seen_by_its_hook_confirms_it(build: InProcBroker) -> None:
    """A read() answer is confirmed by the batch token in its own result, which the PostToolUse
    hook (on switchboard's tools only) passes back. Fails if a pull answer the agent saw is
    expired at Stop and sent again."""
    claude, _ = joined_claude(build)
    claude_hook(claude, event="UserPromptSubmit")  # busy: a message waits for its next call
    build.call("human.say", {"room": "#build", "text": "hello claude"})
    got = claude.call("read", room="#build")
    bid = int(TOKEN_IN.search(got["text"]).group(1))
    claude_hook(
        claude,
        event="PostToolUse",
        tool_name="mcp__switchboard__read",
        tool_use_id="toolu_r1",
        tool_response=json.dumps([{"type": "text", "text": json.dumps(got)}]),
    )
    claude_hook(claude, event="Stop")
    assert build.on_loop(build.state.store.get_batch, bid).state == "confirmed"


def test_the_reply_is_one_of_the_allowed_shapes_or_nothing() -> None:
    """The broker writes a hook's reply (decision 5), so it checks each against the hook script's
    shapes (§7.3) and Codex's Stop block, and answers nothing otherwise. Fails if a continue reaches
    an event that may not have one, or context reaches an event outside the harness's table."""
    reply_for = agent_http.reply_for
    assert reply_for("codex", "Stop", {"kind": "continue", "text": "hi"}) == {
        "decision": "block",
        "reason": "hi",
    }
    assert reply_for("claude", "Stop", {"kind": "continue", "text": "hi"}) is None  # Claude's is the listener
    assert reply_for("codex", "PostToolUse", {"kind": "continue", "text": "hi"}) is None
    assert reply_for("claude", "PostToolUse", {"kind": "context", "text": "hi"}) == {
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "hi"}
    }
    assert reply_for("codex", "UserPromptSubmit", {"kind": "context", "text": "hi"}) is None
    assert reply_for("claude", "PostToolUse", {"kind": "context", "text": "x" * 10_001}) is None
    assert reply_for("claude", "PostToolUse", {"kind": "context", "text": ""}) is None


def test_a_session_that_never_joined_gets_nothing_from_its_hooks(build: InProcBroker) -> None:
    """Hooks of a session with no membership change nothing and answer {}. Fails if a hook can
    create or touch a participant."""
    token = make_token(build, "laptop")
    stranger = Claude(build, token, SESSION_1)
    assert claude_hook(stranger, event="UserPromptSubmit") == "{}"
    assert claude_hook(stranger, event="Stop", wait_s="60") == "{}"  # nothing to hold either
    assert members(build, "#build") == {}


def test_a_round_that_runs_out_asks_for_another_while_the_session_stays_idle(build: InProcBroker) -> None:
    """The listener waits in rounds (#384): one that runs out with nothing answers 408, which the
    listener takes as "wait again", and a later round (``X-Switchboard-Again: 1``) waits only while
    the session is still idle, so it never reports a busy session idle. Fails if a round's end looks
    like a takeover (409, which ends the listener), a later round can't be woken, or one parks a busy
    session."""
    claude, token = joined_claude(build)
    p = participant(build, "claude-1")
    r = listen(build, token, SESSION_1, wait=10)  # held 5 s, then nothing
    assert r.status_code == 408 and r.json()["error"] == "timeout", r.text
    with ThreadPoolExecutor(1) as pool:
        again = pool.submit(listen, build, token, SESSION_1, 60, **{"x-switchboard-again": "1"})
        until(lambda: parked(build, p.id))
        build.call("human.say", {"room": "#build", "text": "@claude-1 still there?"})
        r = again.result(30)
    assert r.status_code == 200 and "still there?" in r.text, r.text
    claude_hook(claude, event="UserPromptSubmit")  # the wake's turn: busy
    r = listen(build, token, SESSION_1, **{"x-switchboard-again": "1"})
    assert r.status_code == 409 and r.json()["error"] == "not_idle", r.text
    assert not parked(build, p.id)


def test_a_broker_that_has_run_a_while_keeps_an_agent_over_https(build: InProcBroker) -> None:
    """The remotes sweep ends the members of a host with no machine link once the broker has run
    15 minutes (``DEFAULT_END_AFTER_S``). An agent over HTTPS has no link: its token is its tie. Seen
    live on 0.32.0: on a broker up for 40 minutes, every join over HTTPS was ended within a second,
    "left (macbook unreachable)". Fails if the sweep takes a token's device for a lost machine."""
    claude, _ = joined_claude(build)
    rem = build.state.remotes

    def age() -> None:
        rem.started_at -= 2 * 900  # as if the broker had run half an hour

    build.on_loop(age)
    build.on_loop(rem.sweep)
    p = participant(build, "claude-1")
    assert p.active and p.status != "offline", p
    assert claude.call("who", room="#build")["ok"]


def session_hook(b: InProcBroker, token: str, body: Any, **headers: str) -> httpx.Response:
    """The plugin's ``bin/session``: a POST of a small JSON object to ``/mcp/session``."""
    h = {"authorization": f"Bearer {token}", "content-type": "application/json", **headers}
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    return httpx.post(f"{b.base}/mcp/session", headers=h, content=data, timeout=10)


def test_the_session_route_takes_only_session_start_and_end_from_an_agent(build: InProcBroker) -> None:
    """``/mcp/session`` (#399) is for Claude Code's SessionStart and SessionEnd only, which it can't
    send as mcp_tool hooks: a SessionEnd takes the session offline, and it answers 200 with a JSON
    object. Any other event (a Stop, which would park), a browser, a missing or wrong token, another
    method or a large body is refused, so the route can't be used to wake or hold a session. Fails if
    it accepts anything wider."""
    claude, token = joined_claude(build)
    sid = SESSION_1
    assert session_hook(build, token, {"event": "Stop", "session_id": sid}).status_code == 400
    assert session_hook(build, token, {"event": "PreToolUse", "session_id": sid}).status_code == 400
    assert session_hook(build, token, ["SessionEnd"]).status_code == 400
    assert (
        session_hook(build, "sbat_" + "x" * 43, {"event": "SessionEnd", "session_id": sid}).status_code == 401
    )
    r = session_hook(build, token, {"event": "SessionEnd", "session_id": sid}, origin=build.origin)
    assert r.status_code == 403
    assert (
        httpx.get(f"{build.base}/mcp/session", headers={"authorization": f"Bearer {token}"}).status_code
        == 405
    )
    assert session_hook(build, token, b"{" + b" " * 4096 + b"}").status_code == 413
    assert participant(build, "claude-1").status != "offline"  # none of those did anything
    r = session_hook(build, token, {"event": "SessionStart", "session_id": sid, "source": "resume"})
    assert r.status_code == 200 and isinstance(r.json(), dict), r.text
    r = session_hook(build, token, {"event": "SessionEnd", "session_id": sid, "reason": "logout"})
    assert r.status_code == 200 and r.json() == {}, r.text
    assert participant(build, "claude-1").status == "offline"
