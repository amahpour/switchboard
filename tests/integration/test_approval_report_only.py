"""A report-only PermissionRequest hook for Codex and Devin (#472, DESIGN.md §11, §49.9).

Codex and Devin run a PermissionRequest hook when they are about to ask their person to approve a tool,
and in both a hook's output there can approve or deny (live, Codex 0.158.0: an "allow" came back and the
command ran with no prompt). The maintainer said yes, on 2026-10-21, to a hook that only reports and
"can never approve or deny anything". Two doors make that so: the plugin's hook is a command that discards
whatever the server replies (tests/unit/test_permission_hooks_static.py), and the server answers the report
with nothing, in every state, and refuses the report from the one kind of hook whose reply would be the
output (an mcp_tool hook). These tests are the server's half.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from conftest import InProcBroker
from test_agents_http import THREAD_A, Codex, make_token
from test_agents_http_claude import SESSION_1, Claude
from test_agents_http_devin import Devin
from test_agents_http_wake import claude_hook, codex_hook, member, participant, row, set_status

from switchboard.broker.agent_http import HttpConn
from switchboard.broker.agents import AgentConn
from switchboard.models import HookOut

DEVIN_SESSION = "session-perm"


@pytest.fixture
def build(broker: InProcBroker) -> InProcBroker:
    broker.call("room.create", {"name": "#build"})
    return broker


def report(
    b: InProcBroker, token: str, harness: str, session: str, event: str = "PermissionRequest", **extra: Any
) -> httpx.Response:
    """What the plugins' report-only hooks send, at /mcp/event."""
    body = {"harness": harness, "event": event, "session_id": session, **extra}
    return httpx.post(b.base + "/mcp/event", json=body, headers={"authorization": f"Bearer {token}"})


def joined_codex(b: InProcBroker) -> tuple[Codex, str]:
    token = make_token(b, "laptop")
    codex = Codex(b, token)
    codex.start()
    assert codex.call("join", THREAD_A, room="#build", screen_name="codex-1")["ok"]
    codex.call("read", THREAD_A, room="#build")
    return codex, token


def joined_devin(b: InProcBroker) -> tuple[Devin, str]:
    token = make_token(b, "laptop")
    dv = Devin(b, token)
    joined = dv.call("join", dv.pre(DEVIN_SESSION, "call-join", {"room": "#build", "screen_name": "dev-1"}))
    assert joined["ok"], joined
    return dv, token


def test_codexs_command_hook_shows_the_thread_as_waiting_and_is_answered_with_nothing(
    build: InProcBroker,
) -> None:
    """The end of Codex's PermissionRequest command hook (the plugin's ``bin/approval``): its report
    makes the thread ``waiting-approval`` in Members and in another agent's ``who`` (which says what that
    means and leaves the choice to the reader), the answer is exactly ``{}``, a message for it waits, and
    the thread's next hook (the tool ran) clears the status and carries the message. Fails if the report
    sets nothing, the answer has anything in it, or a message is offered into the prompt."""
    codex, token = joined_codex(build)
    other = Claude(build, make_token(build, "other"), SESSION_1)
    assert other.call("join", room="#build", screen_name="claude-2")["ok"]
    codex_hook(codex, THREAD_A, event="UserPromptSubmit", turn_id="turn-1", permission_mode="default")
    assert row(build, "codex-1")["status"] == "busy"
    r = report(build, token, "codex", THREAD_A)
    assert (r.status_code, r.text) == (200, "{}"), (r.status_code, r.text)
    assert row(build, "codex-1")["status"] == "waiting-approval"
    line = next(x for x in other.call("who", room="#build")["text"].splitlines() if "codex-1" in x)
    assert "status=waiting-approval" in line and "approval prompt" in line and "your call" in line
    build.call("human.say", {"room": "#build", "text": "@codex-1 look at the tests"})
    assert member(build, "codex-1").queued == 1 and member(build, "codex-1").inflight == 0
    after = codex_hook(
        codex, THREAD_A, event="PostToolUse", tool_name="Bash", tool_use_id="call-1", turn_id="turn-1"
    )
    assert row(build, "codex-1")["status"] == "busy"
    assert "look at the tests" in after


