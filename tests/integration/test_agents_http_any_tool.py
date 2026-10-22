"""Cursor and Devin over HTTPS hear a message after any tool call (#493, DESIGN.md §49.6, §49.7).

Their tool hooks matched switchboard's own tools only, so an agent that worked with a shell and its files
heard nothing until its next switchboard call. Now a second group runs after every other tool, and its
script reports the event, the session, the call's id and the tool's name (never what the tool did), and the
broker answers it with the waiting priority messages as context, as it does for Claude Code and Codex
(#422). Driven with the real scripts and the harnesses' recorded payloads against an in-process broker.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import InProcBroker
from test_agents_http import Codex, make_token
from test_agents_http_cursor import POST as CURSOR_JOIN_POST
from test_agents_http_cursor import SID_A, SID_B, invoke, run_hook
from test_agents_http_devin import Devin, open_waits
from test_agents_http_wake import member, participant, row, until

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "payloads"
SECRET = "SECRET-OF-THE-TOOL-9f3a"


def recorded(name: str, **given: Any) -> dict[str, Any]:
    """A harness's recorded payload (the ``_`` notes dropped), with the given fields set."""
    doc = {k: v for k, v in json.loads((FIX / name).read_text()).items() if not k.startswith("_")}
    return {**doc, **given}


def deliveries(b: InProcBroker, name: str) -> list[dict[str, Any]]:
    """The member's deliveries, each with the path of the batch that carried it."""
    st = b.state
    pid = participant(b, name).id
    out = []
    for m in b.on_loop(st.store.participant_memberships, pid):
        for d in b.on_loop(st.store.deliveries, m.id):
            batch = b.on_loop(st.store.get_batch, d["batch_id"]) if d["batch_id"] else None
            out.append(
                {**d, "path": batch.path if batch else None, "batch_state": batch.state if batch else None}
            )
    return out


def table_counts(b: InProcBroker) -> list[int]:
    return b.on_loop(
        lambda: [
            b.state.store.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("participants", "memberships", "events", "batches", "deliveries")
        ]
    )


# ------------------------------------------------------------------- Cursor
def cursor_pre(broker: InProcBroker, token: str, sid: str, use: str, tool: str, args: dict[str, Any]) -> Any:
    payload = {
        "conversation_id": sid,
        "session_id": sid,
        "hook_event_name": "preToolUse",
        "tool_name": f"MCP:{tool}",
        "tool_use_id": use,
        "tool_input": args,
    }
    return json.loads(run_hook(broker, token, "pre", payload))["updated_input"]


def joined_cursor(broker: InProcBroker) -> tuple[Codex, str]:
    """A Cursor conversation in #build as ``cur``, busy: its join call's postToolUse report ran."""
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker)
    c = Codex(broker, token, client="Cursor")
    assert c.start().status_code == 200
    args = cursor_pre(
        broker, token, SID_A, "use-join", "switchboard_join", {"room": "#build", "screen_name": "cur"}
    )
    assert invoke(c, "switchboard_join", args)["ok"]
    post = {k: v for k, v in CURSOR_JOIN_POST.items() if not k.startswith("_")}
    post.update(
        conversation_id=SID_A, session_id=SID_A, tool_name="MCP:switchboard_join", tool_use_id="use-join"
    )
    run_hook(broker, token, "post", post)
    assert participant(broker, "cur").status == "busy"
    return c, token


def test_a_busy_cursor_hears_a_message_after_a_shell_call_and_after_a_failed_one(
    broker: InProcBroker,
) -> None:
    """The issue's case: a Cursor that works with a shell only. The member is busy, an @mention waits, and
    the answer of the plugin's script after the next Shell call (the recorded Cursor Agent 2026.10.18
    payload, run through `bin/hook post-other`) carries it as `additional_context`: path `hook_ctx`, the
    delivery in context and its batch confirmed. A failing command (`postToolUseFailure`, through
    `failure-other`) and a Read do the same for the next message. Fails where the plugin has no such
    hook, the broker needs a result in the report, or the reply is in another shape."""
    c, token = joined_cursor(broker)
    broker.call("human.say", {"room": "#build", "text": "@cur stop and check the tests"})
    assert member(broker, "cur").queued == 1
    shell = recorded("cursor/https_postToolUse_shell.json", conversation_id=SID_A, session_id=SID_A)
    reply = json.loads(run_hook(broker, token, "post-other", shell))
    assert set(reply) == {"additional_context"} and "check the tests" in reply["additional_context"], reply
    [d] = deliveries(broker, "cur")
    assert (d["state"], d["path"], d["batch_state"]) == ("in_context", "hook_ctx", "confirmed"), d
    assert member(broker, "cur").queued == 0
    for kind, name in (
        ("failure-other", "cursor/https_postToolUseFailure_shell.json"),
        ("post-other", "cursor/https_postToolUse_read.json"),
    ):
        broker.call("human.say", {"room": "#build", "text": f"@cur again, after {kind}"})
        payload = recorded(name, conversation_id=SID_A, session_id=SID_A)
        reply = json.loads(run_hook(broker, token, kind, payload))
        assert f"after {kind}" in reply["additional_context"], (kind, reply)
    assert [d["path"] for d in deliveries(broker, "cur")] == ["hook_ctx"] * 3
    c.c.close()


