"""Graceful restarts keep accepted says and end long waits (DESIGN.md §15, §49)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
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
from test_agents_http import EXIT_FIX, THREAD_A, THREAD_B, Codex, make_token, sse_result, status
from test_agents_http_claude import SESSION_1, Claude
from test_agents_http_cursor import SID_A, invoke
from test_agents_http_wake import claude_hook, joined_claude, listen, parked, participant, until

from switchboard import db
from switchboard.broker import agent_tokens
from switchboard.broker.agent_http import REOPEN_IDLE_S
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
    row = broker.state.store.agent_token_by_hash(agent_tokens.token_hash(token))
    key = hashlib.sha256(sid.encode()).hexdigest()
    before = broker.state.store.mcp_session(row.id, key, REOPEN_IDLE_S)
    assert before is not None and before.client_name == "Cursor"
    broker.restart()
    assert c.tools() == listed  # the same client after the restart, not the plain names
    after = broker.state.store.mcp_session(row.id, key, REOPEN_IDLE_S)
    assert after is not None and after.opened_at > before.opened_at  # its month starts again
    first = cursor_call(
        broker, c, token, "use-2", "switchboard_say", room="#build", text="back after the restart"
    )
    assert first.headers["mcp-session-id"] == sid
    assert tool_json(first)["ok"]
    again = cursor_call(broker, c, token, "use-3", "switchboard_who", room="#build")
    assert again.headers["mcp-session-id"] == sid and tool_json(again)["ok"]
    broker.restart()  # a second restart reopens the same ID again, not the one the first reopen made
    second = cursor_call(broker, c, token, "use-4", "switchboard_who", room="#build")
    assert second.headers["mcp-session-id"] == sid and tool_json(second)["ok"]
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


# ---------------------------------------------------------------- review findings on #903 (§49.2)


def token_row(b: InProcBroker, token: str) -> Any:
    return b.on_loop(b.state.store.agent_token_by_hash, agent_tokens.token_hash(token))


def library_sessions(b: InProcBroker) -> set[str]:
    """The session IDs the MCP library itself holds (its manager's own map), read on the broker's loop."""
    return b.on_loop(lambda: set(b.state.agent_auth.app.session_manager._server_instances))


def opener(
    b: InProcBroker, token: str, name: Any, protocol: str = "2025-06-18"
) -> tuple[httpx.Response, str]:
    """A fresh initialize with this clientInfo name and protocol, then its notifications/initialized."""
    c = Codex(b, token)
    body = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": protocol,
            "capabilities": {},
            "clientInfo": {"name": name, "version": "1"},
        },
    }
    headers = c.headers({"accept": "application/json, text/event-stream", "content-type": "application/json"})
    r = c.c.post("/mcp", json=body, headers=headers)
    assert r.status_code == 200, (r.status_code, r.text)
    sid = r.headers["mcp-session-id"]
    negotiated = sse_result(r)["result"]["protocolVersion"]
    done = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    headers = {**headers, "mcp-session-id": sid, "mcp-protocol-version": negotiated}
    assert c.c.post("/mcp", json=done, headers=headers).status_code == 202
    return r, sid


def test_another_tokens_live_session_is_refused_before_a_restart(broker: InProcBroker) -> None:
    """A session ID is no credential even while its library session is alive: another token sending it gets
    the 404 an unknown session gets, and the owner is still served."""
    broker.call("room.create", {"name": "#build"})
    mine = Codex(broker, make_token(broker, "laptop"))
    assert mine.start().status_code == 200
    assert mine.sid in library_sessions(broker)
    theirs = Codex(broker, make_token(broker, "desk"))
    theirs.sid = mine.sid
    assert theirs.raw("who", THREAD_A, room="#build").status_code == 404
    assert mine.call("join", THREAD_A, room="#build", screen_name="codex-1")["ok"]


def test_a_reopened_sessions_library_id_is_unknown_to_every_token(broker: InProcBroker) -> None:
    """After a reopen the library knows the session by a new ID of its own, which is no key of the broker's
    map of who owns what. Sent directly, by its own token or another's, it must be refused before it reaches
    the library; only the public ID the client owns reaches it. Fails if an ID the broker can't attribute to
    the token is forwarded (#903, F1)."""
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker, "laptop")
    mine = Codex(broker, token)
    assert mine.start().status_code == 200
    assert mine.call("join", THREAD_A, room="#build", screen_name="codex-1")["ok"]
    broker.restart()
    assert mine.call("who", THREAD_A, room="#build")["ok"]  # reopened
    inner = broker.on_loop(lambda: broker.state.agent_auth.sessions[mine.sid].inner)
    assert inner is not None and inner != mine.sid and inner in library_sessions(broker)
    for other in (token, make_token(broker, "desk")):
        direct = Codex(broker, other)
        direct.sid = inner
        assert direct.raw("who", THREAD_A, room="#build").status_code == 404
        assert (
            direct.c.get("/mcp", headers=direct.headers({"accept": "text/event-stream"})).status_code == 404
        )
    assert mine.call("who", THREAD_A, room="#build")["ok"]


def test_an_evicted_session_with_no_record_is_refused(broker: InProcBroker, monkeypatch: Any) -> None:
    """Past ``SESSIONS_MAX`` the broker forgets its oldest session while the library may keep it. With no
    record to open it again by, its ID is one the broker can't attribute, so it gets a 404 and never reaches
    the still-living library session. Fails if it is forwarded (#903, F1)."""
    from switchboard.broker import agent_http

    monkeypatch.setattr(agent_http, "SESSIONS_MAX", 1)
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker, "laptop")
    old = Codex(broker, token)
    assert old.start().status_code == 200
    row = token_row(broker, token)
    broker.on_loop(
        broker.state.store.mcp_session_forget, row.id, hashlib.sha256(old.sid.encode()).hexdigest()
    )
    new = Codex(broker, token)
    assert new.start().status_code == 200  # evicts the old one from the broker's map
    assert old.sid in library_sessions(broker)
    assert old.raw("who", THREAD_A, room="#build").status_code == 404
    assert new.call("join", THREAD_A, room="#build", screen_name="codex-1")["ok"]