def test_devins_command_hook_shows_the_session_as_waiting_and_is_answered_with_nothing(
    build: InProcBroker,
) -> None:
    """Devin's PermissionRequest command hook (``plugins/devin/bin/hook permission``) reports through the
    same door: waiting-approval, the answer exactly ``{}``, a message held, and the session's next hook
    ends it. Fails if the report sets nothing or is answered with anything."""
    dv, token = joined_devin(build)
    r = report(build, token, "devin", DEVIN_SESSION, event="UserPromptSubmit", prompt_id="p-1")
    assert (r.status_code, r.text) == (200, "{}")
    assert row(build, "dev-1")["status"] == "busy"
    r = report(build, token, "devin", DEVIN_SESSION)
    assert (r.status_code, r.text) == (200, "{}"), (r.status_code, r.text)
    assert row(build, "dev-1")["status"] == "waiting-approval"
    build.call("human.say", {"room": "#build", "text": "@dev-1 look at the tests"})
    assert member(build, "dev-1").queued == 1 and member(build, "dev-1").inflight == 0
    report(build, token, "devin", DEVIN_SESSION, event="UserPromptSubmit", prompt_id="p-2")
    assert row(build, "dev-1")["status"] == "busy"
    dv.c.close()


STATES = ["starting", "idle", "busy", "waiting-approval", "offline"]


@pytest.mark.parametrize("status", STATES)
@pytest.mark.parametrize("harness", ["codex", "devin"])
def test_the_report_is_answered_with_nothing_in_every_state_with_a_message_waiting(
    build: InProcBroker, harness: str, status: str
) -> None:
    """The reply to a PermissionRequest report is the bytes ``{}`` -- no context, no continue, no
    decision, no batch to acknowledge -- whatever state the session is in and whatever waits for it (a
    priority message from the person, an @mention). A harness reads a PermissionRequest hook's output as a
    permission decision, so this is pinned by the shape of the whole reply, not by a field. Fails if the
    reply is ever anything else, or the message that waited is offered by it."""
    if harness == "codex":
        _agent, token = joined_codex(build)
        name, session = "codex-1", THREAD_A
    else:
        _agent, token = joined_devin(build)
        name, session = "dev-1", DEVIN_SESSION
    set_status(build, name, status)
    build.call("human.say", {"room": "#build", "text": f"@{name} an urgent message"})
    r = report(build, token, harness, session)
    assert (r.status_code, r.text) == (200, "{}"), (status, r.status_code, r.text)
    assert r.headers["content-type"].startswith("application/json")
    assert member(build, name).queued == 1 and member(build, name).inflight == 0


def test_the_report_reads_the_session_and_nothing_else_of_the_request(build: InProcBroker) -> None:
    """The script sends the session id and nothing else, and the server would read nothing else: a body
    that carries the tool, its input, a notification type or a batch to acknowledge changes nothing but the
    status. Fails if a field of the request other than the session id reaches the engine (a ``tool``
    would anchor a turn; a notification type would count)."""
    _codex, token = joined_codex(build)
    before = participant(build, "codex-1")
    r = report(
        build, token, "codex", THREAD_A,
        tool_name="Bash", tool_input={"command": "rm -rf /"}, notification_type="permission_prompt",
        turn_id="turn-x", tokens=[[1, "abcdef12"]], ok=True, permission_mode="bypassPermissions",
    )  # fmt: skip
    assert (r.status_code, r.text) == (200, "{}")
    after = participant(build, "codex-1")
    assert after.status == "waiting-approval"
    assert (after.gen, after.approval_mode) == (before.gen, before.approval_mode)


def test_a_report_from_a_session_that_never_joined_changes_nothing(build: InProcBroker) -> None:
    """A hook of a session that isn't in a room is inert, as every hook's is: the answer is still
    ``{}``. Fails if a report creates or revives a session."""
    token = make_token(build, "laptop")
    for harness in ("codex", "devin"):
        r = report(build, token, harness, "never-joined")
        assert (r.status_code, r.text) == (200, "{}"), harness


def test_codexs_command_hook_gets_the_permission_request_and_nothing_else(build: InProcBroker) -> None:
    """Codex has no PreToolUse hook here and no other command hook: its one command route takes the
    PermissionRequest report. Any other event, or its use of /mcp/pretool, is refused (400), so the door
    cannot be used to set a status another way, confirm a batch or end a session. Fails if a Codex
    command report is taken for another event."""
    _codex, token = joined_codex(build)
    for event in ("UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd", "SessionStart", "PreToolUse", ""):
        r = report(build, token, "codex", THREAD_A, event=event)
        assert r.status_code == 400, (event, r.status_code)
    r = httpx.post(
        build.base + "/mcp/pretool",
        json={"harness": "codex", "event": "PreToolUse", "session_id": THREAD_A,
              "tool_use_id": "call-1", "tool_name": "say"},
        headers={"authorization": f"Bearer {token}"},
    )  # fmt: skip
    assert r.status_code == 400
    for other in ("cursor", "claude", "test"):  # and Cursor has no such hook, Claude Code reports another way
        assert report(build, token, other, THREAD_A).status_code == 400, other
    assert row(build, "codex-1")["status"] != "waiting-approval"


