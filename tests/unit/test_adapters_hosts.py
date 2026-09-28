"""The adapters keep hosts apart (DESIGN.md §27.5.4, §27.5.6, §27.7), M8b.

No link exists yet, so a remote participant is a row with ``host`` set, as M8c's
joins will write it. Its pids deliberately equal pids of a local row (or of this
test process), so an adapter lookup that ignored the host, or probed this
machine for a remote pid, shows up here. These are the regressions a mutation
run of the M8b review found unguarded (each test names the lines it pins).
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import FakeClock
from engine_world import World
from switchboard.adapters import codex as cx
from switchboard.adapters import cursor as cu
from switchboard.adapters.claude import TIER_HOOK, TIER_INBOX, ClaudeAdapter
from switchboard.adapters.codex import Clients, CodexAdapter
from switchboard.broker import proc
from switchboard.broker.peer import McpIdentity
from switchboard.config import Config
from switchboard.models import Participant, session_key, split_session_key

PI = "fpga-pi"
SOCK = "/tmp/yk-test-inbox.sock"
TID = "019a0000-0000-7000-8000-000000000001"


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def remote(w: World, harness: str, rest: str, name: str, *, like: Participant | None = None,
           **fields: Any) -> Participant:
    """A joined participant on the Pi. With ``like``, it has that local row's pids and
    starts (on the Pi they are some Pi process; here they would name the local one)."""
    base: dict[str, Any] = dict(agent_pid=7001, agent_start=1.0, mcp_pid=7101, mcp_start=2.0)
    if like is not None:
        base = dict(agent_pid=like.agent_pid, agent_start=like.agent_start, mcp_pid=like.mcp_pid,
                    mcp_start=like.mcp_start)
    base.update(status="idle", tier="mcp-only", hooks_seen_at=w.clock.now())
    base.update(fields)
    p = w.store.upsert_participant(harness, session_key(harness, PI, rest), host=PI, **base)
    w.store.create_membership(w.room.id, p.id, name, "h-" + name)
    return p


def me() -> proc.ProcInfo:
    i = proc.info(os.getpid())
    assert i is not None
    return i


# ------------------------------------------------------------------ keys
def test_split_session_key_inverts_session_key() -> None:
    for h, host, rest in [("claude", "", "4242@1.00"), ("claude", PI, "4242@1.00"), ("codex", PI, "a:b:c"),
                          ("cursor", "", "agent:7@1.00"), ("test", PI, "s")]:
        assert split_session_key(session_key(h, host, rest)) == (h, host, rest)
    for bad in ["nocolon", ":x", "@fpga-pi:x", "codex@Not A Host:t", "codex@:t"]:
        assert split_session_key(bad) is None, bad
    assert split_session_key("codex@a:b@c:d") == ("codex", "a", "b@c:d")  # only the head names the host


# ---------------------------------------------------------------- Claude
class _RunnerStub:
    def __init__(self, w: World) -> None:
        self.state = SimpleNamespace(engine=w.engine, store=w.store, clock=w.clock)
        self.acts: list[Any] = []

    def execute(self, acts: list[Any]) -> None:
        self.acts += acts


def claude_ad(w: World) -> ClaudeAdapter:
    a = w.engine.adapters["claude"]
    assert isinstance(a, ClaudeAdapter)
    return a


def local_claude(w: World) -> Participant:
    p, _m = w.agent("vivado", harness="claude", status="idle", hooks=True)
    return w.store.update_participant(p.id, claude_socket=SOCK)


def test_poll_once_reads_and_prunes_only_this_machines_registry(w: World, tmp_path: Path) -> None:
    """claude.py poll_once: a remote row's pid is never looked up in this machine's
    sessions dir, and a remote registry view (M8d's relayed ``reg`` frames) survives
    the local prune."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    a = claude_ad(w)
    a.cfg = a.cfg.replace(claude=dataclasses.replace(a.cfg.claude, sessions_dir=str(sessions)))
    a.runner, a.clock = _RunnerStub(w), w.clock
    rp = remote(w, "claude", "7001@1.00", "bench", claude_socket=SOCK)
    # this machine happens to have a Claude session with the Pi row's pid, waiting on a prompt
    (sessions / f"{rp.agent_pid}.json").write_text(
        json.dumps({"pid": rp.agent_pid, "status": "waiting", "messagingSocketPath": SOCK}))
    a.observe(4242, {"pid": 4242, "status": "busy"}, w.clock.now(), host=PI)  # a relayed Pi view
    a.poll_once()
    assert ("", rp.agent_pid) not in a.registry and (PI, rp.agent_pid) not in a.registry
    assert w.p(rp).status == "idle"  # no approval hold from someone else's registry file
    assert a.registry[(PI, 4242)].status == "busy"  # never pruned by the local poll


def test_push_channels_and_registry_views_are_per_host(w: World) -> None:
    """claude.py conn_for, conn_tier, reg_view: the same pids on another host are
    another process, whose channel and registry view are not this one's."""
    a = claude_ad(w)
    p = local_claude(w)
    rp = remote(w, "claude", "same-pids", "bench", like=p, claude_socket=SOCK)
    conn = SimpleNamespace(closed=False, push=lambda *x: None)
    a.attach(p.mcp_pid, p.mcp_start, conn)
    a.observe(p.agent_pid, {"pid": p.agent_pid, "status": "idle"}, w.clock.now())
    assert a.conn_for(p) is conn and a.conn_for(rp) is None
    assert a.reg_view(p) is not None and a.reg_view(rp) is None
    ident = dict(harness="claude", mcp_pid=p.mcp_pid, mcp_start=p.mcp_start, agent_pid=p.agent_pid,
                 agent_start=p.agent_start, evidence="stub", claude_socket=SOCK)
    assert a.conn_tier(McpIdentity(**ident), None) == (TIER_INBOX, None)
    assert a.conn_tier(McpIdentity(**ident, host=PI), None) == (TIER_HOOK, None)
    # and the Pi's own channel (M8c attaches it with its host) is only the Pi row's
    pi_conn = SimpleNamespace(closed=False, push=lambda *x: None)
    a.attach(rp.mcp_pid, rp.mcp_start, pi_conn, host=PI)
    assert a.conn_for(rp) is pi_conn and a.conn_for(p) is conn
    assert a.detach(pi_conn) == [rp.mcp_pid] and a.conn_for(p) is conn


# ----------------------------------------------------------------- Codex
def codex_ad(w: World) -> CodexAdapter:
    a = w.engine.adapters["codex"]
    assert isinstance(a, CodexAdapter)
    a.clock = w.clock
    a.runner = SimpleNamespace(
        state=SimpleNamespace(store=w.store, engine=w.engine, clock=w.clock, agents=None,
                              info=SimpleNamespace(codex_link="")),
        execute=lambda acts: w.actions.extend(acts))
    return a


def local_codex(w: World) -> Participant:
    p, _m = w.agent("codex-1", harness="codex", status="idle", hooks=True)
    return w.store.update_participant(p.id, session_key=f"codex:{TID}", thread_proof=1)


def attach_daemon(a: CodexAdapter, w: World) -> None:
    a.link_state, a.loaded, a.loaded_at = "up", {TID}, w.clock.now()
    a.clients = Clients(True, w.clock.now(), 1)
    a.view[TID] = ("idle", w.clock.now())


def test_codex_adapter_sees_local_rows_only(w: World) -> None:
    """codex.py _joined, thread_of: CodexAdapter is this machine's daemon's; a Pi
    Codex row (M8c gives it a pull-only adapter of its own) is none of its threads."""
    a = codex_ad(w)
    p = local_codex(w)
    rp = remote(w, "codex", TID, "bench", thread_proof=1)  # even the same thread id
    assert [x.id for x in a._joined()] == [p.id]
    assert cx.thread_of(p) == TID and cx.thread_of(rp) == ""


def test_a_remote_codex_row_is_never_probed_or_live_here(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """codex.py live, tier: the queue tier's liveness check would ask this machine
    about the Pi's pid (here: this test process, alive). Never asked, never live."""
    a = codex_ad(w)
    i = me()
    rp = remote(w, "codex", TID, "bench", thread_proof=1, agent_pid=i.pid, agent_start=i.start)
    attach_daemon(a, w)
    monkeypatch.setattr(proc, "alive", lambda *x: pytest.fail("probed this machine for a Pi pid"))
    assert a.live(rp) == (False, cx.REMOTE_WHY)
    assert a.tier(rp) == ("mcp-only", cx.REMOTE_WHY)


def test_codex_conn_tier_needs_the_same_host(w: World) -> None:
    """codex.py conn_tier: a proven thread's tier carries over only to the very MCP
    process that holds it, on its own host."""
    a = codex_ad(w)
    p = local_codex(w)
    attach_daemon(a, w)
    ident = dict(harness="codex", mcp_pid=p.mcp_pid, mcp_start=p.mcp_start, agent_pid=p.agent_pid,
                 agent_start=p.agent_start, evidence="stub")
    assert a.conn_tier(McpIdentity(**ident), p) == a.tier(p) == ("codex:daemon", None)
    # anything else proves the thread again first: "verifying..." until on_joined's proof ends
    assert a.conn_tier(McpIdentity(**ident, host=PI), p) == ("mcp-only", "verifying...")


def test_a_remote_hello_leaves_the_codex_adapter_alone(w: World) -> None:
    """codex.py on_mcp_hello: a Pi Codex MCP server's hello names a Pi app-server; it
    must never be remembered as this machine's, or re-bind a local orphan to it."""
    a = codex_ad(w)
    p = local_codex(w)
    attach_daemon(a, w)
    w.store.update_participant(p.id, agent_pid=999_999, agent_start=1.0)  # its app-server died
    a.lost = (w.clock.now(), frozenset({TID}))
    a.link_state, a.loaded, a.loaded_at = "down", set(), None
    assert a.defer_end(w.p(p)) is True and p.id in a.orphans
    i = me()  # alive here, and a Codex-named hello
    a.on_mcp_hello(SimpleNamespace(harness="codex", agent_pid=i.pid, agent_start=i.start, host=PI), [w.p(p)])
    assert a.fresh_agents == {} and p.id in a.orphans and w.p(p).agent_pid == 999_999
    # control: the same hello from this machine re-binds the orphan
    a.on_mcp_hello(SimpleNamespace(harness="codex", agent_pid=i.pid, agent_start=i.start, host=""), [w.p(p)])
    assert p.id not in a.orphans and w.p(p).agent_pid == i.pid


# ---------------------------------------------------------------- Cursor
def test_cursor_bound_reads_a_key_of_any_host(w: World) -> None:
    """cursor.py bound: a remote row bound to its conversation (``cursor@<host>:<id>``,
    what _bind_cursor writes) is bound; a pending one (``…:agent:<pid>@<start>``) isn't."""
    a = w.engine.adapters["cursor"]
    lb = w.store.upsert_participant("cursor", "cursor:conv-1", bind_state="bound", status="idle")
    lp = w.store.upsert_participant("cursor", "cursor:agent:7@1.00", bind_state="pending", status="idle")
    rb = w.store.upsert_participant("cursor", "cursor@fpga-pi:conv-2", host=PI, bind_state="bound",
                                    status="idle")
    rpend = w.store.upsert_participant("cursor", "cursor@fpga-pi:agent:7@1.00", host=PI,
                                       bind_state="pending", status="idle")
    assert [cu.bound(x) for x in (lb, lp, rb, rpend)] == [True, False, True, False]
    assert a.tier(rb) == a.tier(lb) and a.tier(rpend) == a.tier(lp)
    assert a.tier(rb)[0] == cu.TIER
