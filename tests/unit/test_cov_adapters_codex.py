"""The Codex adapter's remaining edges (DESIGN.md §9.3), with a FakeClock world and
stand-ins for the app-server connection, ``lsof`` and the process table (no
daemon, no real ``lsof``, no codex binary):

- pure helpers: a Homebrew prefix that can't be read, a flag-like queue text,
  no ``lsof`` at all, junk in ``lsof`` output;
- the liveness guard and routing for restarting, ended, held and queue-tier
  threads, and expiry of an offered push;
- the transport's failure paths: a bad thread id or path, ``turn/start`` and
  ``turn/steer`` errors (RPC error, socket gone, approval wait), a steer that
  lands after its turn ended, and ``codex queue`` that can't start, times out
  or exits non-zero;
- steer settlement without a readable history, the status link's connect
  failure and crash, ``thread/loaded/list`` failures, the lsof pass for
  queue-tier app-servers, the clients loop surviving an error, the thread
  proof's early returns, and the restart-grace bookkeeping.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import socket
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import FakeClock
from engine_world import World
from switchboard.adapters import codex as cx
from switchboard.adapters import codex_rpc
from switchboard.adapters.base import SendError
from switchboard.adapters.codex import AgentClients, Clients, CodexAdapter, Orphan, SteerState
from switchboard.broker import proc
from switchboard.config import CodexCfg, Config
from switchboard.models import Batch, Notice, Push, Release

TID = "019a0000-0000-7000-8000-00000000c0f1"
TID2 = "019a0000-0000-7000-8000-00000000c0f2"
WAKE = Release(items=(), kind="wake", counted=True, reason="human")
PRIO = Release(items=(), kind="priority", counted=False, reason="human")
FAST = Config().with_delivery(quiet_s=0.0, max_hold_s=0.0)


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, FAST)


def ad(w: World) -> CodexAdapter:
    a = w.engine.adapters["codex"]
    assert isinstance(a, CodexAdapter)
    a.clock = w.clock
    return a


def wire(w: World) -> CodexAdapter:
    """The broker state the adapter acts through (store, engine, a runner that
    collects actions), as ``start()`` would give it, without any I/O."""
    a = ad(w)
    a.runner = SimpleNamespace(
        state=SimpleNamespace(store=w.store, engine=w.engine, clock=w.clock, agents=None,
                              info=SimpleNamespace(codex_link="")),
        execute=lambda acts: w.actions.extend(acts))
    return a


def codex(w: World, name: str = "codex-1", *, tid: str = TID, status: str = "idle", proof: bool = True,
          self_agent: bool = False, **upd: Any) -> tuple[Any, Any]:
    p, m = w.agent(name, harness="codex", status=status, hooks=True)
    fields: dict[str, Any] = {"session_key": f"codex:{tid}", "thread_proof": int(proof),
                              "approval_mode": "prompting"}
    if self_agent:  # a live agent process: this test process (the queue tier checks it)
        me = proc.info(os.getpid())
        assert me is not None
        fields.update(agent_pid=os.getpid(), agent_start=me.start)
        ad(w).agent_clients[os.getpid()] = AgentClients(me.start, w.clock.now(), True)
    fields.update(upd)
    return w.store.update_participant(p.id, **fields), m


def attach(w: World, tid: str = TID, view: str | None = "idle", clients: bool = True) -> CodexAdapter:
    a = ad(w)
    a.link_state = "up"
    a.loaded = {tid}
    a.loaded_at = w.clock.now()
    a.clients = Clients(clients, w.clock.now(), 1 if clients else 0, None if clients else "no Codex TUI attached")
    if view is not None:
        a.view[tid] = (view, w.clock.now())
    return a


def queue_tier(w: World) -> CodexAdapter:
    """The link is up but this thread isn't loaded on it: the queue tier."""
    a = attach(w)
    a.loaded = set()
    a.bin_path = "/usr/bin/true"
    return a


def pushes(w: World) -> list[Push]:
    return [x for x in w.take() if isinstance(x, Push)]


def batch(**kw: Any) -> Batch:
    base: dict[str, Any] = {f.name: None for f in dataclasses.fields(Batch)}
    base.update(id=1, membership_id=1, path="turn_start", kind="wake", budget_counted=True, state="offered",
                created_at=1.0)
    base.update(kw)
    return Batch(**base)


def no_lsof(monkeypatch: pytest.MonkeyPatch, a: CodexAdapter) -> None:
    """The send guard's fresh lsof look keeps the clients the test set up."""
    async def keep() -> Clients | None:
        return a.clients

    monkeypatch.setattr(a, "refresh_clients", keep)


class FakeRpc:
    """A connection to a scripted app-server: every request is checked against
    the real allowlist before it is 'sent'."""

    def __init__(self, threads: dict[str, dict[str, Any]] | None = None, *,
                 request_error: BaseException | None = None, read_error: BaseException | None = None):
        self.threads = threads or {}
        self.request_error = request_error
        self.read_error = read_error
        self.sent: list[tuple[str, dict[str, Any] | None]] = []
        self.closed = False

    async def read_thread(self, tid: str, include_turns: bool = False, timeout: float = 10.0) -> dict[str, Any]:
        codex_rpc.check_request("thread/read", codex_rpc.thread_read_params(tid, include_turns))
        if self.read_error is not None:
            raise self.read_error
        return json.loads(json.dumps(self.threads.get(tid, {})))

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 10.0) -> Any:
        codex_rpc.check_request(method, params)
        self.sent.append((method, params))
        if self.request_error is not None:
            raise self.request_error
        if method == "turn/steer" and params is not None:
            th = self.threads[params["threadId"]]
            th["turns"][-1]["items"].append({"type": "userMessage", "content": params["input"]})
        return {}

    async def loaded_threads(self, timeout: float = 10.0) -> set[str]:
        if self.read_error is not None:
            raise self.read_error
        return set(self.threads)