def pause_inner(b: InProcBroker, when: str, hold: threading.Event, at: threading.Event) -> None:
    """Make the broker's handshake stop after it sent ``when`` (a JSON-RPC method) until ``hold`` is set,
    telling the test through ``at``: the test controls the moment, no sleep."""
    auth = b.state.agent_auth
    real = auth._inner

    async def paused(scope: Any, message: Any, session_id: Any, protocol: Any) -> Any:
        answer = await real(scope, message, session_id, protocol)
        if isinstance(message, dict) and message.get("method") == when:
            at.set()
            assert await asyncio.to_thread(hold.wait, 30)
        return answer

    auth._inner = paused


def test_a_delete_during_a_reopen_wins_and_leaves_no_library_session(broker: InProcBroker) -> None:
    """A reopen reads the record, then awaits two library calls. A DELETE of the same ID in between removes
    the record: the reopen must not bind the session after it, and must end the library session it started.
    The race is made with an event the test controls. Fails if the session comes back after its client
    deleted it, or an orphan library session is left (#903, F2)."""
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker, "laptop")
    recorded = json.loads((EXIT_FIX / "mcp_exit_quit.json").read_text())
    mine = Codex(broker, token)
    assert mine.start().status_code == 200
    assert mine.call("join", THREAD_A, room="#build", screen_name="codex-1")["ok"]
    broker.restart()
    assert library_sessions(broker) == set()
    hold, at = threading.Event(), threading.Event()
    pause_inner(broker, "initialize", hold, at)
    with ThreadPoolExecutor(1) as pool:
        call = pool.submit(mine.raw, "who", THREAD_A, room="#build")
        assert at.wait(30)
        deleter = Codex(broker, token)
        deleter.sid = mine.sid
        assert deleter.delete(recorded).status_code == 404  # unknown to the broker: its record goes
        hold.set()
        assert call.result(30).status_code == 404
    until(lambda: library_sessions(broker) == set())
    assert mine.sid not in broker.on_loop(lambda: set(broker.state.agent_auth.sessions))
    row = token_row(broker, token)
    key = hashlib.sha256(mine.sid.encode()).hexdigest()
    assert broker.on_loop(broker.state.store.mcp_session, row.id, key, REOPEN_IDLE_S) is None


