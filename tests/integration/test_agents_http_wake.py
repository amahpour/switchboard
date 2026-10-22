"""Waking agents over HTTPS, and their status from hooks (#383, DESIGN.md §49.4).

Claude Code's listener (``/mcp/listen``, a ``curl`` Stop hook with ``asyncRewake``) and Codex's Stop
hook (a call of the hidden ``hook`` tool) wait on the broker until a message comes; every other hook
reports the session's status, confirms what it saw, and may carry priority context back. Driven with
the requests the two harnesses send (``tests/fixtures/mcp_http/``).
"""

from __future__ import annotations

import copy
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from conftest import InProcBroker
from test_agents_http import CALL as CODEX_CALL
from test_agents_http import THREAD_A, Codex, make_token, members, sse_result
from test_agents_http_claude import HOOK, SESSION_1, USES, Claude

from switchboard import __version__
from switchboard.broker import agent_http, hook_replies

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


def member(b: InProcBroker, name: str) -> Any:
    """A member's buddy-list row: ``queued`` messages waiting for it, ``inflight`` ones offered."""
    st = b.state
    room = b.on_loop(st.store.get_room, "#build")
    return next(m for m in b.on_loop(st.store.members, room.id) if m.name == name)


def row(b: InProcBroker, name: str) -> dict[str, Any]:
    """A member as Members shows it (``who``): its status, and whether and why it is parked."""
    who = b.call("room.who", {"room": "#build"})
    return next(m for m in who["members"] if m["name"] == name)


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


OWN_READ = ("mcp__switchboard__read", "mcp__plugin_switchboard_switchboard__read")


@pytest.mark.parametrize("tool", OWN_READ)
def test_a_switchboard_tools_report_without_its_result_is_dropped(build: InProcBroker, tool: str) -> None:
    """After a call of switchboard's own tool the plugin sends two reports (#422): the one with the
    tool's result (the batch tokens in it confirm a read() answer), and the one every other tool's call
    gets, with none. Codex's matcher can't leave switchboard's tools out of the second, so the server
    drops it: it answers {} and leaves the waiting message for the report that has the result. A
    result that is empty is still a result. Fails if the report without one takes the context (the
    result's report would then find nothing, and a confirmation could be missed) or an empty result is
    dropped with it."""
    claude, _ = joined_claude(build)
    claude_hook(claude, event="UserPromptSubmit")
    build.call("human.say", {"room": "#build", "text": "@claude-1 look at the build"})
    assert member(build, "claude-1").queued == 1
    assert claude_hook(claude, event="PostToolUse", tool_name=tool, tool_use_id="toolu_d1") == "{}"
    assert (member(build, "claude-1").queued, member(build, "claude-1").inflight) == (1, 0)
    out = claude_hook(claude, event="PostToolUse", tool_name=tool, tool_use_id="toolu_d1", tool_response="")
    assert "look at the build" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert (member(build, "claude-1").queued, member(build, "claude-1").inflight) == (0, 0)  # delivered


def test_a_report_without_a_result_confirms_and_takes_back_nothing(build: InProcBroker) -> None:
    """Every other tool's call is reported (#422), with nothing in it a batch token could be read
    from: so it confirms no read() answer on its own, expires none, and hands out nothing while one is
    still in flight. Only the report with the answer's own token confirms it, and the message that
    waited behind it comes with that report, and a later Bash call's report has nothing to add. Fails
    if a Bash call's report confirms or drops an answer it didn't carry."""
    claude, _ = joined_claude(build)
    claude_hook(claude, event="UserPromptSubmit")
    build.call("human.say", {"room": "#build", "text": "hello claude"})
    got = claude.call("read", room="#build")
    bid = int(TOKEN_IN.search(got["text"]).group(1))
    state = lambda: build.on_loop(build.state.store.get_batch, bid).state  # noqa: E731
    assert state() == "offered"
    build.call("human.say", {"room": "#build", "text": "@claude-1 and then this"})
    # a Bash call that runs beside the read(), and one that fails
    for event in ("PostToolUse", "PostToolUseFailure"):
        assert claude_hook(claude, event=event, tool_name="Bash", tool_use_id="toolu_b1") == "{}"
        assert (state(), member(build, "claude-1").queued) == ("offered", 1)  # kept, and nothing handed out
    out = claude_hook(
        claude,
        event="PostToolUse",
        tool_name="mcp__switchboard__read",
        tool_use_id="toolu_r1",
        tool_response=json.dumps([{"type": "text", "text": json.dumps(got)}]),
    )
    assert state() == "confirmed"  # by its own token; and the message that waited behind it comes with it
    assert "and then this" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert claude_hook(claude, event="PostToolUse", tool_name="Bash", tool_use_id="toolu_b2") == "{}"
    assert state() == "confirmed" and member(build, "claude-1").queued == 0  # nothing left to hand out


