"""The Claude adapter for a session on a remote host (DESIGN.md §27.5.6, §27.7), with a
FakeClock: the per-host freshness of a registry view, the relayed registry driving the
same transitions as a local read, ``chk`` on every frame to a remote session, the
uncounted ``stale_status`` re-route, and channels keyed by host."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeClock
from engine_world import World
from switchboard.adapters.base import SendError
from switchboard.adapters.claude import (
    REGISTRY_FRESH_S,
    REGISTRY_LOST_S,
    REMOTE_FRESH_S,
    REMOTE_LOST_S,
    REROUTE_FREE,
    ClaudeAdapter,
)
from switchboard.broker.hosts import RemoteView
from switchboard.config import Config
from switchboard.models import Batch, Push, Release

HOST = "fpga-pi"
SOCK = "/tmp/yk-test-inbox.sock"
WAKE = Release(items=(), kind="wake", counted=True, reason="human")
# the satellite's own report of a dropped frame, as ``AgentService.posted`` hands it on
LASTMILE_STALE = {"ok": False, "err": "stale_status", "lastmile": True}
PRIO = Release(items=(), kind="priority", counted=False, reason="human")


class FakeConn:
    """A local channel (the MCP server on this machine)."""

    closed = False
    remote = False

    def __init__(self) -> None:
        self.pushes: list[tuple[str, dict[str, Any]]] = []

    def push(self, kind: str, data: dict[str, Any]) -> None:
        self.pushes.append((kind, data))


class FakeRemoteConn(FakeConn):
    """A channel carried by a link: pushes to it must go through ``push_checked``."""

    remote = True

    def __init__(self) -> None:
        super().__init__()
        self.checked: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def push(self, kind: str, data: dict[str, Any]) -> None:  # pragma: no cover - must not be used
        raise AssertionError("a push to a remote Claude without chk")

    def push_checked(self, kind: str, data: dict[str, Any], chk: dict[str, Any]) -> None:
        self.checked.append((kind, data, chk))


class _RunnerStub:
    def __init__(self, w: World) -> None:
        self.state = type("S", (), {"engine": w.engine, "store": w.store, "clock": w.clock})()
        self.acts: list[Any] = []

    def execute(self, acts: list[Any]) -> None:
        self.acts += acts


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def ad(w: World) -> ClaudeAdapter:
    a = w.engine.adapters["claude"]
    assert isinstance(a, ClaudeAdapter)
    if a.runner is None:
        a.runner = _RunnerStub(w)
        a.clock = w.clock
    return a


def view_of(w: World) -> RemoteView:
    v = getattr(w, "_remote_view", None)
    if v is None:
        v = RemoteView(HOST, w.clock)
        v.link_up()
        w._remote_view = v  # type: ignore[attr-defined]
    return v


def claude(w: World, name: str, *, host: str = "", status: str = "idle", mode: str = "prompting",
           registry: str | None = "idle") -> tuple[Any, Any, FakeConn]:
    p, m = w.agent(name, harness="claude", status=status, hooks=True, host=host)
    p = w.store.update_participant(p.id, claude_socket=SOCK, approval_mode=mode)
    conn: FakeConn = FakeRemoteConn() if host else FakeConn()
    ad(w).attach(p.mcp_pid, p.mcp_start, conn, host=host)
    if host:
        v = view_of(w)
        pairs = {(x.agent_pid, x.agent_start) for x in w.store.joined_participants() if x.host == host}
        v.set_watch(len(pairs), pairs)
    if registry is not None:
        reg(w, p, registry)
    return p, m, conn


def reg(w: World, p: Any, status: str | None, *, since_age: float | None = None, read_age: float = 0.0) -> list[Any]:
    """One registry read of ``p``: a local file read, or a relayed ``reg`` frame for a remote
    session (ages rebased by its host's view). Returns the actions it implies."""
    a = ad(w)
    now = w.clock.now()
    if p.host:
        got = view_of(w).registry([(p.agent_pid, p.agent_start, status, since_age)], read_age, now)
        return a.relay(p.host, got, now)
    data: dict[str, Any] = {"pid": p.agent_pid, "status": status}
    if since_age is not None:
        data["statusUpdatedAt"] = int((now - since_age) * 1000)
    view, changed = a.observe(p.agent_pid, data, now)
    return a.apply_view(w.p(p), view, now, changed)


def batch(m: Any, kind: str, bid: int = 1) -> Batch:
    return Batch(id=bid, membership_id=m.id, path="inbox", kind=kind, wake_kind=None, wake_reason=None,
                 budget_counted=False, state="offered", created_at=0.0, posted_at=None, confirmed_at=None,
                 expired_at=None, expire_reason=None, turn_start_at=None, first_action_at=None, evidence=None)


async def send_and_answer(a: ClaudeAdapter, p: Any, b: Batch, conn: FakeConn, result: dict[str, Any]) -> Any:
    """``send`` one frame; the channel answers ``mcp.posted`` with ``result``."""

    async def answer() -> None:
        while not (conn.pushes or getattr(conn, "checked", [])):
            await asyncio.sleep(0)
        a.posted(b.id, conn, result)

    t = asyncio.get_running_loop().create_task(answer())
    try:
        return await a.send(p, b, "[switchboard] hi", room="#build", sender="alice")
    finally:
        await t


# ----------------------------------------------------------------- freshness
def test_remote_view_fresh_1_5s_lost_5s(w: World, clock: FakeClock) -> None:
    assert (REGISTRY_FRESH_S, REGISTRY_LOST_S, REMOTE_FRESH_S, REMOTE_LOST_S) == (0.5, 3.0, 1.5, 5.0)
    a = ad(w)
    lp, _lm, _lc = claude(w, "vivado")
    rp, _rm, _rc = claude(w, "bench", host=HOST)
    assert (a.fresh_s(w.p(lp)), a.lost_s(w.p(lp))) == (0.5, 3.0)
    assert (a.fresh_s(w.p(rp)), a.lost_s(w.p(rp))) == (1.5, 5.0)
    t0 = clock.now()

    def kinds(p: Any, dt: float) -> str:
        return a.route(w.p(p), WAKE, None, t0 + dt).kind

    # an idle wake: local 0.5 s / 3 s, remote 1.5 s / 5 s
    assert [kinds(lp, dt) for dt in (0.4, 0.6, 2.9, 3.1)] == ["push", "defer", "defer", "none"]
    assert [kinds(rp, dt) for dt in (1.4, 1.6, 4.9, 5.1)] == ["push", "defer", "defer", "none"]
    r = a.route(w.p(rp), WAKE, None, t0 + 5.1)
    assert r.reason == "can't read the Claude session registry"
    # a relayed view is as old as the satellite's read: read_age counts (clamped to 5 s)
    reg(w, rp, "idle", read_age=1.0)
    assert kinds(rp, 0.4) == "push" and kinds(rp, 0.6) == "defer"
    reg(w, rp, "idle", read_age=60.0)  # clamped to 5 s (§27.4.6): lost a moment later
    assert kinds(rp, 0.0) == "defer" and kinds(rp, 0.1) == "none"
    # mid-task (bypass): the busy read must be fresh by the same per-host limit
    lb = w.store.update_participant(lp.id, status="busy", approval_mode="bypass")
    rb = w.store.update_participant(rp.id, status="busy", approval_mode="bypass")
    reg(w, lb, "busy")
    reg(w, rb, "busy")
    assert a.route(lb, PRIO, None, t0 + 0.4).path == "inbox" and a.route(lb, PRIO, None, t0 + 0.6).kind == "pull"
    assert a.route(rb, PRIO, None, t0 + 1.4).path == "inbox" and a.route(rb, PRIO, None, t0 + 1.6).kind == "pull"


@pytest.mark.parametrize("case", ["waiting", "approved", "declined", "esc"])
def test_remote_transitions_match_local(w: World, clock: FakeClock, case: str) -> None:
    """The same registry story read locally and relayed from a remote host ends in the
    same status: the approval hold, an approved or declined prompt, an Esc-ended turn."""
    lp, _lm, _lc = claude(w, "vivado", status="busy", registry=None)
    rp, _rm, _rc = claude(w, "bench", host=HOST, status="busy", registry=None)
    steps = {"waiting": ["waiting"], "approved": ["waiting", "busy"], "declined": ["waiting", "idle"],
             "esc": ["idle", "idle"]}[case]
    expect = {"waiting": "waiting-approval", "approved": "busy", "declined": "idle", "esc": "idle"}[case]
    t_status = clock.now()
    seen: list[tuple[str, str]] = []
    for i, st in enumerate(steps):
        clock.advance(0.3 if i == 0 else 1.1)
        if i == 0 or st != steps[i - 1]:
            t_status = clock.now()
        for p in (lp, rp):
            acts = reg(w, w.p(p), st, since_age=clock.now() - t_status)
            w.actions += acts
        seen.append((w.p(lp).status, w.p(rp).status))
    assert all(a == b for a, b in seen), seen
    assert w.p(lp).status == w.p(rp).status == expect


# ------------------------------------------------------------------- chk
def test_chk_want_idle_for_wake_busy_for_priority(w: World) -> None:
    a = ad(w)
    rp, rm, rc = claude(w, "bench", host=HOST)
    lp, lm, lc = claude(w, "vivado")
    assert isinstance(rc, FakeRemoteConn)

    async def go() -> None:
        await send_and_answer(a, w.p(rp), batch(rm, "wake", 1), rc, {"ok": True})
        await send_and_answer(a, w.p(rp), batch(rm, "priority", 2), rc, {"ok": True})
        await send_and_answer(a, w.p(lp), batch(lm, "wake", 3), lc, {"ok": True})

    asyncio.run(go())
    assert [(k, d["batch_id"], c) for k, d, c in rc.checked] == [
        ("deliver", 1, {"pid": rp.agent_pid, "start": rp.agent_start, "want": "idle"}),
        ("deliver", 2, {"pid": rp.agent_pid, "start": rp.agent_start, "want": "busy"}),
    ]
    # chk rides beside the frame, never inside it; a local channel gets no chk at all
    assert all("chk" not in d for _k, d, _c in rc.checked)
    assert [(k, d["batch_id"]) for k, d in lc.pushes] == [("deliver", 3)] and "chk" not in lc.pushes[0][1]
    # and the engine routes a remote wake to that same send
    w.human("hello bench")
    pushes = [x for x in w.take() if isinstance(x, Push)]
    assert {x.participant_id for x in pushes} == {rp.id, lp.id}


def test_stale_status_is_uncounted_reroute(w: World, clock: FakeClock) -> None:
    a = ad(w)
    rp, rm, rc = claude(w, "bench", host=HOST)

    async def stale(bid: int) -> SendError:
        with pytest.raises(SendError) as ei:
            await send_and_answer(a, w.p(rp), batch(rm, "wake", bid), rc, LASTMILE_STALE)
        return ei.value

    e = asyncio.run(stale(1))
    assert e.reason == "stale_status" and e.counted is False
    assert rp.id not in a.backoff  # no send backoff: nothing failed, the world changed
    # the view the refusal came with is not enough: route waits for a newer one
    r = a.route(w.p(rp), WAKE, None, clock.now())
    assert (r.kind, r.reason) == ("defer", "registry changed; re-checking")
    clock.advance(0.25)
    reg(w, rp, "idle")  # the next relayed read, after the refusal (the same status)
    assert rp.id not in a.recheck  # it ends the re-check (and routes the member again)
    assert a.route(w.p(rp), WAKE, None, clock.now()).kind == "push"
    # a few in a row are free; past that, re-routes back off too
    for i in range(REROUTE_FREE):
        if i:
            clock.advance(0.25)
            reg(w, rp, "idle")
            assert a.route(w.p(rp), WAKE, None, clock.now()).kind == "push"
        asyncio.run(stale(10 + i))
    clock.advance(0.25)
    reg(w, rp, "idle")
    r = a.route(w.p(rp), WAKE, None, clock.now())
    assert r.kind == "defer" and "keeps changing" in (r.reason or "")
    clock.advance(1.1)
    reg(w, rp, "idle")
    assert a.route(w.p(rp), WAKE, None, clock.now()).kind == "push"
    # a delivered frame clears it all
    asyncio.run(send_and_answer(a, w.p(rp), batch(rm, "wake", 20), rc, {"ok": True}))
    assert rp.id not in a.reroutes and rp.id not in a.recheck
    assert w.p(rp).push_expiries == 0


def test_stale_status_from_a_local_channel_is_a_failure(w: World) -> None:
    """Only a satellite's last-mile check reports stale_status; a local MCP server that says
    so gets the ordinary counted failure and its backoff."""
    a = ad(w)
    lp, lm, lc = claude(w, "vivado")

    async def go() -> SendError:
        with pytest.raises(SendError) as ei:
            await send_and_answer(a, w.p(lp), batch(lm, "wake"), lc, {"ok": False, "err": "stale_status"})
        return ei.value

    e = asyncio.run(go())
    assert e.counted is True and lp.id in a.backoff and lp.id not in a.recheck


@pytest.mark.parametrize("result", [
    {"ok": False, "err": "stale_status"},  # from the Pi MCP connection itself, not the satellite
    {"ok": False, "err": "stale_status", "lastmile": False},
    {"ok": False, "err": "no_chk", "lastmile": True},  # the satellite: a push the broker never sends
    {"ok": False, "err": "bad_chk", "lastmile": True},
])
def test_only_the_satellites_stale_status_is_uncounted(w: World, result: dict[str, Any]) -> None:
    """A client on the remote host can't take the silent re-route path: ``stale_status`` is
    uncounted only in the satellite's own report (``facts.lastmile``, which only the
    satellite sets), and its ``no_chk``/``bad_chk`` are counted, so a broker fault shows."""
    a = ad(w)
    rp, rm, rc = claude(w, "bench", host=HOST)

    async def go() -> SendError:
        with pytest.raises(SendError) as ei:
            await send_and_answer(a, w.p(rp), batch(rm, "wake"), rc, result)
        return ei.value

    e = asyncio.run(go())
    assert e.counted is True and e.reason == result["err"]
    assert rp.id in a.backoff and rp.id not in a.recheck and rp.id not in a.reroutes


def test_recheck_goes_by_arrival_not_by_the_clock(w: World, clock: FakeClock) -> None:
    """After a refusal the member waits for a view that arrived after it. A wall clock
    stepped back (NTP) must not hold it in re-check while fresh views keep coming."""
    a = ad(w)
    rp, rm, rc = claude(w, "bench", host=HOST)
    with pytest.raises(SendError):
        asyncio.run(send_and_answer(a, w.p(rp), batch(rm, "wake"), rc, LASTMILE_STALE))
    assert a.route(w.p(rp), WAKE, None, clock.now()).reason == "registry changed; re-checking"
    clock.advance(-30.0)  # the broker's clock steps back
    clock.advance(0.25)
    reg(w, rp, "idle")  # the next relayed view: older by the clock, newer by arrival
    assert rp.id not in a.recheck
    assert a.route(w.p(rp), WAKE, None, clock.now()).kind == "push"


def test_a_rerouted_wake_gives_its_budget_back(w: World) -> None:
    """A re-route reached nobody: the wake's budget unit goes back to the room, so a session
    whose frames keep being refused (or a remote that refuses them all) can't drain it. A
    counted failure keeps what it spent."""
    rp, rm, _rc = claude(w, "bench", host=HOST)
    budget = w.store.room_by_id(w.room.id).budget_remaining
    w.human("flash it", mentions=("bench",))
    for _ in range(6):
        [push] = [x for x in w.take() if isinstance(x, Push)]
        b = w.store.get_batch(push.batch_id)
        assert b.budget_counted and w.store.room_by_id(w.room.id).budget_remaining == budget - 1
        w.actions += w.engine.on_expire(push.batch_id, "reroute", count_failure=False)
        b = w.store.get_batch(push.batch_id)
        assert (b.state, b.budget_counted) == ("expired", False)
        assert w.store.room_by_id(w.room.id).budget_remaining == budget - 1  # refunded, then spent again
    [push] = [x for x in w.take() if isinstance(x, Push)]
    w.actions += w.engine.on_expire(push.batch_id, "send_error")
    assert w.store.get_batch(push.batch_id).budget_counted and w.p(rp).push_expiries == 1
    [again] = [x for x in w.take() if isinstance(x, Push)]  # offered again: that one spends its own
    assert w.store.get_batch(again.batch_id).budget_counted
    assert w.store.room_by_id(w.room.id).budget_remaining == budget - 2


def test_remote_channel_without_agent_start_sends_nothing(w: World) -> None:
    a = ad(w)
    rp, rm, rc = claude(w, "bench", host=HOST)
    p = dataclasses.replace(w.p(rp), agent_start=None)

    async def go() -> None:
        with pytest.raises(SendError):
            await a.send(p, batch(rm, "wake"), "[switchboard] hi")

    asyncio.run(go())
    assert rc.checked == []


# ------------------------------------------------------------------ channels
def test_channels_keyed_by_host(w: World) -> None:
    a = ad(w)
    lp, _lm, lc = claude(w, "vivado")
    rp, _rm, rc = claude(w, "bench", host=HOST)
    # the same MCP pid on both machines: two channels, never crossed
    a.conns.pop((HOST, rp.mcp_pid))
    rp = w.store.update_participant(rp.id, mcp_pid=lp.mcp_pid, mcp_start=lp.mcp_start)
    a.attach(rp.mcp_pid, rp.mcp_start, rc, host=HOST)
    assert set(a.conns) == {("", lp.mcp_pid), (HOST, lp.mcp_pid)}
    assert a.conn_for(w.p(lp)) is lc and a.conn_for(w.p(rp)) is rc
    assert a.tier(w.p(rp)) == ("claude:inbox", None)
    assert a.detach(rc) == [lp.mcp_pid]
    assert a.conn_for(w.p(rp)) is None and a.conn_for(w.p(lp)) is lc
    assert a.tier(w.p(rp)) == ("claude:hook", None)


def test_relayed_views_are_per_host_and_forgotten_with_the_link(w: World, clock: FakeClock) -> None:
    a = ad(w)
    lp, _lm, _lc = claude(w, "vivado")
    rp, _rm, _rc = claude(w, "bench", host=HOST)
    assert set(a.registry) == {("", lp.agent_pid), (HOST, rp.agent_pid)}
    # a relayed frame without that session (it left) prunes its view, and only that host's
    got = view_of(w).registry([], 0.0, clock.now())
    a.relay(HOST, got, clock.now())
    assert set(a.registry) == {("", lp.agent_pid)}
    reg(w, rp, "idle")
    # an unreadable relayed read keeps the last view, which then ages (as a local one does)
    clock.advance(1.0)
    reg(w, rp, None)
    assert a.reg_view(w.p(rp)).read_at == clock.now() - 1.0
    # a pair the broker never asked about is dropped by the host's view
    got = view_of(w).registry([(rp.agent_pid + 7, rp.agent_start, "idle", None)], 0.0, clock.now())
    assert got == {}
    a.forget_host(HOST)
    assert set(a.registry) == {("", lp.agent_pid)}
    assert a.route(w.p(rp), WAKE, None, clock.now()).kind == "none"