def test_a_failed_reopen_leaves_no_library_session(broker: InProcBroker) -> None:
    """The library session the broker's own initialize opened is ended when the rest of the handshake fails,
    so none stays alive that nothing can reach; the record stays, so the next request can try again. Fails if
    the failed reopen leaves its library session (#903, F2)."""
    broker.call("room.create", {"name": "#build"})
    mine = Codex(broker, make_token(broker, "laptop"))
    assert mine.start().status_code == 200
    assert mine.call("join", THREAD_A, room="#build", screen_name="codex-1")["ok"]
    broker.restart()
    auth = broker.state.agent_auth
    real = auth._inner

    async def refused(scope: Any, message: Any, session_id: Any, protocol: Any) -> Any:
        if isinstance(message, dict) and message.get("method") == "notifications/initialized":
            return 500, None, b""
        return await real(scope, message, session_id, protocol)

    auth._inner = refused
    assert mine.raw("who", THREAD_A, room="#build").status_code == 404
    until(lambda: library_sessions(broker) == set())
    auth._inner = real
    assert mine.call("who", THREAD_A, room="#build")["ok"]  # the record stayed: it opens now


def test_a_reopen_uses_the_protocol_the_library_negotiated(broker: InProcBroker) -> None:
    """The record keeps the protocol the client asked for, which the library may have answered with
    another. The broker's handshake must use the one the library answers it with, not the stored one, and
    keep it. Fails if notifications/initialized carries a protocol the library refuses (#903, F4)."""
    token = make_token(broker, "laptop")
    first, sid = opener(broker, token, "Cursor", protocol="2024-01-01")
    negotiated = sse_result(first)["result"]["protocolVersion"]
    assert negotiated != "2024-01-01"
    row = token_row(broker, token)
    key = hashlib.sha256(sid.encode()).hexdigest()
    assert broker.on_loop(broker.state.store.mcp_session, row.id, key, REOPEN_IDLE_S).protocol == "2024-01-01"
    broker.restart()
    c = Codex(broker, token)
    c.sid = sid
    body = {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}}
    r = c.c.post("/mcp", json=body, headers=c.headers({"accept": "application/json, text/event-stream"}))
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.headers["mcp-session-id"] == sid
    assert broker.on_loop(broker.state.store.mcp_session, row.id, key, REOPEN_IDLE_S).protocol == negotiated


@pytest.mark.parametrize(
    ("name", "kept"),
    [
        ("\x00my-client", "my-client"),  # SQLite's length() stops at the NUL: the CHECK failed
        ("\x00Cursor", None),  # cleaned it would reopen as Cursor, which it wasn't: no record (F12)
        ("\x01\x02\x1b​", None),  # nothing printable: no record
        ("A\x00" + "b" * 200, "A" + "b" * 79),  # cut after the controls go, not before
    ],
)
def test_a_client_name_is_kept_printable_and_never_fails_the_initialize(
    broker: InProcBroker, name: str, kept: str | None
) -> None:
    """clientInfo.name is the client's claim: it is kept the way the broker keeps untrusted text (control
    and format characters dropped, cut to 80), or not at all, and the client's initialize succeeds whatever
    it says. Fails if a NUL-led name turns the initialize into a database error, or a control character is
    stored (#903, F5)."""
    token = make_token(broker, "laptop")
    _, sid = opener(broker, token, name)
    row = token_row(broker, token)
    got = broker.on_loop(
        broker.state.store.mcp_session, row.id, hashlib.sha256(sid.encode()).hexdigest(), REOPEN_IDLE_S
    )
    assert (got.client_name if got is not None else None) == kept


def test_a_record_that_cannot_be_stored_never_fails_the_initialize(
    broker: InProcBroker, caplog: pytest.LogCaptureFixture
) -> None:
    """The record only helps a later restart: a database error writing it is logged, without the client's
    claim, and the client's own initialize still succeeds. Fails if the error reaches the response (#903,
    F5)."""
    import sqlite3

    def broken(*_: Any) -> None:
        raise sqlite3.IntegrityError("CHECK constraint failed")

    broker.state.store.mcp_session_opened = broken
    caplog.set_level(logging.WARNING, logger="switchboard")
    opener(broker, make_token(broker, "laptop"), "Cursor-unstored")
    assert any("could not keep what opened" in r.getMessage() for r in caplog.records)
    assert not any("Cursor-unstored" in r.getMessage() for r in caplog.records)