SUBAGENT = "a0123456789abcdef"  # Claude Code's agent_id for a subagent: 'a' and 16 hex digits


def test_a_subagents_hook_delivers_and_confirms_nothing(build: InProcBroker) -> None:
    """Claude Code runs the session's hooks for a subagent's tool calls, with the session's id (#446).
    The subagent is not in the room, so while the session is busy and a message waits, a subagent's
    PostToolUse or PostToolUseFailure gets ``{}``: the message stays queued, and a read() answer the
    session is holding stays unconfirmed even when the subagent's report carries its token. The
    session's own next hook (``agent_id`` empty, as the plugin sends it) confirms and gets everything.
    Fails if a subagent's tool call takes a message meant for the session, or settles what only the
    session's own hook may."""
    claude, _ = joined_claude(build)
    claude_hook(claude, event="UserPromptSubmit")
    build.call("human.say", {"room": "#build", "text": "hello claude"})
    got = claude.call("read", room="#build")
    bid = int(TOKEN_IN.search(got["text"]).group(1))
    state = lambda: build.on_loop(build.state.store.get_batch, bid).state  # noqa: E731
    assert state() == "offered"
    build.call("human.say", {"room": "#build", "text": "@claude-1 stop and check the tests"})
    assert member(build, "claude-1").queued == 1
    for event in ("PostToolUse", "PostToolUseFailure"):
        assert (
            claude_hook(claude, event=event, tool_name="Bash", tool_use_id="toolu_a1", agent_id=SUBAGENT)
            == "{}"
        )
    # even a report of switchboard's own tool, carrying the answer's token, is the subagent's
    assert (
        claude_hook(
            claude,
            event="PostToolUse",
            tool_name="mcp__switchboard__read",
            tool_use_id="toolu_a2",
            agent_id=SUBAGENT,
            tool_response=json.dumps([{"type": "text", "text": json.dumps(got)}]),
        )
        == "{}"
    )
    assert state() == "offered" and (
        member(build, "claude-1").queued,
        member(build, "claude-1").inflight,
    ) == (1, 1)
    assert participant(build, "claude-1").status == "busy"
    # the session's own report of the same read() (agent_id empty, as the plugin sends it) confirms the
    # answer, and the message that waited behind it comes with it
    out = claude_hook(
        claude,
        event="PostToolUse",
        tool_name="mcp__switchboard__read",
        tool_use_id="toolu_a2",
        agent_id="",
        tool_response=json.dumps([{"type": "text", "text": json.dumps(got)}]),
    )
    assert state() == "confirmed"
    assert "check the tests" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert member(build, "claude-1").queued == 0


