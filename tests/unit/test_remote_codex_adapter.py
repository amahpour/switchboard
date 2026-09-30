"""Codex on a remote host (DESIGN.md §27.7): its own adapter, never CodexAdapter's. Pull only
(``codex:hook``) until its MCP server attaches a wake channel over the link (``codex:link``,
issue #63): then an idle wake is a checked ``deliver`` to that server, confirmed on its
``mcp.posted``."""

from __future__ import annotations

import asyncio
import dataclasses
import secrets
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeClock, InProcBroker
from engine_world import World
from switchboard.adapters import remote_codex as rc
from switchboard.adapters.base import SendError
from switchboard.adapters.codex import CodexAdapter
from switchboard.adapters.remote_codex import NOTE, TIER, TIER_LINK, RemoteCodexAdapter
from switchboard.broker.agents import McpConn, cred_hash
from switchboard.broker.peer import McpIdentity
from switchboard.config import Config
from switchboard.models import Push, Release, session_key

PI = "fpga-pi"
TID = "019a0000-0000-7000-8000-000000000042"
WAKE = Release(items=(), kind="wake", counted=True, reason="human")
PRIO = Release(items=(), kind="priority", counted=False, reason="human")


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0))


def remote_codex(w: World, status: str = "idle") -> Any:
    p, m = w.agent(TID, harness="codex", status=status, hooks=True, host=PI)
    assert p.session_key == f"codex@{PI}:{TID}"
    return p, m


def test_tier_and_routes(w: World) -> None:
    p, m = remote_codex(w)
    a = w.engine.adapter(p)
    assert isinstance(a, RemoteCodexAdapter) and w.engine.adapter_for("codex", PI) is a
    assert isinstance(w.engine.adapter_for("codex", ""), CodexAdapter)
    assert a.tier(p) == (TIER, NOTE) == ("codex:hook", "remote Codex: pull only")
    assert a.context_events(p) == frozenset({"PostToolUse"})
    caps = a.caps(p)
    assert caps.wait_cap_s == w.cfg.codex.wait_cap_s and caps.ctx_max_chars == w.cfg.codex.ctx_max_chars
    rel = Release(items=(), kind="wake", counted=True, reason="human")
    assert a.route(p, rel, None, w.clock.now()).kind == "none"  # idle and not listening: no push path
    assert a.route(p, Release(items=(), kind="priority", counted=False, reason="human"), None, 0).kind == "pull"
    # mid-task, PostToolUse carries priority context, as for a local Codex hook member
    w.store.set_status(p.id, "busy", "hook")
    w.human("urgent")
    out = w.hook(w.p(p), "PostToolUse", tool="Bash", ok=True, sid=TID)
    assert out is not None and out.kind == "context" and "urgent" in out.text
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    w.hook(w.p(p), "Stop", sid=TID)
    # idle: a human message makes no push (no turn/start, steer or queue) ...
    w.take()
    w.human("hi again")
    assert not [x for x in w.take() if isinstance(x, Push)]
    # ... and an open wait() serves it
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 240)
    w.actions += acts
    s = w.engine.sinks.get(sink.id)
    assert s is None or not s.open
    assert any("hi again" in (r.get("text") or "") for r in w.resolved(sink.id))


def test_codex_adapter_never_sees_remote_rows(w: World) -> None:
    p, _m = remote_codex(w)
    cx = w.engine.adapters["codex"]
    assert isinstance(cx, CodexAdapter)
    # its lookups are by this machine's key: a thread of the same id on another host is not found
    assert cx._participant(TID) is None
    assert cx.live(p)[0] is False
    cx.on_mcp_hello(McpIdentity(harness="codex", mcp_pid=5, mcp_start=1.0, agent_pid=4, agent_start=1.0,
                                evidence="parent:codex", host=PI), [p])
    assert cx.fresh_agents == {}  # a remote identity is none of its business
    assert cx.orphans == {}


def test_remote_codex_goes_offline_on_disconnect(broker: InProcBroker) -> None:
    """A local Codex session stays up when its MCP connection drops (CodexLink tracks the
    thread); a remote one has only that connection: it goes offline, its waits end."""

    def mk(host: str, rest: str) -> tuple[int, McpConn]:
        st = broker.state.store
        room = st.get_room("#build") or st.create_room("#build", "alice", 60, 6)
        ident = McpIdentity(harness="codex", mcp_pid=4250 + len(rest), mcp_start=101.0, agent_pid=4242,
                            agent_start=100.0, evidence="parent:codex", host=host)
        p = st.upsert_participant("codex", session_key("codex", host, rest), host=host, agent_pid=4242,
                                  agent_start=100.0, mcp_pid=ident.mcp_pid, mcp_start=101.0, status="idle")
        st.create_membership(room.id, p.id, f"cx{len(rest)}", cred_hash(secrets.token_urlsafe(8)))
        return p.id, McpConn(ident=ident)

    class Stub:
        def __init__(self, mc: McpConn):
            self.mcp = mc
            self.id = 990000 + mc.ident.mcp_pid
            self.closed = False

    remote_id, rmc = broker.on_loop(mk, PI, "t-remote")
    local_id, lmc = broker.on_loop(mk, "", "t-local-1")
    broker.on_loop(broker.state.agents.conn_closed, Stub(rmc))
    broker.on_loop(broker.state.agents.conn_closed, Stub(lmc))
    get = broker.state.store.get_participant
    assert broker.on_loop(get, remote_id).status == "offline"
    assert broker.on_loop(get, local_id).status == "idle"