def test_a_cursors_other_tool_report_is_nothing_for_a_conversation_that_is_in_no_room(
    broker: InProcBroker,
) -> None:
    """Every tool call of every Cursor conversation now reports, in a room or not, so a conversation that
    never joined must cost nothing: the script prints nothing, and no participant, membership, event, batch
    or delivery is written. The same holds for a subagent, whose tool hooks carry a conversation id of its
    own (Cursor Agent 2026.10.18, recorded live): it takes nothing meant for the conversation that started
    it, and the conversation hears its message at its own next tool. Fails if a report can create or touch a
    participant, or a subagent's call takes the parent's message."""
    c, token = joined_cursor(broker)
    before = table_counts(broker)
    for kind, name in (
        ("post-other", "cursor/https_postToolUse_shell.json"),
        ("failure-other", "cursor/https_postToolUseFailure_shell.json"),
    ):
        payload = recorded(name, conversation_id=SID_B, session_id=SID_B)
        assert run_hook(broker, token, kind, payload) == ""
    assert table_counts(broker) == before
    broker.call("human.say", {"room": "#build", "text": "@cur hold on"})
    before = table_counts(broker)
    shell = recorded("cursor/https_postToolUse_shell.json", conversation_id=SID_B, session_id=SID_B)
    assert run_hook(broker, token, "post-other", shell) == ""  # the subagent's tool
    assert member(broker, "cur").queued == 1 and table_counts(broker) == before
    own = recorded("cursor/https_postToolUse_shell.json", conversation_id=SID_A, session_id=SID_A)
    assert "hold on" in json.loads(run_hook(broker, token, "post-other", own))["additional_context"]
    c.c.close()