def test_the_reopen_log_line_holds_no_client_name(
    broker: InProcBroker, caplog: pytest.LogCaptureFixture
) -> None:
    """What a client calls itself is its claim, and may carry anything: the line that says a session was
    opened again names no client. Fails if the log prints the client-supplied name (#903, F6)."""
    token = make_token(broker, "laptop")
    _, sid = opener(broker, token, "Cursor-name-from-the-client")
    broker.restart()
    caplog.set_level(logging.INFO, logger="switchboard")
    c = Codex(broker, token)
    c.sid = sid
    assert c.tools()
    lines = [r.getMessage() for r in caplog.records if r.name.startswith("switchboard")]
    assert any("opened an MCP session again" in line for line in lines), lines
    assert not any("Cursor-name-from-the-client" in line for line in lines)


def restarted_codex(b: InProcBroker) -> tuple[Codex, str]:
    """A Codex session that joined #build, after a restart: its next call is what opens it again."""
    b.call("room.create", {"name": "#build"})
    token = make_token(b, "laptop")
    mine = Codex(b, token)
    assert mine.start().status_code == 200
    assert mine.call("join", THREAD_A, room="#build", screen_name="codex-1")["ok"]
    b.restart()
    assert library_sessions(b) == set()
    return mine, token


def initialized_then(b: InProcBroker, after: Any) -> None:
    """Make the broker's notifications/initialized reach the library, then run ``after`` (a coroutine
    function) in its place: the library holds the reopened session, and AgentAuth hasn't bound it."""
    auth = b.state.agent_auth
    real = auth._inner

    async def patched(scope: Any, message: Any, session_id: Any, protocol: Any) -> Any:
        answer = await real(scope, message, session_id, protocol)
        if isinstance(message, dict) and message.get("method") == "notifications/initialized":
            assert answer[0] == 202
            return await after()
        return answer

    auth._inner = patched


def test_an_error_in_the_reopens_handshake_leaves_no_library_session(broker: InProcBroker) -> None:
    """Once the broker's own initialize returned the library's ID, an exception in the rest of the handshake
    must still end that library session: AgentAuth never binds it, so nothing else could reach it. Fails if
    only a returned status ends it (#903, F9)."""
    mine, _ = restarted_codex(broker)

    async def broken() -> Any:
        raise RuntimeError("the handshake broke")

    initialized_then(broker, broken)
    assert mine.raw("who", THREAD_A, room="#build").status_code == 500
    until(lambda: library_sessions(broker) == set())
    assert mine.sid not in broker.on_loop(lambda: set(broker.state.agent_auth.sessions))


def test_a_cancelled_reopen_leaves_no_library_session_and_stays_cancelled(broker: InProcBroker) -> None:
    """A request cancelled while the reopen's second library call runs (its client gone, a shutdown) must
    still end the library session its initialize opened, and the cancellation must still reach the caller:
    the cleanup can't swallow it. The moment is an event the test controls. Fails if the cancellation skips
    the cleanup, or the cleanup eats the cancellation (#903, F9)."""
    mine, _ = restarted_codex(broker)
    auth = broker.state.agent_auth
    parked = threading.Event()
    held: dict[str, Any] = {}

    async def park() -> Any:
        held["task"] = asyncio.current_task()
        parked.set()
        await asyncio.Event().wait()  # until the test cancels it

    initialized_then(broker, park)
    real_reopen = auth.reopen
    cancelled = threading.Event()

    async def watched(*args: Any) -> Any:
        try:
            return await real_reopen(*args)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    auth.reopen = watched
    with ThreadPoolExecutor(1) as pool:
        call = pool.submit(mine.raw, "who", THREAD_A, room="#build")
        assert parked.wait(30)
        assert library_sessions(broker) != set()  # the library holds it, unbound
        broker.on_loop(lambda: held["task"].cancel())
        assert cancelled.wait(30)
        try:
            call.result(30)
        except httpx.HTTPError:
            pass  # the request was cancelled: how its client hears that is the server's business
    until(lambda: library_sessions(broker) == set())
    assert mine.sid not in broker.on_loop(lambda: set(auth.sessions))


def test_a_database_error_finishing_a_reopen_leaves_no_library_session(broker: InProcBroker) -> None:
    """The reopen's last step writes its record again. A database error there must end the library session
    the handshake made, as a missing record does. Fails if the error skips the cleanup (#903, F9)."""
    import sqlite3

    mine, _ = restarted_codex(broker)
    store = broker.state.store

    def broken(*_: Any) -> bool:
        raise sqlite3.OperationalError("database is locked")

    store.mcp_session_reopened = broken
    assert mine.raw("who", THREAD_A, room="#build").status_code == 500
    until(lambda: library_sessions(broker) == set())
    assert mine.sid not in broker.on_loop(lambda: set(broker.state.agent_auth.sessions))
    del store.mcp_session_reopened
    assert mine.call("who", THREAD_A, room="#build")["ok"]  # the record stayed: it opens now