# ------------------------------------------------------------ codex:link (issue #63)
class LinkConn:
    """A remote Codex MCP server's link connection: every push to it carries ``chk``."""

    closed = False
    remote = True

    def __init__(self) -> None:
        self.checked: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def push(self, kind: str, data: dict[str, Any]) -> None:  # pragma: no cover - must not be used
        raise AssertionError("a wake without chk")

    def push_checked(self, kind: str, data: dict[str, Any], chk: dict[str, Any]) -> None:
        self.checked.append((kind, data, chk))


class _RunnerStub:
    def __init__(self, w: World) -> None:
        self.state = type("S", (), {"engine": w.engine, "store": w.store, "clock": w.clock})()
        self.acts: list[Any] = []

    def execute(self, acts: list[Any]) -> None:
        self.acts += acts


def linked(w: World, status: str = "idle") -> tuple[Any, Any, RemoteCodexAdapter, LinkConn]:
    p, m = remote_codex(w, status)
    a = w.engine.adapter(p)
    assert isinstance(a, RemoteCodexAdapter)
    a.runner, a.clock = _RunnerStub(w), w.clock
    conn = LinkConn()
    a.attach(p.mcp_pid, p.mcp_start, conn, host=PI)
    return p, m, a, conn


def wake_push(w: World, p: Any, text: str = "hi") -> Push:
    w.take()
    w.human(text)
    [push] = [x for x in w.take() if isinstance(x, Push) and x.participant_id == p.id]
    return push


async def send_and_answer(a: RemoteCodexAdapter, p: Any, push: Push, conn: LinkConn,
                          result: dict[str, Any] | None) -> Any:
    """``send`` one wake; the server answers ``mcp.posted`` with ``result`` (None: never)."""
    b = a.runner.state.store.get_batch(push.batch_id)

    async def answer() -> None:
        while not conn.checked:
            await asyncio.sleep(0)
        if result is not None:
            a.posted(b.id, conn, result)

    t = asyncio.get_running_loop().create_task(answer())
    try:
        return await a.send(p, b, push.text, room=push.room, sender=push.sender)
    finally:
        await t


def test_a_wake_channel_makes_it_codex_link(w: World) -> None:
    p, _m, a, conn = linked(w)
    assert a.tier(p) == (TIER_LINK, None) == ("codex:link", None)
    guide = a.join_guidance(p, "#fpga")
    assert "starts `[switchboard]`" in guide and "You don't need to call wait()" in guide
    now = w.clock.now()
    r = a.route(p, WAKE, None, now)
    assert (r.kind, r.path) == ("push", "turn_start")
    assert a.route(p, PRIO, None, now).kind == "pull"  # mid-task: PostToolUse context, as before
    assert a.route(w.store.update_participant(p.id, status="busy"), WAKE, None, now).kind == "defer"
    p = w.store.update_participant(p.id, status="idle")
    # SessionEnd: nothing wakes it until a later hook of that thread shows it running again
    w.hook(p, "SessionEnd", sid=TID)
    assert a.route(w.p(p), WAKE, None, now).reason == "session ended"
    w.clock.advance(1.0)
    w.hook(w.p(p), "UserPromptSubmit", sid=TID)
    w.hook(w.p(p), "Stop", sid=TID)
    assert a.route(w.p(p), WAKE, None, w.clock.now()).kind == "push"
    # the channel is (host, mcp pid, mcp start): another host's, or a restarted server's, isn't it
    assert not a.attached(dataclasses.replace(w.p(p), host="other-pi"))
    assert not a.attached(dataclasses.replace(w.p(p), mcp_start=99.0))
    assert a.detach(conn) == [p.mcp_pid]
    assert a.tier(w.p(p)) == (TIER, NOTE)
    assert a.route(w.p(p), WAKE, None, w.clock.now()).kind == "none"


