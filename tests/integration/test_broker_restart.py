"""Graceful restarts keep accepted says and end long waits (DESIGN.md §15, §49)."""

from __future__ import annotations

import asyncio
import json
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import InProcBroker, SubprocBroker
from fakes.fake_agent import FakeAgent, ids_in
from test_agents_http import EXIT_FIX, THREAD_A, Codex, make_token, sse_result, status
from test_agents_http_claude import SESSION_1, Claude
from test_agents_http_cursor import SID_A, invoke
from test_agents_http_wake import claude_hook, joined_claude, listen, parked, participant, until

from switchboard import db
from switchboard.clock import SystemClock
from switchboard.config import Config
from switchboard.store import Store


async def test_shutdown_posts_accepted_queued_says_once_in_order(tmp_home: Path) -> None:
    """A queued say promises not to resend: shutdown must store it before losing the queue."""
    b = InProcBroker(
        tmp_home, Config(human_name="alice").with_delivery(rate_limit_s=3600, quiet_s=0, max_hold_s=0)
    ).start()
    try:
        b.call("room.create", {"name": "#build"})
        async with FakeAgent(b.home, "writer") as writer:
            await writer.join("#build", "writer")
            assert (await writer.say("#build", "one"))["posted_id"]
            for text in ("two", "three", "four"):
                assert (await writer.say("#build", text))["queued"]
        await asyncio.to_thread(b.restart)
        async with FakeAgent(b.home, "reader") as reader:
            joined = await reader.join("#build", "reader")
            for text in ("one", "two", "three", "four"):
                assert text in joined["text"]
        web = b.web_client()
        try:
            messages = web.get("/api/rooms/build/messages").json()["messages"]
        finally:
            web.close()
        assert [m["text"] for m in messages if m["from"] == "writer" and m["kind"] == "chat"] == [
            "one",
            "two",
            "three",
            "four",
        ]
    finally:
        b.stop()


async def test_restart_answers_open_wait_and_preserves_queued_wake(tmp_home: Path) -> None:
    """Restart while a scripted agent waits and its peer sends: retry receives each queued post once."""
    b = InProcBroker(
        tmp_home, Config(human_name="alice").with_delivery(rate_limit_s=3600, quiet_s=0, max_hold_s=0)
    ).start()
    waiting = threading.Event()
    try:
        b.call("room.create", {"name": "#build"})
        async with FakeAgent(b.home, "writer") as writer, FakeAgent(b.home, "reader") as reader:
            await writer.join("#build", "writer")
            await reader.join("#build", "reader")
            await writer.say("#build", "one")
            await reader.read("#build")
            assert (await writer.say("#build", "two"))["queued"]
            original = b.state.runner.future_for

            def future_for(sink_id: int):
                future = original(sink_id)
                waiting.set()
                return future

            b.on_loop(setattr, b.state.runner, "future_for", future_for)
            task = reader.wait_task("#build", 100)
            try:
                assert await asyncio.to_thread(waiting.wait, 5), "wait never opened"
                # The server hook runs before Uvicorn drains these HTTP requests.
                await asyncio.to_thread(b.restart)
                result = await asyncio.wait_for(task, 5)
                assert result["ok"] is False and result["code"] == "restarting", result
                assert result["retry_after_s"] == 1
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        async with FakeAgent(b.home, "reader") as again:
            # Transport sessions are new, while the participant and pending delivery survived.
            answer = await again.wait("#build", 1)
            assert answer["status"] == "messages" and "two" in answer["text"], answer
            assert len(ids_in(answer["text"])) == 1
            assert (await again.read("#build"))["count"] == 0
    finally:
        b.stop()