def use_rpc(monkeypatch: pytest.MonkeyPatch, r: FakeRpc | BaseException) -> list[str]:
    """Every fresh connection (``one_shot``) goes to ``r``, or fails with it."""
    opened: list[str] = []

    async def one_shot(path: str, fn: Callable[[Any], Any], *, timeout: float = 10.0) -> Any:
        opened.append(path)
        if isinstance(r, BaseException):
            raise r
        return await fn(r)

    monkeypatch.setattr(cx, "one_shot", one_shot)
    return opened


def busy_thread(tid: str = TID, flags: list[str] | None = None) -> dict[str, Any]:
    return {"id": tid, "status": {"type": "active", "activeFlags": list(flags or [])},
            "turns": [{"id": "turn-1", "status": "inProgress", "items": []}]}


async def until(cond: Callable[[], Any], timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    assert cond(), "condition not reached"


# ------------------------------------------------------------ pure helpers
def test_a_homebrew_prefix_that_cannot_be_read_is_not_an_install_dir(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = str(tmp_path / "brew")  # listed as a prefix, but not there
    monkeypatch.setattr(cx, "HOMEBREW_PREFIXES", (root,))
    assert cx._homebrew_install_dir(root, root + "/bin") is False
    assert cx._homebrew_install_dir(root, root) is False  # the prefix itself is not an install dir


def test_no_binary_version_when_nothing_up_to_the_root_names_one() -> None:
    assert cx.bin_version("/codex") is None  # no versioned directory, no package.json up to /


def test_queue_text_that_looks_like_a_flag_is_refused() -> None:
    with pytest.raises(ValueError, match="flag"):
        cx.queue_argv("/usr/bin/codex", TID, "--dangerously-bypass-approvals", None)


def test_no_lsof_on_this_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "lsof").write_text("#!/bin/sh\n")  # there, but not executable
    monkeypatch.setattr(cx, "LSOF_CANDIDATES", (str(tmp_path / "missing"), str(tmp_path / "lsof")))
    assert cx.lsof_bin() is None


def test_parse_lsof_skips_blank_lines_and_unparsable_pids() -> None:
    out = "p12\n\nf3\nd0xa\nn/run/cx.sock\n\npnot-a-pid\nf4\nd0xb\nn->0xa\np13\nf5\nd0xc\nn->0xa\n"
    assert cx.parse_lsof(out) == [(12, "0xa", "/run/cx.sock"), (13, "0xc", "->0xa")]


# --------------------------------------------------------- liveness, routing
def test_live_says_why_a_restarting_or_ended_thread_is_not_live(w: World) -> None:
    p, _m = codex(w)
    a = attach(w)
    assert a.live(p) == (True, "")
    a.orphans[p.id] = Orphan(w.clock.now(), p.agent_pid, p.agent_start)
    assert a.live(p) == (False, "its Codex app-server restarted")
    del a.orphans[p.id]
    a.ended[TID] = w.clock.now()
    assert a.live(p) == (False, "session ended")


def test_a_queue_tier_thread_whose_process_is_the_daemon_is_not_live(w: World) -> None:
    p, _m = codex(w, self_agent=True)
    a = queue_tier(w)
    assert a.live(p) == (True, "")
    a.server_pids = {os.getpid()}  # its process serves the control socket: the daemon, yet not loaded
    assert a.live(p) == (False, "thread not loaded in the Codex daemon")


def test_a_held_queue_tier_thread_is_released_after_idling(w: World) -> None:
    p, _m = codex(w, self_agent=True, status="busy")
    a = queue_tier(w)
    a.suspect[TID] = w.clock.now()
    assert a.live(p) == (False, cx.HOLD_WHY)
    w.clock.advance(cx.DROP_HOLD_S)
    a.agent_clients[os.getpid()] = dataclasses.replace(a.agent_clients[os.getpid()], at=w.clock.now())
    assert a.live(p) == (False, cx.HOLD_WHY)  # busy all along: still held
    p = w.store.update_participant(p.id, status="idle")
    assert a.live(p) == (True, "")


def test_an_open_wait_takes_the_batch_whatever_the_tier(w: World) -> None:
    p, _m = codex(w, proof=False)
    r = ad(w).route(p, WAKE, SimpleNamespace(path="wait", id=5), w.clock.now())
    assert (r.kind, r.path, r.sink_id) == ("sink", "wait", 5)


def test_queue_tier_routing(w: World) -> None:
    gone, _ = codex(w, "codex-gone")  # its agent pid is no live process
    a = queue_tier(w)
    r = a.route(gone, WAKE, None, w.clock.now())
    assert r.kind == "none" and r.reason == "detached? the Codex process is gone"
    p, _m = codex(w, "codex-2", tid=TID2, self_agent=True, status="busy")
    assert a.tier(p) == ("codex:queue", None)
    r = a.route(p, WAKE, None, w.clock.now())
    assert (r.kind, r.reason) == ("defer", "turn still running")
    p = w.store.update_participant(p.id, status="idle")
    a.backoff[p.id] = (w.clock.now() + 5, 1)
    r = a.route(p, WAKE, None, w.clock.now())
    assert (r.kind, r.reason) == ("defer", "codex queue failed; retrying")
    w.clock.advance(6)
    a.agent_clients[os.getpid()] = dataclasses.replace(a.agent_clients[os.getpid()], at=w.clock.now())
    a.loaded_at = w.clock.now()
    assert a.route(p, WAKE, None, w.clock.now()).path == "queue"


def test_no_queue_tier_when_the_fallback_is_off(tmp_path: Path, clock: FakeClock) -> None:
    w = World(tmp_path, clock, Config().replace(codex=CodexCfg(queue_fallback=False)).with_delivery(
        quiet_s=0.0, max_hold_s=0.0))
    p, _m = codex(w, self_agent=True)
    a = queue_tier(w)
    assert a.queue_guard() == (False, None, "queue fallback is off")
    assert a.tier(p) == ("mcp-only", "queue fallback is off")


def test_a_stop_restarts_a_held_threads_idle_clock(w: World) -> None:
    p, _m = codex(w)
    a = attach(w)
    t0 = w.clock.now()
    a.suspect[TID] = t0
    w.clock.advance(40)
    w.hook(p, "Stop", sid=TID)
    assert a.suspect[TID] == t0 + 40  # idle again: the 70 s count from now


def test_only_an_offered_push_of_an_offline_member_expires_early(w: World) -> None:
    p, _m = codex(w)
    a = ad(w)
    now = w.clock.now()
    assert a.expire_due(p, batch(state="confirmed"), now) is None
    assert a.expire_due(p, batch(path="wait"), now) is None
    assert a.expire_due(p, batch(), now) is None
    for path in ("turn_start", "steer", "queue"):
        assert a.expire_due(w.store.update_participant(p.id, status="offline"), batch(path=path), now) == "offline"
    a.steers[9] = SteerState(9, p.id, TID, "yk:b9.x", "turn-1", now)
    a.push_expired(p, batch(id=9, path="steer"), "offline", now)
    assert a.steers == {}


# ---------------------------------------------------- link notes and holds
def test_a_notification_without_a_thread_id_only_counts(w: World) -> None:
    a = ad(w)
    a._on_note("thread/status/changed", {"status": {"type": "idle"}}, w.clock.now())
    a._on_note("thread/closed", {"threadId": ""}, w.clock.now())
    assert a.notes == {"thread/status/changed": 1, "thread/closed": 1}
    assert a.view == {} and a.loaded == set()


def test_an_ended_thread_seen_offline_counts_as_gone(w: World) -> None:
    a = attach(w)
    t_end = w.clock.now()
    a.ended[TID] = t_end
    w.clock.advance(3)
    a._set_view(TID, "offline", w.clock.now())
    assert a.ended_gone[TID] == t_end + 3 and a.thread_view(TID) == "offline"
    a._set_view(TID, "offline", w.clock.now() + 9)
    assert a.ended_gone[TID] == t_end + 3  # the first time it was seen gone


def test_holding_no_threads_is_no_hold(w: World) -> None:
    a = wire(w)
    a._hold("control", set(), w.clock.now(), "a TUI disconnected")
    assert a.holds == {} and a.suspect == {}
    assert w.store.count_events("codex_hold") == 0


# ------------------------------------------------------------- transport
async def test_send_refuses_a_bad_thread_id_or_path(w: World) -> None:
    a = wire(w)
    bad, _ = codex(w, "codex-bad", tid="--dangerously")
    with pytest.raises(SendError) as ei:
        await a.send(bad, batch(), "[switchboard] hi")
    assert ei.value.reason == "bad thread id" and ei.value.counted
    p, _m = codex(w)
    with pytest.raises(SendError) as ei:
        await a.send(p, batch(path="wait"), "[switchboard] hi")
    assert ei.value.reason == "no such codex path" and ei.value.counted
    assert a.reroutes == {}  # a counted failure is not a re-route


async def test_turn_start_into_a_thread_that_is_not_idle_is_rerouted(
        w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    p, _m = codex(w)
    a = wire(w)
    attach(w, view="busy")
    no_lsof(monkeypatch, a)
    opened = use_rpc(monkeypatch, FakeRpc())
    with pytest.raises(SendError) as ei:
        await a.send(p, batch(), "[switchboard] hi")
    assert ei.value.reason == "thread not idle" and not ei.value.counted
    assert opened == [] and a.reroutes[p.id] == (0.0, 1)


@pytest.mark.parametrize(("failure", "reason"), [
    (codex_rpc.RpcError(-32000, "server overloaded"), "turn/start error -32000"),
    (ConnectionRefusedError(61, "refused"), "codex control socket unavailable"),
    (codex_rpc.SocketRefused("control socket not found"), "codex control socket unavailable"),
])
async def test_turn_start_failures_back_off(w: World, monkeypatch: pytest.MonkeyPatch,
                                            failure: BaseException, reason: str) -> None:
    p, _m = codex(w)
    a = wire(w)
    attach(w)
    no_lsof(monkeypatch, a)
    use_rpc(monkeypatch, failure)
    with pytest.raises(SendError) as ei:
        await a.send(p, batch(), "[switchboard] hi")
    assert ei.value.reason == reason and ei.value.counted
    until_, n = a.backoff[p.id]
    assert n == 1 and until_ == w.clock.now() + cx.SEND_BACKOFF_S[0]
    assert a._active == {}
    r = a.route(w.p(p), WAKE, None, w.clock.now())
    assert (r.kind, r.reason) == ("defer", "turn/start failed; retrying")


async def test_a_steer_into_an_approval_wait_is_rerouted_and_holds(
        w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    p, _m = codex(w, status="busy")
    a = wire(w)
    attach(w, view="busy")
    no_lsof(monkeypatch, a)
    r = FakeRpc({TID: busy_thread(flags=["waitingOnApproval"])})
    use_rpc(monkeypatch, r)
    with pytest.raises(SendError) as ei:
        await a.send(p, batch(path="steer", kind="priority"), "[switchboard] hi")
    assert ei.value.reason == "waiting on approval" and not ei.value.counted
    assert r.sent == []  # never steered into the prompt
    assert a.thread_view(TID) == "waiting-approval" and TID not in a.no_steer
    assert a.reroutes[p.id] == (0.0, 1) and a.steers == {}


@pytest.mark.parametrize(("failure", "reason"), [
    (codex_rpc.RpcError(-32603, "internal error"), "turn/steer error -32603"),
    (ConnectionResetError(54, "reset"), "codex control socket unavailable"),
])
async def test_steer_failures_other_than_a_turn_change_back_off(
        w: World, monkeypatch: pytest.MonkeyPatch, failure: BaseException, reason: str) -> None:
    p, _m = codex(w, status="busy")
    a = wire(w)
    attach(w, view="busy")
    no_lsof(monkeypatch, a)
    if isinstance(failure, codex_rpc.RpcError):
        r = FakeRpc({TID: busy_thread()}, request_error=failure)
        use_rpc(monkeypatch, r)
    else:
        use_rpc(monkeypatch, failure)
    with pytest.raises(SendError) as ei:
        await a.send(p, batch(path="steer", kind="priority"), "[switchboard] hi")
    assert ei.value.reason == reason and ei.value.counted
    assert a.backoff[p.id][1] == 1 and a.steers == {} and a.reroutes == {}
    assert a.route(w.p(p), PRIO, None, w.clock.now()).kind == "pull"  # backing off: no steer now


async def test_a_steer_whose_turn_ended_meanwhile_is_settled_from_history(
        w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    p, m = codex(w, status="busy")
    a = wire(w)
    attach(w, view="busy")
    msg = w.human("add a test for parse_port")
    [push] = pushes(w)
    assert push.path == "steer"
    b = w.store.get_batch(push.batch_id)
    no_lsof(monkeypatch, a)
    r = FakeRpc({TID: busy_thread()})
    use_rpc(monkeypatch, r)
    a.view[TID] = ("idle", w.clock.now())  # the status link saw the turn end while we steered
    assert await a.send(w.p(p), b, push.text) is None
    [(method, params)] = r.sent
    assert method == "turn/steer" and params is not None and params["expectedTurnId"] == "turn-1"
    await until(lambda: not a._tasks)
    assert a.steers == {}
    b = w.store.get_batch(push.batch_id)
    assert (b.state, b.evidence) == ("confirmed", "rpc:steer+history")
    assert w.delivery(m, msg)["state"] == "in_context"


async def test_settling_with_no_readable_history(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """The history read fails: a steer that saw an approval wait may have been
    declined with it (expired, re-delivered); any other is taken as landed."""
    p, m = codex(w, status="busy")
    a = wire(w)
    attach(w, view="busy")
    w.human("first")
    [push] = pushes(w)

    async def unreadable(tid: str, include_turns: bool = False) -> dict[str, Any]:
        raise ConnectionRefusedError(61, "refused")

    monkeypatch.setattr(a, "_read", unreadable)
    tok = w.engine.token(w.store.get_batch(push.batch_id))
    a.steers[push.batch_id] = SteerState(push.batch_id, p.id, TID, tok, "turn-1", w.clock.now(), approval_seen=True)
    await a._settle(TID)
    b = w.store.get_batch(push.batch_id)
    assert (b.state, b.expire_reason) == ("expired", "steer_approval")
    [again] = pushes(w)  # re-delivered, as another steer
    assert again.path == "steer" and again.batch_id != push.batch_id
    tok = w.engine.token(w.store.get_batch(again.batch_id))
    a.steers[again.batch_id] = SteerState(again.batch_id, p.id, TID, tok, "turn-1", w.clock.now())
    await a._settle(TID)
    b = w.store.get_batch(again.batch_id)
    assert (b.state, b.evidence) == ("confirmed", "rpc:steer+idle")


async def test_settling_skips_a_steer_settled_meanwhile_and_is_a_no_op_without_steers(
        w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    p, _m = codex(w, status="busy")
    a = wire(w)
    attach(w, view="busy")
    w.human("first")
    [push] = pushes(w)
    tok = w.engine.token(w.store.get_batch(push.batch_id))
    a.steers[push.batch_id] = SteerState(push.batch_id, p.id, TID, tok, "turn-1", w.clock.now())
    a.steers[4242] = SteerState(4242, p.id, TID, "yk:b4242.x", "turn-1", w.clock.now())
    reads: list[str] = []

    async def read(tid: str, include_turns: bool = False) -> dict[str, Any]:
        reads.append(tid)
        a.steers.pop(4242)  # another settle of this thread took it while we read
        return {"turns": [{"id": "turn-1", "items": [{"text": tok}]}]}

    confirmed: list[tuple[int, str]] = []
    real = w.engine.on_confirm
    monkeypatch.setattr(w.engine, "on_confirm", lambda bid, why: confirmed.append((bid, why)) or real(bid, why))
    monkeypatch.setattr(a, "_read", read)
    await a._settle(TID)
    assert reads == [TID] and confirmed == [(push.batch_id, "rpc:steer+history")] and a.steers == {}
    await a._settle(TID2)  # nothing outstanding on that thread: no read at all
    assert reads == [TID]


def _script(path: Path, body: str) -> str:
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


async def queue_world(w: World, monkeypatch: pytest.MonkeyPatch, bin_path: str | None) -> tuple[Any, CodexAdapter]:
    p, _m = codex(w, self_agent=True)
    a = wire(w)
    queue_tier(w)
    a.bin_path = bin_path
    no_lsof(monkeypatch, a)
    return w.p(p), a


async def test_no_queue_send_without_a_codex_binary(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    looked: list[str] = []
    monkeypatch.setattr(cx, "resolve_codex", lambda name: looked.append(name))
    p, a = await queue_world(w, monkeypatch, None)
    with pytest.raises(SendError) as ei:
        await a.send(p, batch(path="queue"), "[switchboard] hi")
    assert ei.value.reason.startswith("queue guard: codex binary not found") and not ei.value.counted
    assert looked == ["codex"] and a.reroutes[p.id] == (0.0, 1)


async def test_a_flag_like_queue_text_is_never_run(
        w: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ran = tmp_path / "ran"
    p, a = await queue_world(w, monkeypatch, _script(tmp_path / "codex", f"#!/bin/sh\ntouch {ran}\n"))
    with pytest.raises(SendError) as ei:
        await a.send(p, batch(path="queue"), "--config approval_policy=never")
    assert ei.value.reason == "queue text refused" and ei.value.counted
    assert not ran.exists()


async def test_a_codex_queue_that_cannot_start(w: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    exe = tmp_path / "codex"
    exe.write_bytes(b"\x00\x01\x02 not a program")
    exe.chmod(0o755)
    p, a = await queue_world(w, monkeypatch, str(exe))
    with pytest.raises(SendError) as ei:
        await a.send(p, batch(path="queue"), "[switchboard] hi")
    assert ei.value.reason == "codex queue could not start" and ei.value.counted
    assert a.backoff[p.id][1] == 1


async def test_a_codex_queue_that_fails(w: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    p, a = await queue_world(w, monkeypatch, _script(tmp_path / "codex", "#!/bin/sh\nexit 3\n"))
    with pytest.raises(SendError) as ei:
        await a.send(p, batch(path="queue"), "[switchboard] hi")
    assert ei.value.reason == "codex queue exit 3" and ei.value.counted
    assert a.backoff[p.id][1] == 1
    a.bin_path = _script(tmp_path / "codex-ok", "#!/bin/sh\nexit 0\n")
    a.backoff.clear()
    assert await a.send(p, batch(path="queue"), "[switchboard] hi") is None
    assert a.backoff == {} and a.reroutes == {}


async def test_a_codex_queue_that_hangs_is_killed(w: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    p, a = await queue_world(w, monkeypatch, _script(tmp_path / "codex", "#!/bin/sh\nexec sleep 30\n"))
    monkeypatch.setattr(cx, "QUEUE_TIMEOUT_S", 0.2)
    children: list[Any] = []
    real = asyncio.create_subprocess_exec

    async def spawn(*argv: str, **kw: Any) -> Any:
        child = await real(*argv, **kw)
        children.append(child)
        return child

    monkeypatch.setattr(cx.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(SendError) as ei:
        await a.send(p, batch(path="queue"), "[switchboard] hi")
    assert ei.value.reason == "codex queue timed out" and ei.value.counted
    [child] = children
    assert await asyncio.wait_for(child.wait(), 5) != 0  # killed, not left running
    assert a.backoff[p.id][1] == 1


# ------------------------------------------------------------- the link
async def test_the_link_retries_a_socket_that_refuses_connections(
        w: World, monkeypatch: pytest.MonkeyPatch, tmp_home: Path) -> None:
    monkeypatch.setattr(cx, "LINK_BACKOFF_S", (0.01, 0.02))
    a = wire(w)
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(tmp_home / "cx.sock"))  # ours, but nobody listens
    a.sock = str(tmp_home / "cx.sock")
    tries: list[int] = []
    monkeypatch.setattr(a, "refresh_tiers", lambda: tries.append(1))  # once per failed try
    task = asyncio.get_running_loop().create_task(a._link_loop())
    try:
        await until(lambda: len(tries) >= 3)  # it keeps retrying, backing off
        assert a.link_state == "down" and a.link_note == "connect failed" and a.rpc is None
        assert a.status_summary().startswith("down (connect failed; retrying)")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        s.close()
    [ev] = w.store.recent_events(kinds=["codex_link"], limit=5)
    assert ev.data == {"state": "down", "note": "connect failed"}


async def test_the_link_survives_a_failure_while_up(
        w: World, monkeypatch: pytest.MonkeyPatch, tmp_home: Path, caplog: pytest.LogCaptureFixture) -> None:
    from websockets.asyncio.server import unix_serve

    async def handler(ws: Any) -> None:
        async for frame in ws:
            msg = json.loads(frame)
            if msg.get("method") == "initialize":
                await ws.send(json.dumps({"id": msg["id"], "result": {"userAgent": "fake/0.157.0"}}))

    monkeypatch.setattr(cx, "LINK_BACKOFF_S", (0.01, 0.02))
    monkeypatch.setattr(cx, "resolve_codex", lambda name: None)
    a = wire(w)
    a.sock = str(tmp_home / "cx.sock")
    server = await unix_serve(handler, a.sock, ping_interval=None, compression=None)
    polls: list[int] = []

    async def poll() -> None:
        polls.append(1)
        if len(polls) == 1:
            raise RuntimeError("poll bug")

    monkeypatch.setattr(a, "poll", poll)
    task = asyncio.get_running_loop().create_task(a._link_loop())
    try:
        with caplog.at_level(logging.ERROR, logger="switchboard.codex"):
            await until(lambda: len(polls) >= 2 and a.link_state == "up")
        assert "codex link failed" in caplog.text
        assert a.server_version == "0.157.0" and a.rpc is not None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.close()
        await server.wait_closed()
    states = [e.data.get("note") or e.data["state"] for e in reversed(w.store.recent_events(kinds=["codex_link"],
                                                                                              limit=10))]
    # up, lost (the failure), up again, lost (the cancel); the version recorded once
    assert states == ["up", "version", "connection lost", "up", "connection lost"]
    assert a.rpc is None and a.link_state == "down"


async def test_a_failed_loaded_list_changes_nothing(w: World) -> None:
    a = wire(w)
    a.rpc = FakeRpc(read_error=ConnectionResetError(54, "reset"))  # type: ignore[assignment]
    a.loaded = {TID}
    await a.poll()
    assert a.loaded == {TID} and a.loaded_at is None


async def test_a_poll_forgets_unloaded_threads_and_ignores_an_ended_one_it_asked_about_too_early(
        w: World) -> None:
    a = wire(w)
    a.link_state = "up"
    a.rpc = FakeRpc({TID: {"id": TID, "ephemeral": True, "threadSource": "thread_title"}})  # type: ignore[assignment]
    a.internal = {TID2: False}  # learned before; now unloaded
    a.ended[TID] = w.clock.now()  # a SessionEnd at the very time the list was asked for
    await a.poll()
    assert a.loaded == {TID} and a.loaded_at == w.clock.now()
    assert a.internal == {TID: True}  # the app-server's own title thread, learned; TID2 forgotten
    assert TID in a.ended and TID not in a.ended_gone  # a list no later than the SessionEnd proves nothing


async def test_learning_threads_stops_on_a_closed_link_and_skips_unreadable_ones(w: World) -> None:
    a = ad(w)
    a.loaded = {TID, TID2}
    r = FakeRpc(read_error=ConnectionResetError(54, "reset"))
    await a._learn_threads(r)  # type: ignore[arg-type]
    assert a.internal == {}  # unknown: counted as a user's thread
    r.closed = True
    r.read_error = None
    r.threads = {TID: {"id": TID}}
    await a._learn_threads(r)  # type: ignore[arg-type]
    assert a.internal == {}


# ------------------------------------------------------------------ lsof
async def test_without_lsof_nobody_is_attached(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    a = ad(w)
    a.agent_clients = {1: AgentClients(1.0, 0.0, True)}
    monkeypatch.setattr(cx, "lsof_bin", lambda: None)
    c = await a.refresh_clients()
    assert (c.ok, c.n, c.why) == (False, 0, "can't run lsof to see who is attached")
    assert a.clients is c and a.agent_clients == {}


async def test_a_failing_lsof_means_nobody_is_attached(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    a = ad(w)
    monkeypatch.setattr(cx, "lsof_bin", lambda: "/usr/sbin/lsof")

    def boom(lsof: str) -> str:
        raise OSError("lsof crashed")

    monkeypatch.setattr(cx, "_run_lsof", boom)
    c = await a.refresh_clients()
    assert (c.ok, c.why) == (False, "lsof failed")


async def test_queue_tier_app_servers_are_checked_for_their_own_tuis(
        w: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """One lsof pass: the control socket's TUIs, and for each queue-tier thread's
    own process whether a Codex TUI is on it (its own socket's clients, or its
    parent when it has no socket)."""
    a = wire(w)
    a.sock = str(tmp_path / "control.sock")
    agent_sock = "/run/agent300.sock"
    tids = {300: TID, 500: TID2, 700: "019a0000-0000-7000-8000-00000000c0f3",
            900: "019a0000-0000-7000-8000-00000000c0f4", 950: "019a0000-0000-7000-8000-00000000c0f5"}
    parts = {pid: codex(w, f"codex-{pid}", tid=tid, agent_pid=pid, agent_start=float(pid))[0]
             for pid, tid in tids.items()}
    table = {  # pid -> (ppid, argv)
        100: (1, "/opt/homebrew/bin/codex app-server --listen unix://control"),
        200: (1, "/opt/homebrew/bin/codex"),
        300: (1, "/opt/homebrew/bin/codex app-server --listen unix:///run/agent300.sock"),
        400: (1, "/opt/homebrew/bin/codex --remote unix:///run/agent300.sock"),
        500: (600, "/opt/homebrew/bin/codex app-server"),
        600: (1, "/opt/homebrew/bin/codex"),
        700: (800, "/opt/homebrew/bin/codex app-server"),
        800: (1, "/sbin/launchd"),
        900: (1, "/opt/homebrew/bin/codex"),
    }
    lsof = [(100, "0xa1", a.sock), (200, "0xb1", "->0xa1"), (300, "0xc1", agent_sock), (400, "0xd1", "->0xc1")]

    def lsof_out() -> str:
        return "".join(f"p{pid}\nf3\nd{dev}\nn{name}\n" for pid, dev, name in lsof)

    def info(pid: int) -> proc.ProcInfo | None:
        row = table.get(pid)
        return None if row is None else proc.ProcInfo(pid=pid, ppid=row[0], start=float(pid), uid=os.getuid())

    monkeypatch.setattr(cx, "lsof_bin", lambda: "/usr/sbin/lsof")
    monkeypatch.setattr(cx, "_run_lsof", lambda _bin: lsof_out())
    monkeypatch.setattr(cx.proc, "info", info)
    monkeypatch.setattr(cx.proc, "argv_many", lambda infos: {i.pid: table[i.pid][1] for i in infos})
    a._seen[("agent", 12345, 1.0)] = frozenset({1})  # an agent process no longer joined

    c = await a.refresh_clients()
    assert (c.ok, c.n, c.pids) == (True, 1, frozenset({200}))
    assert a.server_pids == {100}
    acs = a.agent_clients
    assert set(acs) == {300, 500, 700, 900}  # 950 is gone
    assert (acs[300].ok, acs[300].server, acs[300].n) == (True, True, 1)  # its own TUI on its socket
    assert (acs[500].ok, acs[500].server) == (True, True)  # its parent is a TUI
    assert (acs[700].ok, acs[700].why) == (False, "no Codex TUI attached")
    assert (acs[900].ok, acs[900].server) == (True, False)  # the TUI itself
    assert ("agent", 12345, 1.0) not in a._seen and ("agent", 300, 300.0) in a._seen
    assert a.live(w.p(parts[300]), w.clock.now()) == (True, "")
    assert a.live(w.p(parts[700]), w.clock.now()) == (False, "no Codex TUI attached")
    assert a.live(w.p(parts[950]), w.clock.now()) == (False, "the Codex process is gone")

    lsof.pop()  # the TUI on agent 300's own app-server quit: its thread is held
    await a.refresh_clients()
    assert (a.agent_clients[300].ok, a.agent_clients[300].why) == (False, "no Codex TUI attached to its app-server")
    assert TID in a.suspect and a.holds[("agent", 300, 300.0)].tids == {TID}


async def test_the_clients_loop_survives_an_error(
        w: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(cx, "CLIENTS_POLL_S", 0.01)
    monkeypatch.setattr(cx, "resolve_codex", lambda name: None)
    codex(w)
    a = wire(w)
    looks: list[int] = []

    async def refresh() -> Clients | None:
        looks.append(1)
        if len(looks) == 1:
            raise RuntimeError("lsof parse bug")
        return a.clients

    monkeypatch.setattr(a, "refresh_clients", refresh)
    task = asyncio.get_running_loop().create_task(a._clients_loop())
    try:
        with caplog.at_level(logging.ERROR, logger="switchboard.codex"):
            await until(lambda: len(looks) >= 2)
        assert "codex client check failed" in caplog.text
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------- thread proof
def test_on_joined_ignores_other_harnesses(w: World) -> None:
    a = wire(w)
    p, _m = w.agent("bot", harness="test")
    a.ended[TID] = w.clock.now()
    a.on_joined(p, "0" * 16, True)
    assert TID in a.ended and a._proofs == {}


def test_reprove_is_bounded_and_needs_a_loop(w: World) -> None:
    p, _m = codex(w, proof=False, bind_nonce="n1")
    a = wire(w)
    a._reprove(p)  # no running loop (a synchronous caller): the try is counted, nothing scheduled
    assert a._proof_tries == {(p.id, "n1"): 1} and a._proofs == {}
    a._proof_tries[(p.id, "n1")] = cx.PROOF_RETRY_MAX
    a._reprove(p)
    assert a._proof_tries == {(p.id, "n1"): cx.PROOF_RETRY_MAX}


async def test_a_second_join_replaces_the_pending_proof(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    p, _m = codex(w, proof=False, bind_nonce="n1")
    a = wire(w)
    no_lsof(monkeypatch, a)
    a.on_joined(w.p(p), "n1", True)
    first = a._proofs[p.id]
    p = w.store.update_participant(p.id, bind_nonce="n2")
    a.on_joined(p, "n2", True)
    second = a._proofs[p.id]
    assert second is not first
    await asyncio.gather(first, return_exceptions=True)
    assert first.cancelled() and not second.done()
    await a.stop()
    assert second.cancelled() and a._proofs == {}


async def test_a_proof_stops_early_for_a_gone_rejoined_or_proven_member(
        w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    p, _m = codex(w, proof=False, bind_nonce="n1")
    a = wire(w)

    async def read(tid: str, include_turns: bool = False) -> dict[str, Any]:
        raise AssertionError("no read expected")

    monkeypatch.setattr(a, "_read", read)
    await a._prove(p.id, TID, "old-nonce", (0.0,))  # it joined again since: another proof runs
    await a._prove(424242, TID, "n1", (0.0,))  # no such participant
    w.store.update_participant(p.id, thread_proof=1)
    await a._prove(p.id, TID, "n1", (0.0,))  # proven meanwhile
    assert w.store.count_events("bind") == 0


async def test_a_proof_clears_a_session_end_and_resyncs_the_status(
        w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    p, m = codex(w, proof=False, bind_nonce="n1")
    a = wire(w)
    attach(w)
    w.hook(p, "SessionEnd", sid=TID, reason="other")
    assert w.p(p).status == "offline" and TID in a.ended
    join_result = {"type": "mcpToolCall", "server": "switchboard", "tool": "join", "status": "completed",
                   "result": {"content": [{"type": "text", "text": "joined #build yk:jn1"}]}}

    async def read(tid: str, include_turns: bool = False) -> dict[str, Any]:
        return {"turns": [{"id": "t", "status": "completed", "items": [join_result]}]}

    monkeypatch.setattr(a, "_read", read)
    await a._prove(p.id, TID, "n1", (0.0,))
    q = w.p(p)
    assert q.thread_proof and TID not in a.ended and q.status == "idle"
    [ev] = w.store.recent_events(kinds=["codex_session"], limit=5)
    assert ev.data == {"what": "running_again", "why": "thread proof"}


async def test_a_proof_before_the_first_tui_check_is_announced_after_it(
        w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """A proof that passes while the look after the join is still running would announce
    "codex:daemon (detached?)" for a TUI nobody had looked for: the notice waits for the look."""
    p, m = codex(w, proof=False, bind_nonce="n1")
    a = wire(w)
    attach(w)
    a.clients = None
    w.store.add_event("join", room_id=m.room_id, participant_id=p.id, membership_id=m.id,
                      data={"verifying": True})
    join_result = {"type": "mcpToolCall", "server": "switchboard", "tool": "join", "status": "completed",
                   "result": {"content": [{"type": "text", "text": "joined #build yk:jn1"}]}}

    async def read(tid: str, include_turns: bool = False) -> dict[str, Any]:
        return {"turns": [{"id": "t", "status": "completed", "items": [join_result]}]}

    def notices() -> list[str]:
        return [x.text for x in w.actions if isinstance(x, Notice)]

    monkeypatch.setattr(a, "_read", read)
    await a._prove(p.id, TID, "n1", (0.0,))
    assert w.p(p).thread_proof and notices() == []
    a.refresh_tiers()  # not looked yet: still waiting
    assert notices() == []
    a.clients = Clients(True, w.clock.now(), 1, None)
    a.refresh_tiers()
    a.refresh_tiers()
    assert notices() == ["codex-1 is verified: codex:daemon"]


# ------------------------------------------------------- daemon restarts
def test_defer_end_is_codex_only_and_needs_the_broker_state(w: World) -> None:
    a = ad(w)
    p, _m = codex(w)
    assert a.defer_end(p) is False  # not started: no state to keep it in
    wire(w)
    q, _ = w.agent("bot", harness="test")
    assert a.defer_end(q) is False


def test_the_grace_ends_with_a_last_look_that_rebinds(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    p, _m = codex(w)
    a = wire(w)
    attach(w)
    w.store.update_participant(p.id, agent_pid=999_999, agent_start=1.0)
    assert a.defer_end(w.p(p)) is True and p.id in a.orphans
    w.clock.advance(a.cfg.codex.restart_grace_s + 1)
    attach(w)  # the new daemon has the thread loaded
    me = proc.info(os.getpid())
    assert me is not None
    monkeypatch.setattr(a, "_new_agent", lambda: (os.getpid(), me.start))
    w.take()
    assert a.defer_end(w.p(p)) is True  # re-bound by the last look, not ended
    assert p.id not in a.orphans and w.p(p).agent_pid == os.getpid()
    assert [x.text for x in w.take() if isinstance(x, Notice)] == [
        "codex-1 reconnected after a Codex daemon restart"]


def test_mcp_hello_bookkeeping(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cx, "FRESH_AGENTS_MAX", 2)
    p, _m = codex(w)
    a = wire(w)
    for pid in (11, 12, 13):
        a.on_mcp_hello(SimpleNamespace(harness="codex", agent_pid=pid, agent_start=float(pid)), [])
    assert list(a.fresh_agents) == [(12, 12.0), (13, 13.0)]  # the oldest hello is dropped
    before = w.p(p)
    a.on_mcp_hello(SimpleNamespace(harness="codex", agent_pid=os.getpid(), agent_start=0.0), [before])
    assert w.p(p) == before  # not restarting: a hello re-binds nothing


def test_rebind_bookkeeping_without_state_or_members(w: World) -> None:
    a = ad(w)
    p, _m = codex(w)
    o = Orphan(w.clock.now(), p.agent_pid, p.agent_start)
    a.orphans[p.id] = o
    a._rebind(p, o, (1, 1.0), "loaded")  # not started: nothing to re-bind through
    a.refresh_tiers()
    assert a.orphans == {p.id: o} and w.p(p).agent_pid == p.agent_pid
    wire(w)
    a.orphans = {424242: o}  # a participant that no longer exists
    a._rebind_ready()
    assert a.orphans == {}
    a.orphans = {424243: o}
    a._rejoins = {424244: o}
    a.refresh_tiers()  # neither is a joined member any more
    assert a.orphans == {} and a._rejoins == {}


async def test_stop_cancels_everything_and_closes_the_link_quietly(w: World) -> None:
    a = ad(w)

    class BrokenLink:
        closes = 0

        async def close(self) -> None:
            BrokenLink.closes += 1
            raise OSError("already gone")

    a.rpc = BrokenLink()  # type: ignore[assignment]
    main = asyncio.get_running_loop().create_task(asyncio.sleep(60))
    a._main = [main]
    await a.stop()
    assert main.cancelled() and BrokenLink.closes == 1 and a.rpc is None