def test_a_subagents_tool_calls_leave_an_idle_sessions_listener_waiting(build: InProcBroker) -> None:
    """A background subagent keeps making tool calls after its parent's turn ended, and each one runs
    the parent's hooks (#446). The parent is idle with its listener waiting; those calls must neither
    end the listener (any hook of the session does, §49.4) nor mark the session busy, or an @mention
    would find no one to wake. Fails if a subagent's PostToolUse ends the wait or changes the status, or
    the message doesn't reach the listener afterwards."""
    claude, token = joined_claude(build)
    p = participant(build, "claude-1")
    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(listen, build, token, SESSION_1)
        until(lambda: parked(build, p.id))
        for event in ("PostToolUse", "PostToolUseFailure", "PostToolUse"):
            assert (
                claude_hook(claude, event=event, tool_name="Bash", tool_use_id="toolu_b1", agent_id=SUBAGENT)
                == "{}"
            )
        assert parked(build, p.id) and participant(build, "claude-1").status == "idle"
        build.call("human.say", {"room": "#build", "text": "@claude-1 are you there"})
        r = waiting.result(30)
    assert r.status_code == 200 and "are you there" in r.text, (r.status_code, r.text)


def test_a_subagents_own_call_of_a_switchboard_tool_is_still_named_by_its_hook(build: InProcBroker) -> None:
    """A subagent can call the session's switchboard tools. Its PreToolUse report, which only says which
    session made the call (§49.3), is kept as it always was (#446), so the call is the session's and is
    not refused for want of a report. Fails if a PreToolUse with an ``agent_id`` is dropped."""
    claude, _ = joined_claude(build)
    use = next(USES)
    assert (
        claude_hook(
            claude, event="PreToolUse", tool_use_id=use, tool_name="mcp__switchboard__read", agent_id=SUBAGENT
        )
        == "{}"
    )
    r = claude.raw("read", use, room="#build")
    assert r.status_code == 200 and json.loads(sse_result(r)["result"]["content"][0]["text"])["ok"]


def test_what_counts_as_a_subagents_hook() -> None:
    """``from_subagent`` is true for any ``agent_id`` that says something, of any length or type
    (a value that isn't recognized is not the session's own: fails closed), and false for nothing:
    absent, empty or blank. ``agent_type`` alone is not it, because the main thread of a session
    started with ``claude --agent`` has one. Fails if a long, odd or unexpected value passes for the
    session's own call and takes its messages."""
    sub = agent_http.from_subagent
    for yes in (SUBAGENT, " x ", "x" * 500, "${agent_id}", 7, ["a"], {"a": 1}, True):
        assert sub({"agent_id": yes}), yes
    for no in (None, "", "   ", "\n", False):
        assert not sub({"agent_id": no}), no
    assert not sub({}) and not sub({"agent_type": "tester"})
    assert "agent_id" in agent_http.HOOK_FIELDS and "agent_type" not in agent_http.HOOK_FIELDS


def test_the_report_of_a_second_tool_is_told_from_the_one_with_a_result() -> None:
    """``second_report`` is true only for a PostToolUse of a switchboard tool (by any harness's name
    for it: Claude Code's, Codex's, Devin's and Cursor's) that sent no ``tool_response`` at all. Fails if
    it drops a report that has a result (empty or not), another tool's report, or a failure's (which never
    has a result)."""
    second = agent_http.second_report
    none: dict[str, Any] = {"tool_response": None}
    for tool in (
        "mcp__switchboard__say",
        "mcp__plugin_switchboard_switchboard__wait",
        "mcp__switchboard_https__wait",
        "MCP:switchboard_say",
    ):
        assert second("PostToolUse", {"tool_name": tool, **none})
        assert not second("PostToolUse", {"tool_name": tool, "tool_response": ""})
        assert not second("PostToolUse", {"tool_name": tool, "tool_response": '[{"type": "text"}]'})
        assert not second("PostToolUseFailure", {"tool_name": tool, **none})
    for tool in (
        "Bash",
        "mcp__github__say",
        "MCP:github_say",
        "xmcp__switchboard__say",
        "xMCP:switchboard_say",
        "",
    ):
        assert not second("PostToolUse", {"tool_name": tool, **none}), tool
    assert not second("PostToolUse", none)  # no tool name at all


