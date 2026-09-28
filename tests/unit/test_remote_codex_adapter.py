"""Codex on a remote host (DESIGN.md §27.7): its own pull-only adapter, never CodexAdapter's."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeClock, InProcBroker
from engine_world import World
from switchboard.adapters.codex import CodexAdapter
from switchboard.adapters.remote_codex import NOTE, TIER, RemoteCodexAdapter
from switchboard.broker.agents import McpConn, cred_hash
from switchboard.broker.peer import McpIdentity
from switchboard.config import Config
from switchboard.models import Push, Release, session_key

PI = "fpga-pi"
TID = "019a0000-0000-7000-8000-000000000042"


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