@pytest.mark.parametrize(
    ("harness", "event", "tool"),
    [
        ("cursor", "postToolUse", "MCP:switchboard_say"),
        ("devin", "PostToolUse", "mcp__switchboard_https__say"),
    ],
)
def test_a_switchboard_tools_report_without_its_result_is_dropped_for_cursor_and_devin(
    broker: InProcBroker, harness: str, event: str, tool: str
) -> None:
    """Devin's group for every tool also runs after switchboard's own tools (its matcher can't leave them
    out), and a project that merged Cursor's groups by hand does too. The plugins' scripts send nothing for
    those, and the broker drops the report anyway if it comes with no result (as for Codex, #422): it answers
    {} and leaves the waiting message for the report that has the result. A result that is empty is still a
    result. Fails if the report without one takes the context, or an empty result is dropped with it."""
    if harness == "cursor":
        c, token = joined_cursor(broker)
        name, sid = "cur", SID_A
        c.c.close()
    else:
        dv, sid = joined_devin(broker, "session-dup")
        token, name = dv.token, "dev"
        dv.c.close()
    broker.call("human.say", {"room": "#build", "text": f"@{name} look at the build"})
    assert member(broker, name).queued == 1

    def report(**extra: Any) -> dict[str, Any]:
        body = {
            "harness": harness,
            "event": event,
            "session_id": sid,
            "tool_use_id": "u1",
            "tool_name": tool,
            **extra,
        }
        r = httpx.post(f"{broker.base}/mcp/event", json=body, headers={"authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        return dict(r.json())

    assert report() == {}
    assert member(broker, name).queued == 1  # still waiting
    assert "look at the build" in json.dumps(report(tool_response=""))
    assert member(broker, name).queued == 0


# -------------------------------------------------------------------- Devin
def joined_devin(broker: InProcBroker, sid: str = "session-busy") -> tuple[Devin, str]:
    """A Devin in #build as ``dev``, busy: its join call's PostToolUse report ran."""
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker)
    dv = Devin(broker, token)
    assert dv.call("join", dv.pre(sid, "call-join", {"room": "#build", "screen_name": "dev"}))["ok"]
    dv.post(sid, "call-join", "join", "ok")
    assert participant(broker, "dev").status == "busy"
    return dv, sid


def devin_tool(sid: str, **given: Any) -> dict[str, Any]:
    return recorded("devin/https_PostToolUse_exec.json", session_id=sid, **given)


def test_a_busy_devin_hears_a_message_after_a_shell_call(broker: InProcBroker) -> None:
    """The same for Devin CLI 3000.11.92: the member is busy, an @mention waits, and the script's answer
    after the next `exec` (its recorded payload, through `bin/hook post-other`) carries it as a PostToolUse
    `additionalContext`, path `hook_ctx`, confirmed. A session in no room costs nothing: no report of its
    tools writes a row. Fails where the template has no group for other tools, the broker needs a result in
    the report, or the reply is in another shape."""
    dv, sid = joined_devin(broker)
    broker.call("human.say", {"room": "#build", "text": "@dev stop and check the tests"})
    assert member(broker, "dev").queued == 1
    reply = json.loads(dv.run_hook("post-other", devin_tool(sid)).stdout)
    hso = reply["hookSpecificOutput"]
    assert set(reply) == {"hookSpecificOutput"} and hso["hookEventName"] == "PostToolUse", reply
    assert "check the tests" in hso["additionalContext"]
    [d] = deliveries(broker, "dev")
    assert (d["state"], d["path"], d["batch_state"]) == ("in_context", "hook_ctx", "confirmed"), d
    before = table_counts(broker)
    assert dv.run_hook("post-other", devin_tool("session-never-joined")).stdout == ""
    assert table_counts(broker) == before
    dv.c.close()


def test_a_devins_approval_hold_ends_at_the_tool_that_ran_and_that_hook_carries_what_waited(
    broker: InProcBroker,
) -> None:
    """Devin's PermissionRequest hook shows the session as waiting for approval (§49.9), and nothing is
    delivered to a session that is. Its tool hooks used to run for switchboard's own tools only, so after
    the person approved a shell command nothing of Devin's reached the broker until the next switchboard
    call or the Stop: a long approved command, then many other tools, kept "waiting for approval" for the
    whole turn. The report after the approved tool ends it, at that hook: the status is busy again and the
    message that waited comes in that hook's answer. Meanwhile, with the prompt open, a message waits.
    Fails if the status stays until Stop, or a message is delivered while the prompt is open."""
    dv, sid = joined_devin(broker)
    permission = recorded("devin/PermissionRequest_exec.json", session_id=sid)
    assert dv.run_hook("permission", permission).stdout == ""
    assert row(broker, "dev")["status"] == "waiting-approval"
    broker.call("human.say", {"room": "#build", "text": "@dev check the tests when you can"})
    assert row(broker, "dev")["status"] == "waiting-approval"
    assert member(broker, "dev").queued == 1  # not delivered into an open prompt
    reply = json.loads(dv.run_hook("post-other", devin_tool(sid)).stdout)
    assert "check the tests" in reply["hookSpecificOutput"]["additionalContext"]
    assert row(broker, "dev")["status"] == "busy"
    dv.c.close()


def test_a_tool_of_the_devin_itself_or_its_subagent_does_not_end_its_open_wait(broker: InProcBroker) -> None:
    """Devin listens in wait(), and a background subagent works meanwhile: its tool calls report with the
    main agent's session id (Devin's hooks carry no marker of a subagent, recorded live). If each report
    ended the wait, the listening Devin would be told its session "moved on" by its own helper, over and
    over, and a message would find no wait open. The wait stays open through the reports, and the message
    arrives as the wait's answer. Fails if a report of another tool ends the wait."""
    dv, sid = joined_devin(broker)
    args = dv.pre(sid, "call-wait", {"room": "#build", "timeout_s": 30}, "wait")
    with ThreadPoolExecutor(1) as pool:
        got = pool.submit(dv.call, "wait", args)
        until(lambda: open_waits(broker, "dev") == 1)
        for i in range(3):
            assert dv.run_hook("post-other", devin_tool(sid, tool_use_id=f"call-sub-{i}")).stdout == ""
        assert open_waits(broker, "dev") == 1
        broker.call("human.say", {"room": "#build", "text": "@dev time to review"})
        answer = got.result(60)
    assert answer["ok"] and "time to review" in json.dumps(answer), answer
    dv.c.close()