def test_the_report_needs_an_agent_token_and_no_browser(build: InProcBroker) -> None:
    """As every agent door: no token or a wrong one is 401, a request a browser could send (an Origin) is
    403. Fails if the report can be made without a token."""
    token = make_token(build, "laptop")
    body = {"harness": "codex", "event": "PermissionRequest", "session_id": THREAD_A}
    assert httpx.post(build.base + "/mcp/event", json=body).status_code == 401
    bad = {"authorization": "Bearer sbat_" + "0" * 40}
    assert httpx.post(build.base + "/mcp/event", json=body, headers=bad).status_code == 401
    ok = {"authorization": f"Bearer {token}", "origin": "http://switchboard.localhost"}
    assert httpx.post(build.base + "/mcp/event", json=body, headers=ok).status_code == 403


def test_an_mcp_tool_hook_cannot_report_a_permission_request(build: InProcBroker) -> None:
    """An mcp_tool hook's output is the server's reply to the hook tool, and for a PermissionRequest that
    is a decision (Codex 0.158.0, live). So the server takes the report from no mcp_tool hook: the hook
    tool answers ``{}`` and does nothing, for Codex, for Claude Code (which has no such hook) and for the
    event spelled any way. Fails if the hook tool sets a status from a PermissionRequest, which would be a
    door whose reply could one day be a decision."""
    codex, _token = joined_codex(build)
    claude = Claude(build, make_token(build, "laptop2"), SESSION_1)
    assert claude.call("join", room="#build", screen_name="claude-1")["ok"]
    claude.call("read", room="#build")
    codex_hook(codex, THREAD_A, event="UserPromptSubmit", turn_id="turn-1")
    claude_hook(claude, event="UserPromptSubmit")
    assert codex_hook(codex, THREAD_A, event="PermissionRequest") == "{}"
    assert claude_hook(claude, event="PermissionRequest") == "{}"
    assert row(build, "codex-1")["status"] == "busy" and row(build, "claude-1")["status"] == "busy"


@pytest.mark.parametrize(
    ("harness", "event", "params"),
    [
        ("codex", "PermissionRequest", {}),
        ("devin", "PermissionRequest", {}),
        ("claude", "Notification", {"ntype": "permission_prompt"}),
    ],
)
def test_whatever_the_engine_hands_back_a_report_is_answered_with_nothing(
    build: InProcBroker, monkeypatch: pytest.MonkeyPatch, harness: str, event: str, params: dict[str, str]
) -> None:
    """The second lock: should the engine ever give a report-only hook an output (a context, a continue, a
    park, a batch), ``AgentService.http_hook`` drops it and acknowledges nothing. Forced here by making the
    engine answer every hook with a context. Fails if the output of a PermissionRequest or a permission
    Notification can get past the service, which is where a reply is built from (§49.4)."""
    if harness == "codex":
        _agent, _token = joined_codex(build)
        sid, name = THREAD_A, "codex-1"
    elif harness == "devin":
        _agent, _token = joined_devin(build)
        sid, name = DEVIN_SESSION, "dev-1"
    else:
        claude = Claude(build, make_token(build, "laptop"), SESSION_1)
        assert claude.call("join", room="#build", screen_name="claude-1")["ok"]
        sid, name = SESSION_1, "claude-1"
    engine = build.state.engine
    real = engine.claim_for_hook

    def hostile(p: Any, ev: Any) -> Any:
        _out, acts = real(p, ev)
        return HookOut("context", "approve this", batch_id=7, ack="x"), acts

    monkeypatch.setattr(engine, "claim_for_hook", hostile)
    agent = AgentConn(harness=harness, host="laptop")
    res = asyncio.run_coroutine_threadsafe(
        build.state.agents.http_hook(HttpConn(1, "laptop", agent), harness, event, {"sid": sid, **params}),
        build.loop,  # type: ignore[arg-type]
    ).result(10)
    assert res == {"out": None}
    assert row(build, name)["status"] == "waiting-approval"  # and the report itself still counted