@pytest.mark.parametrize("ending", ["leave", "close", "delete"])
async def test_shutdown_discards_queued_says_whose_membership_ended(
    broker: InProcBroker, ending: str
) -> None:
    """Flushing must keep the queue's existing rule: leaving or closing cancels accepted late posts."""
    broker.call("room.create", {"name": "#build"})
    async with FakeAgent(broker.home, "writer") as writer:
        await writer.join("#build", "writer")
        await writer.say("#build", "first")
        assert (await writer.say("#build", "cancelled post"))["queued"]
        if ending == "leave":
            await writer.leave("#build")
        elif ending == "close":
            broker.call("human.command", {"room": "#build", "text": "/close"})
        else:
            broker.call("human.command", {"room": "#build", "text": "/close"})
            plan = broker.call("room.delete", {"room": "#build", "dry_run": True})
            broker.call(
                "room.delete", {"room": "#build", **{k: plan[k] for k in ("room_id", "name", "created_at")}}
            )
    state = broker.state
    await asyncio.to_thread(broker.restart)
    assert not state.agents._say_timers and not state.agents._queued_says
    assert (
        broker.on_loop(
            lambda: broker.state.store.con.execute(
                "SELECT COUNT(*) FROM messages WHERE text='cancelled post'"
            ).fetchone()[0]
        )
        == 0
    )


@pytest.mark.parametrize("path", ["/mcp", "/mcp/listen", "/mcp/event"])
def test_draining_refuses_new_agent_requests(broker: InProcBroker, path: str) -> None:
    """Once shutdown flushed the queues, late requests cannot open another wait or queue another say."""
    broker.on_loop(broker.state.begin_shutdown)
    broker.on_loop(broker.state.begin_shutdown)  # harmless when called again by the lifespan
    response = httpx.post(broker.base + path)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "error": "restarting",
        "message": "switchboard is restarting; try again in a minute",
    }


@pytest.mark.parametrize("kind", ["claude", "cursor", "codex"])
def test_shutdown_releases_open_stop_polls(broker: InProcBroker, kind: str) -> None:
    """HTTP polls get 503; an already-open MCP SSE stream gets a tool error, never a hook decision."""
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker)
    opened = threading.Event()
    original = broker.state.runner.future_for

    def future_for(sink_id: int) -> Any:
        future = original(sink_id)
        opened.set()
        return future

    broker.on_loop(setattr, broker.state.runner, "future_for", future_for)
    client: Any
    if kind == "claude":
        client = Claude(broker, token, SESSION_1)
        assert client.call("join", room="#build", screen_name="worker")["ok"]

        def request() -> httpx.Response:
            return listen(broker, token, SESSION_1, wait=3600)
    elif kind == "codex":
        client = Codex(broker, token)
        assert client.start().status_code == 200
        assert client.call("join", THREAD_A, room="#build", screen_name="worker")["ok"]

        def request() -> httpx.Response:
            return client.raw("hook", THREAD_A, event="Stop", session_id=THREAD_A, wait_s="86390")
    else:
        client = Codex(broker, token, client="Cursor")
        assert client.start().status_code == 200
        assert (
            client.c.post(
                "/mcp/pretool",
                headers={"authorization": f"Bearer {token}"},
                json={
                    "harness": "cursor",
                    "event": "preToolUse",
                    "session_id": SESSION_1,
                    "tool_use_id": "join",
                    "tool_name": "switchboard_join",
                },
            ).status_code
            == 200
        )
        assert invoke(
            client,
            "switchboard_join",
            {
                "room": "#build",
                "screen_name": "worker",
                "switchboard_session_id": SESSION_1,
                "switchboard_tool_use_id": "join",
            },
        )["ok"]

        def request() -> httpx.Response:
            return client.c.post(
                "/mcp/event",
                headers={"authorization": f"Bearer {token}"},
                json={"harness": "cursor", "event": "stop", "session_id": SESSION_1, "wait_s": 630},
            )

    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(request)
            try:
                assert opened.wait(5), "Stop never parked"
                broker.restart()
                response: httpx.Response = pending.result(5)
            finally:
                # On a regression, close the request so the test never leaves a broker behind.
                if not pending.done():
                    broker.on_loop(lambda: broker.state.runner.execute(broker.state.engine.sink_timeout(1)))
            if kind == "codex":
                result = sse_result(response)["result"]
                assert result["isError"] is True
                assert "restarting" in result["content"][0]["text"]
            else:
                assert response.status_code == 503, response.text
                assert response.headers["retry-after"] == "1"
                assert response.json()["error"] == "restarting"
        assert broker.thread is not None and broker.thread.is_alive()
    finally:
        client.c.close()


