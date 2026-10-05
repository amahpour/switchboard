"""The broker's agent side keeps hosts apart (DESIGN.md §27.5.4-§27.5.6), M8b.

No link exists yet, so a remote participant here is a row with ``host`` set, as
M8c's joins will write it. Its pids are pids on its own host: they deliberately
equal live pids of this machine (the test process), so any lookup that ignored
the host, or probed this machine for it, would show up.
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Any

import pytest
from conftest import InProcBroker

from switchboard.broker import proc
from switchboard.broker.agents import McpConn, cred_hash
from switchboard.broker.peer import McpIdentity
from switchboard.broker.service import ServiceError
from switchboard.mcp.client import Stream
from switchboard.models import HookEvent, Participant, session_key

PI = "fpga-pi"


def me() -> proc.ProcInfo:
    i = proc.info(os.getpid())
    assert i is not None
    return i


def mk(
    b: InProcBroker,
    harness: str,
    host: str,
    rest: str,
    name: str,
    *,
    agent: tuple[int, float] = (4242, 100.0),
    mcp: tuple[int, float] = (4250, 101.0),
    status: str = "idle",
    **kw: Any,
) -> tuple[Participant, Any, str]:
    """A joined participant (and its credential), written as the join path writes one."""

    def f() -> tuple[Participant, Any, str]:
        st = b.state.store
        room = st.get_room("#build") or st.create_room("#build", "alice", 60, 6)
        p = st.upsert_participant(
            harness,
            session_key(harness, host, rest),
            host=host,
            agent_pid=agent[0],
            agent_start=agent[1],
            mcp_pid=mcp[0],
            mcp_start=mcp[1],
            status=status,
            **kw,
        )
        cred = secrets.token_urlsafe(16)
        m = st.create_membership(room.id, p.id, name, cred_hash(cred))
        b.state.agents.refresh_index()
        return p, m, cred

    return b.on_loop(f)


def get(b: InProcBroker, pid: int) -> Participant:
    p = b.on_loop(b.state.store.get_participant, pid)
    assert p is not None
    return p


def ident(
    host: str,
    *,
    harness: str = "claude",
    mcp: tuple[int, float] = (4250, 101.0),
    agent: tuple[int, float] = (4242, 100.0),
) -> McpIdentity:
    return McpIdentity(
        harness=harness,
        mcp_pid=mcp[0],
        mcp_start=mcp[1],
        agent_pid=agent[0],
        agent_start=agent[1],
        evidence="stub",
        host=host,
    )


class StubConn:
    closed = False

    def __init__(self, mc: McpConn) -> None:
        self.mcp = mc
        self.id = 99002

    def push(self, kind: str, data: dict[str, Any]) -> None:
        pass


def test_session_keys_name_the_host(broker: InProcBroker) -> None:
    a = broker.state.agents
    for host, want in (("", "claude:4242@100.00"), (PI, "claude@fpga-pi:4242@100.00")):
        assert a._session_key(McpConn(ident=ident(host)), None) == want
    assert a._session_key(McpConn(ident=ident(PI, harness="codex")), "t-1") == "codex@fpga-pi:t-1"
    assert (
        a._session_key(McpConn(ident=ident(PI, harness="cursor")), None) == "cursor@fpga-pi:agent:4242@100.00"
    )
    assert (
        a._session_key(McpConn(ident=ident(PI, harness="test"), test_session="s"), None) == "test@fpga-pi:s"
    )
    assert a._session_key(McpConn(ident=ident("", harness="test"), test_session="s"), None) == "test:s"


def test_a_local_hook_never_reaches_a_remote_row(broker: InProcBroker) -> None:
    i = me()
    # on the Pi these pids are some Pi process; here they are this test process, whose hook this is
    rp, _m, _c = mk(broker, "test", PI, "bench", "bench", agent=(i.pid, i.start), mcp=(i.pid, i.start))
    ev = {"harness": "test", "event": "UserPromptSubmit", "sid": "s-1", "t": time.time()}
    assert broker.call("hook.event", ev) == {"out": None}
    assert get(broker, rp.id).hooks_seen_at is None
    # control: the same row on this machine is found by the same hook
    lp, _m, _c = mk(broker, "test", "", "vivado", "vivado", agent=(i.pid, i.start), mcp=(i.pid, i.start))
    broker.call("hook.event", ev)
    assert get(broker, lp.id).hooks_seen_at is not None
    assert get(broker, rp.id).hooks_seen_at is None
    assert broker.state.agents._agent_index == {"": {i.pid}, PI: {i.pid}}


def test_liveness_never_asks_this_machine_about_a_remote_row(broker: InProcBroker) -> None:
    dead_here = (os.getpid(), 1.0)  # this pid with another start time: gone, as far as this machine knows
    rp, _m, _c = mk(broker, "claude", PI, "4242@1.00", "bench", agent=dead_here)
    lp, _m, _c = mk(broker, "claude", "", "4242@1.00", "vivado", agent=dead_here)
    broker.on_loop(broker.state.agents.check_liveness)
    assert not get(broker, lp.id).active  # ended: its agent is gone
    assert get(broker, rp.id).active  # its host can't tell yet (no link): never ended on a guess


def test_a_rejoin_cant_take_over_a_remote_session_its_host_cant_vouch_for(broker: InProcBroker) -> None:
    rp, _m, _c = mk(broker, "claude", PI, "4242@100.00", "bench")
    check = broker.state.agents._check_same_session
    with pytest.raises(ServiceError) as ei:
        broker.on_loop(check, rp, McpConn(ident=ident(PI, mcp=(4260, 102.0))))
    assert ei.value.code == "conflict" and "can't verify this session on fpga-pi yet" in ei.value.message
    # the MCP process that holds it may always come back (a broker or link reconnect)
    broker.on_loop(check, rp, McpConn(ident=ident(PI)))
    # the same pids on this machine are another process
    with pytest.raises(ServiceError):
        broker.on_loop(check, rp, McpConn(ident=ident("")))


def test_a_credential_works_only_from_its_own_host(broker: InProcBroker) -> None:
    rp, m, cred = mk(broker, "test", PI, "bench", "bench")
    member = broker.state.agents._member
    here = StubConn(McpConn(ident=ident("", harness="test")))
    with pytest.raises(ServiceError) as ei:
        broker.on_loop(lambda: member(here, {"cred": cred}, next_call=False))
    assert ei.value.code == "unauthorized"
    there = StubConn(McpConn(ident=ident(PI, harness="test")))
    p, got, _room = broker.on_loop(lambda: member(there, {"cred": cred}, next_call=False))
    assert p.id == rp.id and got.id == m.id


def test_cursor_bind_waits_for_a_remote_holder_its_host_cant_vouch_for(broker: InProcBroker) -> None:
    nonce = "0123456789abcdef"
    holder, _m, _c = mk(
        broker, "cursor", PI, "conv-1", "cur-old", agent=(os.getpid(), 1.0), bind_state="bound"
    )
    p, _m, _c = mk(
        broker,
        "cursor",
        PI,
        "agent:4400@100.00",
        "cur-new",
        agent=(4400, 100.0),
        bind_state="pending",
        bind_nonce=nonce,
    )
    ev = HookEvent(harness="cursor", event="PostToolUse", sid="conv-1", join_nonce=nonce)
    assert broker.on_loop(broker.state.agents._bind_cursor, p, ev) is None
    h = get(broker, holder.id)
    assert h.active and h.session_key == "cursor@fpga-pi:conv-1"
    assert get(broker, p.id).bind_state == "pending"
    # control: on this machine a holder whose agent is gone gives the conversation up
    lh, _m, _c = mk(broker, "cursor", "", "conv-2", "cur-l-old", agent=(os.getpid(), 1.0), bind_state="bound")
    lp, _m, _c = mk(
        broker,
        "cursor",
        "",
        "agent:4401@100.00",
        "cur-l-new",
        agent=(4401, 100.0),
        bind_state="pending",
        bind_nonce=nonce,
    )
    ev2 = HookEvent(harness="cursor", event="PostToolUse", sid="conv-2", join_nonce=nonce)
    bound = broker.on_loop(broker.state.agents._bind_cursor, lp, ev2)
    assert bound is not None and bound.session_key == "cursor:conv-2" and bound.bind_state == "bound"
    assert not get(broker, lh.id).active
    assert get(broker, holder.id).active  # the Pi's holder still untouched


def test_hello_and_disconnect_touch_only_their_own_hosts_rows(broker: InProcBroker) -> None:
    i = me()
    lp, _m, _c = mk(broker, "test", "", "t-local", "vivado", mcp=(i.pid, i.start), status="offline")
    rp, _m, _c = mk(broker, "test", PI, "t-pi", "bench", mcp=(i.pid, i.start), status="offline")
    with Stream(broker.paths.sock, timeout=5) as s:
        s.call("mcp.hello", {"harness": "test", "test_session": "t-local"}, 5)
        assert get(broker, lp.id).status == "starting"  # this MCP process's own session
        assert get(broker, rp.id).status == "offline"  # same pid, other host: not this process
        broker.on_loop(lambda: broker.state.store.set_status(rp.id, "idle", "test"))
    deadline = time.monotonic() + 5
    while get(broker, lp.id).status != "offline" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert get(broker, lp.id).status == "offline"
    assert get(broker, rp.id).status == "idle"


def test_early_models_are_kept_per_host(broker: InProcBroker) -> None:
    a = broker.state.agents
    i = me()
    # M8c: _remember_model takes the hook's chain (a local walk, or a remote hook's facts.chain)
    broker.on_loop(a._remember_model, "", a.hosts.local.ancestry(i.pid, 8), "claude-test-model")
    assert a._early_models.get(("", i.pid, f"{i.start:.2f}")) == "claude-test-model"
    assert not [k for k in a._early_models if k[0] != ""]
    broker.on_loop(a._remember_model, PI, a.hosts.view(PI).ancestry(i.pid, 8) or [], "claude-test-model")
    assert not [k for k in a._early_models if k[0] == PI]  # this machine is never walked for the Pi


# The review's mutation run (M8b) found these unguarded: each test pins one host-scoped line.
def test_a_remote_codex_credential_names_its_own_hosts_thread(broker: InProcBroker) -> None:
    """agents._member: a Codex credential checks its thread as ``codex@<host>:<thread>``."""
    rp, m, cred = mk(broker, "codex", PI, "t-1", "bench", thread_proof=1)
    member = broker.state.agents._member
    there = StubConn(McpConn(ident=ident(PI, harness="codex")))
    p, got, _room = broker.on_loop(lambda: member(there, {"cred": cred, "thread_id": "t-1"}, next_call=False))
    assert p.id == rp.id and got.id == m.id
    with pytest.raises(ServiceError) as ei:
        broker.on_loop(lambda: member(there, {"cred": cred, "thread_id": "t-2"}, next_call=False))
    assert ei.value.code == "unauthorized" and "another Codex thread" in ei.value.message


def test_a_bound_remote_cursor_row_keeps_its_own_conversation(broker: InProcBroker) -> None:
    """agents._bind_cursor without a nonce: only the row's own conversation goes on."""
    p, _m, _c = mk(broker, "cursor", PI, "conv-1", "cur", bind_state="bound")
    bind = broker.state.agents._bind_cursor
    got = broker.on_loop(bind, p, HookEvent(harness="cursor", event="PostToolUse", sid="conv-1"))
    assert got is not None and got.id == p.id
    assert broker.on_loop(bind, p, HookEvent(harness="cursor", event="PostToolUse", sid="conv-2")) is None