def test_the_reply_is_one_of_the_allowed_shapes_or_nothing() -> None:
    """The broker writes a hook's reply (decision 5), so it checks each against the hook script's
    shapes (§7.3) and Codex's Stop block, and answers nothing otherwise. Fails if a continue reaches
    an event that may not have one, or context reaches an event outside the harness's table."""
    reply_for = hook_replies.reply_for
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
    """Hooks of a session with no membership change nothing and answer {}, however many tool calls
    it reports (every tool's is, #422, in a room or not): no participant, event or batch is written.
    Fails if a hook can create or touch a participant, or costs a session in no room a write."""
    token = make_token(build, "laptop")
    stranger = Claude(build, token, SESSION_1)
    rows = lambda: build.on_loop(  # noqa: E731
        lambda: [
            build.state.store.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("participants", "memberships", "events", "batches")
        ]
    )
    before = rows()
    assert claude_hook(stranger, event="UserPromptSubmit") == "{}"
    assert claude_hook(stranger, event="Stop", wait_s="60") == "{}"  # nothing to hold either
    # every tool call is reported, in a room or not (#422)
    for event in ("PostToolUse", "PostToolUseFailure"):
        assert claude_hook(stranger, event=event, tool_name="Bash", tool_use_id="toolu_s1") == "{}"
    assert members(build, "#build") == {}
    assert rows() == before  # nothing written for it: not a participant, an event, or a batch


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


def set_status(b: InProcBroker, name: str, status: str) -> None:
    """Put the member's session in a state, as a hook would (the listener's door reads it)."""
    p = participant(b, name)
    b.on_loop(b.state.store.set_status, p.id, status, "test")


@pytest.mark.parametrize("status", ["idle", "starting"])
def test_a_later_round_waits_in_any_state_that_has_not_moved_on(build: InProcBroker, status: str) -> None:
    """A reconnecting listener (``X-Switchboard-Again: 1``) is the one a proxy cut, and a refusal ends
    it for good, leaving the session deaf (#436). So the broker refuses it only when the session has
    moved on, and any other state is treated as still listening: "idle", and "starting" too, which an
    older rule that wanted exactly "idle" turned away. Fails if a state that isn't a running turn or
    an ended session is refused, or a message doesn't reach the round that waited."""
    claude, token = joined_claude(build)
    set_status(build, "claude-1", status)
    p = participant(build, "claude-1")
    with ThreadPoolExecutor(1) as pool:
        again = pool.submit(listen, build, token, SESSION_1, 60, **{"x-switchboard-again": "1"})
        until(lambda: parked(build, p.id))
        build.call("human.say", {"room": "#build", "text": "@claude-1 are you there"})
        r = again.result(30)
    assert r.status_code == 200 and "are you there" in r.text, (r.status_code, r.text)


@pytest.mark.parametrize(
    "status,why",
    [
        ("busy", "a turn is running"),
        ("waiting-approval", "a turn is waiting on an approval"),
        ("offline", "the session ended"),
    ],
)
def test_a_later_round_is_refused_once_the_session_has_moved_on_and_the_broker_says_so(
    build: InProcBroker, caplog: pytest.LogCaptureFixture, status: str, why: str
) -> None:
    """A turn that started (busy, or waiting on an approval, which a message must never be pushed
    into) or a session that ended (offline) is the only reason to refuse a reconnecting listener. It
    is refused with the reason and the state, and the broker logs the refusal at warning level, with
    the session and the state and not the token, so a deaf session can be explained afterwards (#436).
    Fails if such a round parks the session, or the refusal leaves no log line."""
    claude, token = joined_claude(build)
    set_status(build, "claude-1", status)
    p = participant(build, "claude-1")
    with caplog.at_level(logging.WARNING, logger="switchboard.agent_http"):
        r = listen(build, token, SESSION_1, **{"x-switchboard-again": "1"})
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "not_idle" and r.json()["status"] == status and why in r.json()["message"]
    assert not parked(build, p.id)
    [rec] = [x for x in caplog.records if x.name == "switchboard.agent_http"]
    assert rec.levelno == logging.WARNING
    assert SESSION_1 in rec.getMessage() and status in rec.getMessage() and token not in rec.getMessage()