async def test_sigterm_answers_wait_and_posts_queued_say(subproc_broker: SubprocBroker) -> None:
    """The real foreground server owns SIGTERM: its SSE library cannot cancel our restart answer first."""
    b = subproc_broker
    assert b.cli("create", "#build").returncode == 0
    async with FakeAgent(b.home, "signal") as agent:
        await agent.join("#build", "worker")
        await agent.say("#build", "first")
        assert (await agent.say("#build", "second"))["queued"]
        waiting = agent.wait_task("#build", 100)
        try:
            # Opening a wait changes this session to idle/sink in the same synchronous
            # engine call that registers its future. Read that event, never sleep "by now".
            con = db.connect(b.paths.db, readonly=True)
            try:
                store = Store(con, SystemClock())
                deadline = time.monotonic() + 5
                while True:
                    participant = store.find_participant("test", "test:signal")
                    if (
                        participant is not None
                        and participant.status == "idle"
                        and participant.status_src == "sink"
                    ):
                        break
                    assert time.monotonic() < deadline, "wait never opened"
                    await asyncio.sleep(0.01)
            finally:
                con.close()
            assert b.proc is not None
            b.proc.send_signal(signal.SIGTERM)
            result = await asyncio.wait_for(waiting, 5)
            assert result["ok"] is False and result["code"] == "restarting", result
            assert await asyncio.to_thread(b.proc.wait, 10) == 0
        finally:
            if not waiting.done():
                waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    con = db.connect(b.paths.db, readonly=True)
    try:
        store = Store(con, SystemClock())
        room = store.get_room("#build")
        assert room is not None
        assert [m.text for m in store.history(room.id) if m.kind == "chat"] == ["first", "second"]
    finally:
        con.close()


@pytest.mark.parametrize("approval", ["none", "before", "after"])
def test_listener_returns_after_restart_only_if_it_was_idle(broker: InProcBroker, approval: str) -> None:
    """An idle listener re-parks after restart. Approval reports on either side of the restart
    prevent the old listener round from waking the session."""
    broker.call("room.create", {"name": "#build"})
    claude, token = joined_claude(broker)
    pid = participant(broker, "claude-1").id
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(listen, broker, token, SESSION_1, 15)
        until(lambda: parked(broker, pid))
        if approval == "before":
            claude_hook(claude, event="Notification", notification_type="permission_prompt")
            assert participant(broker, "claude-1").status == "waiting-approval"
        broker.restart()
        assert pending.result(5).status_code == (409 if approval == "before" else 503)

    if approval == "after":
        claude_hook(claude, event="Notification", notification_type="permission_prompt")
    broker.call("human.say", {"room": "#build", "text": "@claude-1 after restarting"})
    headers = {"x-switchboard-again": "1"}
    if approval == "none":
        assert listen(broker, token, SESSION_1, 10, **headers).status_code == 409
    headers["x-switchboard-resume-idle"] = "1"
    response = listen(broker, token, SESSION_1, 10, **headers)
    if approval == "none":
        assert response.status_code == 200 and "after restarting" in response.text
    else:
        assert response.status_code == 409