def test_a_join_takes_the_early_model_of_its_own_host(broker: InProcBroker) -> None:
    """agents.join: a SessionStart model remembered for (host, pid, start) goes to a
    join from that host only (the same pid here is another process)."""
    a = broker.state.agents

    def seed() -> None:
        a._early_models[(PI, 4242, "100.00")] = "model-from-the-pi"
        a._early_models[("", 4242, "100.00")] = "model-from-here"
        broker.state.store.get_room("#build") or broker.state.store.create_room("#build", "alice", 60, 6)

    broker.on_loop(seed)
    res = broker.on_loop(
        a.join, StubConn(McpConn(ident=ident(PI))), {"room": "#build", "screen_name": "bench"}
    )
    assert res["tier"] == "claude:hook"
    p = broker.on_loop(broker.state.store.find_participant, "claude", "claude@fpga-pi:4242@100.00")
    assert p is not None and p.host == PI
    assert a._models[p.id] == "model-from-the-pi"


def test_attach_keys_the_push_channel_by_host(broker: InProcBroker) -> None:
    """agents.attach: a channel is attached under its MCP process's host."""
    a = broker.state.agents
    mc = McpConn(
        ident=McpIdentity(
            harness="claude",
            mcp_pid=4250,
            mcp_start=101.0,
            agent_pid=4242,
            agent_start=100.0,
            evidence="stub",
            claude_socket="/tmp/yk-x.sock",
            host=PI,
        ),
        has_messaging_token=True,
    )
    conn = StubConn(mc)
    assert broker.on_loop(a.attach, conn, {"guard_ok": True}) == {"attached": True, "tier": "claude:inbox"}
    adapter = broker.state.engine.adapters["claude"]
    assert (PI, 4250) in adapter.conns and ("", 4250) not in adapter.conns
    assert broker.on_loop(adapter.detach, conn) == [4250]