def test_a_later_round_of_a_session_that_never_joined_is_refused_and_logged(
    build: InProcBroker, caplog: pytest.LogCaptureFixture
) -> None:
    """A session with no record here has nothing to be woken for: its reconnecting listener is refused
    (409) and logged, not parked. One that someone else removed is told so instead, once (410, tested
    below). Fails if the round parks anything, or leaves no log line."""
    token = make_token(build, "laptop")
    with caplog.at_level(logging.WARNING, logger="switchboard.agent_http"):
        r = listen(build, token, SESSION_1, **{"x-switchboard-again": "1"})
    assert r.status_code == 409 and r.json()["error"] == "not_idle", r.text
    [rec] = [x for x in caplog.records if x.name == "switchboard.agent_http"]
    assert "in no room" in rec.getMessage() and SESSION_1 in rec.getMessage()
    assert build.on_loop(lambda: build.state.engine.sinks.open_sinks()) == []


def test_a_first_round_is_never_logged_as_a_refusal(
    build: InProcBroker, caplog: pytest.LogCaptureFixture
) -> None:
    """Only a reconnect can be refused for its state: a session's first round is its Stop, and ends in
    the ordinary ways (a takeover, a hook), none of which is a warning. Fails if an idle session's
    listener, taken over by a newer one, fills the broker's log."""
    claude, token = joined_claude(build)
    p = participant(build, "claude-1")
    with caplog.at_level(logging.WARNING, logger="switchboard.agent_http"), ThreadPoolExecutor(2) as pool:
        first = pool.submit(listen, build, token, SESSION_1)
        until(lambda: parked(build, p.id))
        second = pool.submit(listen, build, token, SESSION_1)  # takes over
        assert first.result(30).status_code == 409
        until(lambda: parked(build, p.id))
        claude_hook(claude, event="UserPromptSubmit")  # the person typed: ends the second
        assert second.result(30).status_code == 409
    assert [x for x in caplog.records if x.name == "switchboard.agent_http"] == []


def test_the_join_over_https_says_the_stop_hook_wakes_an_idle_session(build: InProcBroker) -> None:
    """A join over HTTPS, for Claude Code and for Codex (#402), describes how the session is
    reached: its Stop hook waits on the broker and wakes it when idle, and while it works messages
    come after its tool calls. Fails if the join says switchboard can't wake the session, as the
    generic text of a harness with no push path does."""
    claude = Claude(build, make_token(build, "laptop"), SESSION_1)
    codex = Codex(build, make_token(build, "desk"))
    codex.start()
    joins = [
        claude.call("join", room="#build", screen_name="claude-1"),
        codex.call("join", THREAD_A, room="#build", screen_name="codex-1"),
    ]
    for r in joins:
        text = json.dumps(r)
        assert "can't wake" not in text
        assert "Stop hook waits" in text and "after your tool calls" in text
        assert f"switchboard {__version__}" in text  # the server's version, behind the token


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
    # the route's own guarantee that it never parks (F2 on #404's board): a park needs a Stop with
    # wait_s, and the route takes neither, whatever the engine does with a SessionStart
    assert "wait_s" not in agent_http.SESSION_FIELDS and "Stop" not in agent_http.SESSION_EVENTS
    r = session_hook(build, token, {"event": "SessionStart", "session_id": sid, "wait_s": "60"})
    assert r.status_code == 200 and not parked(build, participant(build, "claude-1").id), r.text
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