def cursor_call(b: InProcBroker, c: Codex, token: str, use: str, tool: str, **args: Any) -> httpx.Response:
    """A Cursor tools/call as its hook and client make it: the hook's report, then the call with the IDs."""
    report = {"harness": "cursor", "event": "preToolUse", "session_id": SID_A, "tool_use_id": use}
    r = c.c.post(
        "/mcp/pretool", json={**report, "tool_name": tool}, headers={"authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200, r.text
    body = {
        "jsonrpc": "2.0",
        "id": 40,
        "method": "tools/call",
        "params": {
            "name": tool,
            "arguments": {**args, "switchboard_session_id": SID_A, "switchboard_tool_use_id": use},
        },
    }
    headers = c.headers(
        {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            "mcp-protocol-version": "2025-11-25",
        }
    )
    return c.c.post("/mcp", json=body, headers=headers, timeout=60)


def tool_json(r: httpx.Response) -> dict[str, Any]:
    assert r.status_code == 200, (r.status_code, r.text)
    return json.loads(sse_result(r)["result"]["content"][0]["text"])


def test_a_client_that_keeps_its_session_id_is_served_after_a_restart(broker: InProcBroker) -> None:
    """Cursor Agent 2026.11.19 keeps the Mcp-Session-Id it had and never initializes again after a 404
    (seen live, #823): every switchboard tool then fails in Cursor until it restarts. The broker must
    reopen the token's session under the same ID, as the client it was (Cursor's prefixed tools, its
    conversation from the hook), and keep answering with that ID."""
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker)
    c = Codex(broker, token, client="Cursor")
    assert c.start().status_code == 200
    sid = c.sid
    listed = c.tools()
    assert listed and all(name.startswith("switchboard_") for name in listed)  # Cursor's names
    joined = cursor_call(broker, c, token, "use-1", "switchboard_join", room="#build", screen_name="one")
    assert tool_json(joined)["ok"]
    broker.restart()
    assert c.tools() == listed  # the same client after the restart, not the plain names
    first = cursor_call(
        broker, c, token, "use-2", "switchboard_say", room="#build", text="back after the restart"
    )
    assert first.headers["mcp-session-id"] == sid
    assert tool_json(first)["ok"]
    again = cursor_call(broker, c, token, "use-3", "switchboard_who", room="#build")
    assert again.headers["mcp-session-id"] == sid and tool_json(again)["ok"]
    history = broker.call("room.history", {"room": "#build"})["messages"]
    assert [m["from"] for m in history if m["text"] == "back after the restart"] == ["one"]


def test_only_the_token_that_opened_a_session_can_reopen_it(broker: InProcBroker) -> None:
    """A session ID is no credential: after a restart another device's token presenting it gets the 404
    an unknown session gets, and the owner's session still reopens afterwards."""
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker, "laptop")
    mine = Codex(broker, token)
    assert mine.start().status_code == 200
    assert mine.call("join", THREAD_A, room="#build", screen_name="codex-1")["ok"]
    broker.restart()
    theirs = Codex(broker, make_token(broker, "desk"))
    theirs.sid = mine.sid
    assert theirs.raw("who", THREAD_A, room="#build").status_code == 404
    assert mine.call("who", THREAD_A, room="#build")["ok"]


def test_a_reopened_session_still_ends_its_threads_when_deleted(broker: InProcBroker) -> None:
    """Codex's DELETE at exit takes its threads offline (#449). After a restart that reopened its session,
    the threads its calls name are still tied to the ID it sends, so its DELETE still ends them; a session
    it deleted is forgotten for good and is not reopened by a later restart."""
    broker.call("room.create", {"name": "#build"})
    recorded = json.loads((EXIT_FIX / "mcp_exit_quit.json").read_text())
    codex = Codex(broker, make_token(broker, "laptop"))
    assert codex.start().status_code == 200
    assert codex.call("join", THREAD_A, room="#build", screen_name="codex-1")["ok"]
    broker.restart()
    assert codex.call("hook", THREAD_A, event="UserPromptSubmit", session_id=THREAD_A) == {}
    assert status(broker, "codex-1") == "busy"
    assert codex.delete(recorded).status_code == 200
    assert status(broker, "codex-1") == "offline"
    broker.restart()
    assert codex.raw("who", THREAD_A, room="#build").status_code == 404