def test_an_accepted_wake_is_confirmed_as_link_turn_start(w: World) -> None:
    p, _m, a, conn = linked(w)
    push = wake_push(w, p, "please flash it")
    assert push.path == "turn_start" and push.text.startswith("[switchboard]")
    t_post = w.clock.now()
    assert asyncio.run(send_and_answer(a, w.p(p), push, conn, {"ok": True, "t_post": t_post})) is None
    [(kind, data, chk)] = conn.checked
    assert kind == "deliver" and set(data) == {"batch_id", "text", "thread_id", "room", "sender"}
    assert (data["batch_id"], data["thread_id"], data["text"]) == (push.batch_id, TID, push.text)
    assert chk == {"pid": p.agent_pid, "start": p.agent_start, "want": "idle"}
    b = w.store.get_batch(push.batch_id)
    assert b is not None and (b.state, b.evidence, b.turn_start_at) == ("confirmed", "link:turn/start", t_post)


def test_refusals_reroute_or_back_off(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    p, _m, a, conn = linked(w)
    push = wake_push(w, p)

    def send(result: dict[str, Any] | None) -> SendError:
        conn.checked.clear()
        with pytest.raises(SendError) as e:
            asyncio.run(send_and_answer(a, w.p(p), push, conn, result))
        return e.value

    def routed() -> Any:
        return a.route(w.p(p), WAKE, None, w.clock.now())

    # busy or unloaded right now (on its machine, or the satellite's own read): uncounted
    # re-routes; a few in a row are free, then they back off
    for err in ("not_idle", "not_loaded", "stale_status")[: rc.REROUTE_FREE]:
        e = send({"ok": False, "err": err})
        assert (e.reason, e.counted) == (err, False) and routed().kind == "push"
    e = send({"ok": False, "err": "stale_status"})
    assert e.counted is False and routed().reason == "the wake failed on its machine; retrying"
    w.clock.advance(rc.REROUTE_BACKOFF_S[0] + 0.1)
    assert routed().kind == "push"
    # anything else is a counted failure with a backoff (1 s doubling)
    e = send({"ok": False, "err": "no_tui"})
    assert (e.reason, e.counted) == ("no_tui", True) and routed().kind == "defer"
    w.clock.advance(rc.SEND_BACKOFF_S[0] + 0.1)
    assert routed().kind == "push"
    monkeypatch.setattr(rc, "POST_TIMEOUT_S", 0.05)
    e = send(None)  # no mcp.posted at all
    assert (e.reason, e.counted) == ("no mcp.posted", True)
    assert a.backoff[p.id][1] == 2 and routed().kind == "defer"
    # a wake the server took clears both
    w.clock.advance(rc.SEND_BACKOFF_S[1])
    conn.checked.clear()
    asyncio.run(send_and_answer(a, w.p(p), push, conn, {"ok": True, "t_post": w.clock.now()}))
    assert p.id not in a.backoff and p.id not in a.reroutes


def test_a_lost_channel_fails_the_wake_in_flight(w: World) -> None:
    p, _m, a, conn = linked(w)
    push = wake_push(w, p)

    async def go() -> None:
        b = w.store.get_batch(push.batch_id)
        t = asyncio.get_running_loop().create_task(a.send(w.p(p), b, push.text))
        while not conn.checked:
            await asyncio.sleep(0)
        a.detach(conn)
        with pytest.raises(SendError) as e:
            await t
        assert (e.value.reason, e.value.counted) == ("disconnected", True)

    asyncio.run(go())
    with pytest.raises(SendError):  # no channel now: nothing is sent
        asyncio.run(a.send(w.p(p), w.store.get_batch(push.batch_id), push.text))


def test_the_broker_takes_a_codex_wake_channel_only_over_a_link(broker: InProcBroker) -> None:
    class Stub:
        facts: dict[str, Any] = {}

        def __init__(self, mc: McpConn, remote: bool):
            self.mcp, self.remote, self.closed, self.id = mc, remote, False, 880000 + mc.ident.mcp_pid

    def mk(harness: str, host: str, n: int) -> Stub:
        ident = McpIdentity(harness=harness, mcp_pid=5000 + n, mcp_start=101.0, agent_pid=4000 + n,
                            agent_start=100.0, evidence=f"parent:{harness}", host=host)
        return Stub(McpConn(ident=ident), remote=bool(host))

    ag = broker.state.agents
    local = mk("codex", "", 1)
    assert broker.on_loop(ag.attach, local, {"codex": True, "guard_ok": True})["attached"] is False
    claude = mk("claude", PI, 2)
    assert broker.on_loop(ag.attach, claude, {"codex": True, "guard_ok": True})["attached"] is False
    remote = mk("codex", PI, 3)
    assert broker.on_loop(ag.attach, remote, {"codex": True})["attached"] is False  # its own guard must pass
    assert broker.on_loop(ag.attach, remote, {"codex": True, "guard_ok": True}) == {"attached": True,
                                                                                   "tier": "codex:link"}
    a = broker.state.engine.adapters["codex@remote"]
    assert remote.mcp.codex_attached and a.conns[(PI, 5003)] == (101.0, remote)
    broker.on_loop(ag.conn_closed, remote)
    assert a.conns == {}