def expire_in_library(b: InProcBroker, sid: str) -> None:
    """End a session inside the MCP library alone, as its idle timeout or a crash of the session's task does
    (the manager's own ``_discard_session``, which both end in), leaving AgentAuth's map as it was."""
    manager = b.state.agent_auth.app.session_manager

    async def discard() -> None:
        await manager._discard_session(sid, manager._server_instances[sid])

    assert b.loop is not None
    asyncio.run_coroutine_threadsafe(discard(), b.loop).result(10)
    assert sid not in library_sessions(b)
    assert sid in b.on_loop(lambda: set(b.state.agent_auth.sessions))


def test_a_delete_of_a_session_the_library_expired_is_never_reopened(broker: InProcBroker) -> None:
    """The library expires idle sessions apart from AgentAuth's map. A client's DELETE of such an ID then gets
    the library's 404, and must still forget the ID and its record: after a restart that deleted ID is not
    opened again (404). Fails if only a 200, 202 or 204 forgets it (#903, F10)."""
    recorded = json.loads((EXIT_FIX / "mcp_exit_quit.json").read_text())
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker, "laptop")
    mine = Codex(broker, token)
    assert mine.start().status_code == 200
    assert mine.call("join", THREAD_A, room="#build", screen_name="codex-1")["ok"]
    expire_in_library(broker, mine.sid)
    assert mine.delete(recorded).status_code == 404  # the library's own answer
    assert mine.sid not in broker.on_loop(lambda: set(broker.state.agent_auth.sessions))
    key = hashlib.sha256(mine.sid.encode()).hexdigest()
    row = token_row(broker, token)
    assert broker.on_loop(broker.state.store.mcp_session, row.id, key, REOPEN_IDLE_S) is None
    broker.restart()
    assert mine.raw("who", THREAD_A, room="#build").status_code == 404
    assert library_sessions(broker) == set()


def test_a_delete_the_library_answers_404_ends_only_the_threads_it_served_last(broker: InProcBroker) -> None:
    """The client's DELETE ends its session on the library's 404 too, and the rule of #449 still holds
    for a 404: it takes offline only the threads no newer session has served since. Fails if a 404 ends a
    thread a newer session serves, or leaves the still-old one online (#903, F10)."""
    recorded = json.loads((EXIT_FIX / "mcp_exit_quit.json").read_text())
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker, "laptop")
    old = Codex(broker, token)
    assert old.start().status_code == 200
    for thread, name in ((THREAD_A, "codex-1"), (THREAD_B, "codex-2")):
        assert old.call("join", thread, room="#build", screen_name=name)["ok"]
        assert old.call("hook", thread, event="Stop", session_id=thread) == {}
    resumed = Codex(broker, token)
    assert resumed.start().status_code == 200
    assert resumed.call("who", THREAD_A, room="#build")["ok"]
    expire_in_library(broker, old.sid)
    assert old.delete(recorded).status_code == 404
    assert status(broker, "codex-1") == "idle"
    assert status(broker, "codex-2") == "offline"


def test_a_delete_the_library_refuses_keeps_the_session_and_its_record(broker: InProcBroker) -> None:
    """A DELETE the library rejects before it ends anything leaves its session alive: one whose
    Mcp-Protocol-Version is no handshake version goes to the library's stateless handler, which answers 405
    and never touches the live transport. The broker must keep the ID, its record and its threads, so the
    client's next valid call is served (200, not 404) and a restart still reopens it. Fails if every answer
    to a DELETE forgets the session (#903, F10 reopened)."""
    recorded = json.loads((EXIT_FIX / "mcp_exit_quit.json").read_text())
    broker.call("room.create", {"name": "#build"})
    token = make_token(broker, "laptop")
    mine = Codex(broker, token)
    assert mine.start().status_code == 200
    assert mine.call("join", THREAD_A, room="#build", screen_name="codex-1")["ok"]
    assert mine.call("hook", THREAD_A, event="Stop", session_id=THREAD_A) == {}
    last = [e for e in recorded["server_events"] if e.get("method") == "DELETE"][0]
    headers = mine.headers({**last["headers"], "mcp-protocol-version": "2099-01-01"})
    assert mine.c.delete("/mcp", headers=headers).status_code == 405  # the library ended nothing
    assert mine.sid in broker.on_loop(lambda: set(broker.state.agent_auth.sessions))
    key = hashlib.sha256(mine.sid.encode()).hexdigest()
    row = token_row(broker, token)
    assert broker.on_loop(broker.state.store.mcp_session, row.id, key, REOPEN_IDLE_S) is not None
    assert status(broker, "codex-1") == "idle"  # no SessionEnd
    after = mine.raw("who", THREAD_A, room="#build")
    assert after.status_code == 200 and tool_json(after)["ok"]
    broker.restart()
    assert mine.call("who", THREAD_A, room="#build")["ok"]  # its record still reopens it