def test_a_session_someone_else_ended_hears_why_from_its_listener(build: InProcBroker) -> None:
    """An idle session over HTTPS that someone else ends (here a /kick) can't be told any other way,
    so its listener's next round answers 410 with why, which the listener prints once (#402). One
    that left on its own, or never joined, gets the usual 409, which wakes nothing. Fails if a kicked
    session goes deaf without a word, or a session that chose to leave is woken about it."""
    claude, token = joined_claude(build)
    build.call("human.command", {"room": "#build", "text": "/kick claude-1"})
    r = listen(build, token, SESSION_1)
    assert r.status_code == 410, r.text
    assert "claude-1 is no longer in #build: your user removed it" in r.text, r.text
    again = listen(build, token, SESSION_1, **{"x-switchboard-again": "1"})
    assert again.status_code == 410  # the same line: the listener keeps it from repeating
    two = Claude(build, token, "00000000-0000-4000-8000-0000000000aa")
    assert two.call("join", room="#build", screen_name="claude-2")["ok"]
    assert two.call("leave", room="#build")["ok"]
    assert listen(build, token, "00000000-0000-4000-8000-0000000000aa").status_code == 409
    assert listen(build, token, "00000000-0000-4000-8000-0000000000bb").status_code == 409  # never joined


def stop_hook_request(codex: Codex, thread: str, **fields: Any) -> tuple[int, dict[str, Any]]:
    """The request id and body of a Codex Stop hook's call of the hook tool, for a test that sends it
    itself and so can cancel it by id (``codex_hook`` sends it and waits)."""
    body = copy.deepcopy(CODEX_CALL["body"])
    body["id"] = next(codex.ids)
    body["params"]["name"] = "hook"
    body["params"]["arguments"] = {"session_id": thread, "event": "Stop", **fields}
    del body["params"]["_meta"]["threadId"]
    return body["id"], body


def cancel_call(codex: Codex, request_id: int, reason: str) -> None:
    """What Codex's MCP client sends when it gives up on a call: Esc, or its ``tool_timeout_sec``."""
    note = {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": request_id, "reason": reason},
    }
    r = codex.c.post("/mcp", json=note, headers=codex.headers(CODEX_CALL["headers"]))
    assert r.status_code == 202, r.text


def test_a_codex_whose_stop_hook_was_cancelled_is_shown_as_not_listening_until_its_next_hook(
    build: InProcBroker,
) -> None:
    """#449: a Codex whose MCP server has the default ``tool_timeout_sec`` gave up on its Stop hook after
    5 minutes ("Hook failed") and stopped listening, and Members showed only an idle session. The
    cancelled call closes the park, and Members now says the Stop hook ended, until the session's next
    hook. Fails if a cancelled Stop wait leaves the session looking as if it listens, or if the
    warning outlives the next hook."""
    codex = Codex(build, make_token(build, "laptop"))
    codex.start()
    assert codex.call("join", THREAD_A, room="#build", screen_name="codex-1")["ok"]
    codex.call("read", THREAD_A, room="#build")
    p = participant(build, "codex-1")
    rid, body = stop_hook_request(codex, THREAD_A, wait_s="60")
    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(
            lambda: codex.c.post("/mcp", json=body, headers=codex.headers(CODEX_CALL["headers"]))
        )
        until(lambda: parked(build, p.id))
        assert not row(build, "codex-1")["parked"]  # listening
        cancel_call(codex, rid, "request timeout")
        waiting.result(30)
    until(lambda: not parked(build, p.id))
    got = row(build, "codex-1")
    assert got["status"] == "idle" and got["parked"], got
    assert "not listening: its Stop hook ended" in got["parked_reason"], got
    assert "tool_timeout_sec" in got["parked_reason"], got
    # the person writes to it meanwhile: still nothing to wake it with, and it says why
    build.call("human.say", {"room": "#build", "text": "@codex-1 are you there?"})
    assert "Stop hook ended" in row(build, "codex-1")["parked_reason"]
    # its next hook (the person typed, or Codex ran another turn) ends the warning...
    codex_hook(codex, THREAD_A, event="UserPromptSubmit", turn_id="t-2")
    got = row(build, "codex-1")
    assert got["status"] == "busy" and "Stop hook ended" not in (got["parked_reason"] or ""), got
    # ...and the Stop hook after it is a wait again, which hands over what had waited
    reply = json.loads(codex_hook(codex, THREAD_A, event="Stop", wait_s="60", turn_id="t-2"))
    assert reply["decision"] == "block" and "are you there?" in reply["reason"], reply