def test_a_revoke_during_an_initialize_leaves_no_record(broker: InProcBroker) -> None:
    """A fresh initialize is let in with a live token, then awaits the library's answer. A revoke that commits
    in between deletes the token's rows; the record written after the answer must not bring one back, so a
    revoked token keeps nothing a restart could open. The moment is an event the test controls. Fails if the
    insert checks only that the token row exists (#903, F11)."""
    token = make_token(broker, "laptop")
    row = token_row(broker, token)
    auth = broker.state.agent_auth
    real = auth.app
    hold, at = threading.Event(), threading.Event()

    async def paused(scope: Any, receive: Any, send: Any) -> None:
        opening = scope.get("method") == "POST" and not any(
            k.lower() == b"mcp-session-id" for k, _ in scope.get("headers", [])
        )
        if opening:
            at.set()
            assert await asyncio.to_thread(hold.wait, 30)
        await real(scope, receive, send)

    auth.app = paused
    c = Codex(broker, token, client="Cursor")
    body = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "Cursor"}},
    }
    headers = c.headers({"accept": "application/json, text/event-stream", "content-type": "application/json"})
    try:
        with ThreadPoolExecutor(1) as pool:
            call = pool.submit(c.c.post, "/mcp", json=body, headers=headers)
            assert at.wait(30)
            assert broker.on_loop(broker.state.store.agent_token_revoke_id, row.id) is not None
            hold.set()
            r = call.result(30)
    finally:
        auth.app = real
    assert r.status_code == 200, r.text  # the library answered: only what is kept is refused
    count = broker.on_loop(
        lambda: broker.state.store.con.execute(
            "SELECT COUNT(*) FROM mcp_sessions WHERE token_id=?", (row.id,)
        ).fetchone()[0]
    )
    assert count == 0


@pytest.mark.parametrize(
    "name",
    [
        "\x00Cursor",  # control character
        "Cursor\u200b",  # zero-width space, a format character
        "\uff23ursor",  # a fullwidth C, which NFKC folds
        " Cursor ",  # whitespace a cleaned name loses
        "codex-mcp-client\x1b",  # Codex's name with an ESC
        "claude-\u200dcode",  # Claude Code's name with a zero-width joiner
    ],
)
def test_a_name_that_cleans_into_another_client_is_not_reopened(broker: InProcBroker, name: str) -> None:
    """A session is classified from the raw, cut name (``prime``, and the Cursor tools of ``InputIdentity``),
    a reopened one from what was kept, and the kept name is cleaned. A raw name whose cleaned form is another
    client's must not come back as that client: it keeps no record, and after a restart its ID is unknown
    (404), never a session with Cursor's tools. Fails on 143172dc, which reopened it as the cleaned name
    (#903, F12)."""
    token = make_token(broker, "laptop")
    _, sid = opener(broker, token, name)
    c = Codex(broker, token)
    c.sid = sid
    first = c.tools()
    assert not any(t.startswith("switchboard_") for t in first)  # not Cursor's tools: it wasn't Cursor
    row = token_row(broker, token)
    key = hashlib.sha256(sid.encode()).hexdigest()
    assert broker.on_loop(broker.state.store.mcp_session, row.id, key, REOPEN_IDLE_S) is None
    broker.restart()
    body = {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}}
    r = c.c.post("/mcp", json=body, headers=c.headers({"accept": "application/json, text/event-stream"}))
    assert r.status_code == 404, (r.status_code, r.text)
    assert library_sessions(broker) == set()
