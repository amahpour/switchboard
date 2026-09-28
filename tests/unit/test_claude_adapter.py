"""The Claude adapter with a FakeClock (DESIGN.md §9.2, §8.7): routing between
the inbox and hook context, the registry-driven approval hold, confirmation
by UserPromptSubmit, the event-based expiries and the warn notice."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeClock
from engine_world import KEY, World
from switchboard import envelope
from switchboard.adapters.claude import (
    REGISTRY_IDLE_GRACE_S,
    ClaudeAdapter,
    RegView,
    registry_transition,
    registry_view,
)
from switchboard.claude_registry import read_registry
from switchboard.config import Config
from switchboard.models import Notice, Push, Release

SOCK = "/tmp/yk-test-inbox.sock"


class FakeConn:
    closed = False

    def __init__(self) -> None:
        self.pushes: list[tuple[str, dict[str, Any]]] = []

    def push(self, kind: str, data: dict[str, Any]) -> None:
        self.pushes.append((kind, data))


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def ad(w: World) -> ClaudeAdapter:
    a = w.engine.adapters["claude"]
    assert isinstance(a, ClaudeAdapter)
    return a


def claude(w: World, name: str = "claude-1", *, status: str = "idle", mode: str = "prompting",
           attached: bool = True, registry: str | None = "idle"):
    p, m = w.agent(name, harness="claude", status=status, hooks=True)
    p = w.store.update_participant(p.id, claude_socket=SOCK, approval_mode=mode)
    conn = FakeConn()
    if attached:
        ad(w).attach(p.mcp_pid, p.mcp_start, conn)
    if registry is not None:
        reg(w, p, registry)
    return p, m, conn


def reg(w: World, p: Any, status: str, since: float | None = None) -> None:
    data: dict[str, Any] = {"pid": p.agent_pid, "status": status}
    if since is not None:
        data["statusUpdatedAt"] = int(since * 1000)
    ad(w).observe(p.agent_pid, data, w.clock.now())


def pushes(w: World) -> list[Push]:
    return [a for a in w.take() if isinstance(a, Push)]


def tok(text: str) -> tuple[int, str]:
    m = envelope.TOKEN_RE.search(text)
    assert m, text
    return int(m.group(1)), m.group(2)


# ------------------------------------------------------------- pure pieces
def test_registry_transition_table() -> None:
    t0 = 100.0

    def v(status: str, since: float = t0) -> RegView:
        return RegView(status=status, read_at=t0 + 5, since=since)

    now = t0 + 5
    assert registry_transition("busy", t0 - 1, v("waiting"), now) == ("waiting-approval", False)
    assert registry_transition("idle", t0 - 1, v("waiting"), now) == ("waiting-approval", False)
    assert registry_transition("waiting-approval", t0 - 1, v("waiting"), now) is None
    # the prompt went away: declined (the turn ended, no Stop hook) or approved (tool runs)
    assert registry_transition("waiting-approval", t0 - 1, v("idle"), now) == ("idle", True)
    assert registry_transition("waiting-approval", t0 - 1, v("busy"), now) == ("busy", False)
    # a Stop-less turn end (Esc): idle for the grace period since after the last hook
    assert registry_transition("busy", t0 - 1, v("idle"), now) == ("idle", True)
    assert registry_transition("busy", t0 + 1, v("idle"), now) is None  # a hook came after
    assert registry_transition("busy", t0 - 1, v("idle"), t0 + REGISTRY_IDLE_GRACE_S / 2) is None
    assert registry_transition("busy", None, v("idle"), now) is None  # no hooks: no inference
    assert registry_transition("idle", t0 - 1, v("busy"), now) is None  # busy comes from hooks
    assert registry_transition("busy", t0 - 1, v("something-new"), now) is None


def test_registry_view_uses_status_updated_at_in_ms() -> None:
    v = registry_view({"status": "idle", "statusUpdatedAt": 1790000000123}, None, 5.0)
    assert v.status == "idle" and v.since == pytest.approx(1790000000.123) and v.read_at == 5.0
    v2 = registry_view({"status": "idle"}, v, 9.0)
    assert v2.since == v.since  # same status, no timestamp: keeps the first sighting
    v3 = registry_view({"status": "busy"}, v2, 9.5)
    assert v3.since == 9.5
    assert registry_view({"status": 7}, None, 1.0).status is None


def test_read_registry_only_our_regular_files(tmp_path: Path) -> None:
    (tmp_path / "42.json").write_text('{"status": "idle", "pid": 42}')
    assert read_registry(str(tmp_path), 42) == {"status": "idle", "pid": 42}
    assert read_registry(str(tmp_path), 43) is None
    (tmp_path / "44.json").write_text("not json")
    assert read_registry(str(tmp_path), 44) is None
    os.mkfifo(tmp_path / "45.json")
    assert read_registry(str(tmp_path), 45) is None  # never blocks on or reads a fifo


def test_route_matrix(w: World, clock: FakeClock) -> None:
    p, m, _c = claude(w)
    a = ad(w)
    wake = Release(items=(), kind="wake", counted=True, reason="human")
    prio = Release(items=(), kind="priority", counted=False, reason="human")
    now = clock.now()
    assert a.route(w.p(p), wake, None, now).kind == "push"
    assert a.route(w.p(p), prio, None, now).kind == "pull"  # prompting: hook context only
    pb = w.store.update_participant(p.id, approval_mode="bypass")
    reg(w, p, "busy")
    assert a.route(pb, prio, None, now).path == "inbox"  # bypass: the inbox mid-task
    # ... only while a fresh registry read says the turn is running
    assert a.route(pb, prio, None, now + 0.6).kind == "pull"  # stale read
    reg(w, p, "waiting")  # e.g. the human switched modes and a prompt opened
    assert a.route(pb, prio, None, now).kind == "pull"
    reg(w, p, "busy")
    pu = w.store.update_participant(p.id, approval_mode="unknown")
    assert a.route(pu, prio, None, now).kind == "pull"  # unknown is treated like prompting here
    reg(w, p, "idle")
    # the registry must be fresh and idle for an idle wake
    assert a.route(w.p(p), wake, None, now + 0.6).kind == "defer"
    assert a.route(w.p(p), wake, None, now + 5).kind == "none"
    reg(w, p, "busy")
    assert a.route(w.p(p), wake, None, clock.now()).kind == "defer"
    reg(w, p, "idle")
    busy = w.store.update_participant(p.id, status="busy")
    assert a.route(busy, wake, None, clock.now()).kind == "defer"
    nohooks = w.store.update_participant(p.id, status="idle", hooks_seen_at=None)
    r = a.route(nohooks, wake, None, clock.now())
    assert r.kind == "none" and "install claude" in r.reason


def test_unattached_session_is_the_hook_tier(w: World) -> None:
    p, m, _c = claude(w, attached=False)
    assert ad(w).tier(w.p(p)) == ("claude:hook", None)
    w.human("anyone?")
    assert pushes(w) == [] and w.engine.parked_reason(m.id)
    p2, _m2, _c2 = claude(w, "claude-2")
    assert ad(w).tier(w.p(p2)) == ("claude:inbox", None)


# ---------------------------------------------------------- engine + adapter
def test_idle_wake_goes_to_the_inbox_and_user_prompt_submit_confirms(w: World, clock: FakeClock) -> None:
    p, m, _c = claude(w)
    msg = w.human("please look at parse_port")
    [push] = pushes(w)
    assert push.path == "inbox" and push.room == "#build" and push.sender == "alice"
    assert f"id={msg.id} " in push.text and push.text.startswith("[switchboard]")
    b = w.store.get_batch(push.batch_id)
    assert (b.kind, b.wake_kind, b.wake_reason, b.budget_counted) == ("wake", "idle_wake", "human", True)
    assert w.states(m)[msg.id] == "offered"
    # Claude starts a turn from the frame: its UserPromptSubmit prompt is the body
    clock.advance(0.04)
    t_ups = clock.now()
    w.hook(p, "UserPromptSubmit", gen="g1", t=t_ups, tokens=(tok(push.text),), permission_mode="default")
    b = w.store.get_batch(push.batch_id)
    assert b.state == "confirmed" and b.evidence == "hook:UserPromptSubmit" and b.turn_start_at == t_ups
    assert w.states(m)[msg.id] == "in_context" and w.p(p).status == "busy"
    ev = w.store.recent_events(kinds=("turn_start",), limit=5)
    assert ev and ev[0].data.get("batch_id") == push.batch_id


def test_a_foreign_token_never_confirms_an_inbox_frame(w: World) -> None:
    p, m, _c = claude(w)
    p2, _m2, _c2 = claude(w, "claude-2", status="busy")
    w.human("x")
    push = next(x for x in pushes(w) if x.participant_id == p.id)
    w.hook(p2, "UserPromptSubmit", tokens=(tok(push.text),))
    assert w.store.get_batch(push.batch_id).state == "offered"


def test_registry_not_idle_defers_without_parking(w: World, clock: FakeClock) -> None:
    p, m, _c = claude(w, registry="busy")
    w.human("x")
    assert pushes(w) == [] and w.engine.parked_reason(m.id) is None
    reg(w, p, "idle")
    w.actions += w.engine.evaluate_participant(p.id)
    [push] = pushes(w)
    assert push.path == "inbox"


def test_mid_task_prompting_member_gets_hook_context_not_the_inbox(w: World) -> None:
    p, m, _c = claude(w, status="busy")
    msg = w.human("also check the tests")
    assert pushes(w) == []
    out = w.hook(p, "PostToolUse", ok=True)
    assert out is not None and out.kind == "context" and f"id={msg.id} " in out.text
    assert w.store.get_batch(out.batch_id).path == "hook_ctx"
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    assert w.states(m)[msg.id] == "in_context"


def test_mid_task_bypass_member_gets_the_inbox(w: World, clock: FakeClock) -> None:
    p, m, _c = claude(w, status="busy", mode="bypass", registry="busy")
    msg = w.human("stop and check this")
    [push] = pushes(w)
    b = w.store.get_batch(push.batch_id)
    assert push.path == "inbox" and b.kind == "priority" and not b.budget_counted
    # a PostToolUse in the meantime can't claim it twice (one offer at a time)
    assert w.hook(p, "PostToolUse", ok=True) is None
    # it lands at the next tool boundary as a queued prompt: inside the turn, not a turn start
    w.hook(p, "UserPromptSubmit", tokens=(tok(push.text),))
    b = w.store.get_batch(push.batch_id)
    assert w.states(m)[msg.id] == "in_context" and b.state == "confirmed" and b.turn_start_at is None
    ev = w.store.recent_events(kinds=("turn_start",), limit=5)
    assert ev and ev[0].data.get("batch_id") is None


def test_mid_task_bypass_member_with_a_waiting_registry_gets_hook_context(w: World) -> None:
    p, m, _c = claude(w, status="busy", mode="bypass", registry="waiting")
    msg = w.human("mode switched, prompt open")
    assert pushes(w) == []
    out = w.hook(p, "PostToolUse", ok=True, permission_mode="bypassPermissions")
    assert out is not None and f"id={msg.id} " in out.text and w.store.get_batch(out.batch_id).path == "hook_ctx"


def test_waiting_approval_holds_every_path_until_the_prompt_clears(w: World, clock: FakeClock) -> None:
    p, m, _c = claude(w, status="busy")
    # the registry says a permission prompt is open
    reg(w, p, "waiting")
    view = ad(w).registry[("", p.agent_pid)]  # keyed (host, pid); '' is this machine
    tr = registry_transition(w.p(p).status, w.p(p).hooks_seen_at, view, clock.now())
    assert tr == ("waiting-approval", False)
    w.actions += w.engine.set_status(w.p(p), *tr[:1], "claude:registry")
    msg = w.human("a message while the prompt is open")
    assert pushes(w) == []
    assert w.hook(p, "PostToolUse", ok=True) is None  # no context either
    assert w.p(p).status == "waiting-approval" and w.states(m)[msg.id] == "pending"
    # declined with Esc: the registry goes idle, the turn is over
    clock.advance(0.5)
    reg(w, p, "idle")
    tr = registry_transition(w.p(p).status, w.p(p).hooks_seen_at, ad(w).registry[("", p.agent_pid)], clock.now())
    assert tr == ("idle", True)
    w.actions += w.engine.set_status(w.p(p), tr[0], "claude:registry", bump=tr[1])
    [push] = pushes(w)
    assert push.path == "inbox" and f"id={msg.id} " in push.text


def test_idle_expiry_and_the_warn_after_three(w: World, clock: FakeClock) -> None:
    p, m, _c = claude(w)
    msg = w.human("hello?")
    idle_s = w.cfg.claude.inbox_idle_expire_s
    for i in range(3):
        if i:
            # unconfirmed frames back off: idle_s * 2**(n-1) after the n-th expiry
            clock.advance(idle_s * 2 ** (i - 1) - 0.1)
            reg(w, p, "idle")
            w.actions += w.engine.tick()
            assert pushes(w) == []
            clock.advance(0.2)
            reg(w, p, "idle")
            w.actions += w.engine.tick()
        [push] = pushes(w)
        w.store.mark_posted(push.batch_id)
        clock.advance(idle_s - 0.1)
        reg(w, p, "idle")
        w.actions += w.engine.tick()
        assert w.store.get_batch(push.batch_id).state == "offered"  # not yet
        clock.advance(0.2)
        reg(w, p, "idle")
        w.actions += w.engine.tick()
        b = w.store.get_batch(push.batch_id)
        assert b.state == "expired" and b.expire_reason == "idle_no_token"
        d = w.delivery(m, msg)
        assert d["attempts"] == i + 1
        if i < 2:
            assert not [a for a in w.actions if isinstance(a, Notice)]
    notes = [a for a in w.actions if isinstance(a, Notice) and "not confirmed" in a.text]
    assert len(notes) == 1 and w.p(p).push_expiries == 3
    # from the warn count on, the member shows parked while it backs off
    assert "not confirmed" in (w.engine.parked_reason(m.id) or "")
    clock.advance(idle_s * 4 + 0.1)
    reg(w, p, "idle")
    w.actions += w.engine.tick()
    # a confirmation resets the count (and so the backoff)
    [push] = pushes(w)
    assert w.engine.parked_reason(m.id) is None
    w.hook(p, "UserPromptSubmit", tokens=(tok(push.text),))
    assert w.p(p).push_expiries == 0
    assert ad(w)._unconfirmed_wait(w.p(p), clock.now()) == 0


def test_unconfirmed_frames_back_off_and_cannot_drain_the_budget(w: World, clock: FakeClock) -> None:
    """Frames that land but are never confirmed (a failing UserPromptSubmit hook):
    ten minutes of that is a handful of pushes, not one every few seconds."""
    p, m, _c = claude(w)
    a = ad(w)
    w.human("are you there?")
    budget0 = w.store.room_by_id(w.room.id).budget_remaining
    n = 0
    for _ in range(600):
        for x in pushes(w):
            n += 1
            w.store.mark_posted(x.batch_id)
        clock.advance(1.0)
        a.observe(p.agent_pid, {"pid": p.agent_pid, "status": "idle"}, clock.now())
        w.actions += w.engine.tick()
    assert 5 <= n <= 10, n
    assert w.store.room_by_id(w.room.id).budget_remaining >= budget0 - 10


def test_a_busy_registry_keeps_a_posted_frame_alive(w: World, clock: FakeClock) -> None:
    p, m, _c = claude(w)
    w.human("x")
    [push] = pushes(w)
    w.store.mark_posted(push.batch_id)
    reg(w, p, "busy")  # e.g. the human typed at the same moment: the frame waits its turn
    clock.advance(60)
    w.actions += w.engine.tick()
    assert w.store.get_batch(push.batch_id).state == "offered"


def test_offline_expires_the_frame_at_once(w: World) -> None:
    p, m, _c = claude(w)
    msg = w.human("x")
    [push] = pushes(w)
    w.hook(p, "SessionEnd", reason="prompt_input_exit")
    b = w.store.get_batch(push.batch_id)
    assert b.state == "expired" and b.expire_reason == "offline"
    assert w.states(m)[msg.id] == "pending"


def test_clear_keeps_the_session_and_the_inbox(w: World, clock: FakeClock) -> None:
    p, m, _c = claude(w)
    w.hook(p, "SessionEnd", reason="clear")
    out = w.hook(p, "SessionStart", source="clear", sid="new-session-id")
    assert out is not None and "#build as claude-1" in out.text
    assert w.p(p).status == "idle" and w.p(p).session_id == "new-session-id"
    reg(w, p, "idle")
    w.human("still there?")
    [push] = pushes(w)
    assert push.path == "inbox"


def test_send_failure_backs_off(w: World, clock: FakeClock) -> None:
    p, m, _c = claude(w)
    a = ad(w)
    a.clock = clock
    a._failed(w.p(p))
    wake = Release(items=(), kind="wake", counted=True, reason="human")
    assert a.route(w.p(p), wake, None, clock.now()).kind == "defer"
    clock.advance(1.1)
    reg(w, p, "idle")
    assert a.route(w.p(p), wake, None, clock.now()).kind == "push"


def test_wait_sink_still_wins(w: World) -> None:
    p, m, _c = claude(w, status="busy")
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 110)
    w.actions += acts
    w.human("x")
    assert w.resolved(sink.id)[0]["status"] == "messages"
    assert pushes(w) == []


def test_key_is_per_membership(w: World) -> None:
    p, m, _c = claude(w)
    w.human("x")
    [push] = pushes(w)
    bid, mac = tok(push.text)
    assert envelope.check_token(KEY, mac, bid, m.id)


# ------------------------------------------------ races around a frame in flight
def test_a_message_arriving_before_the_confirming_ups_rides_that_hook(w: World, clock: FakeClock) -> None:
    """B arrives while A's frame is on its way: B must not become a second frame
    posted into the turn A starts; it goes as the UserPromptSubmit's context."""
    p, m, _c = claude(w)
    a = w.human("message A")
    [pa] = pushes(w)
    w.store.mark_posted(pa.batch_id)
    clock.advance(0.02)
    b = w.human("message B")
    assert pushes(w) == []  # one offer per member at a time
    clock.advance(0.03)  # the registry read (≤ 250 ms old) still says idle
    out = w.hook(p, "UserPromptSubmit", tokens=(tok(pa.text),), permission_mode="default")
    assert pushes(w) == []
    assert out is not None and w.store.get_batch(out.batch_id).path == "hook_ups"
    assert f"id={b.id} " in out.text and f"id={a.id} " not in out.text
    assert w.p(p).status == "busy" and w.states(m)[a.id] == "in_context"


def test_more_than_one_batch_pending_at_wake_the_rest_is_hook_context(w: World, clock: FakeClock) -> None:
    p, m, _c = claude(w)
    w.store.set_paused(w.room.id, True, "x")
    msgs = [w.human(f"msg {i}") for i in range(25)]
    w.take()
    w.store.set_paused(w.room.id, False, None)
    w.actions += w.engine.on_command(w.room.id, "resume")
    [push] = pushes(w)
    w.store.mark_posted(push.batch_id)
    clock.advance(0.05)
    out = w.hook(p, "UserPromptSubmit", tokens=(tok(push.text),), permission_mode="default")
    assert pushes(w) == []  # no second frame into the running turn
    assert out is not None and w.store.get_batch(out.batch_id).path == "hook_ups"
    assert f"id={msgs[-1].id} " in out.text


def test_one_frame_per_session_across_rooms(w: World, clock: FakeClock) -> None:
    """A session in two rooms: room 2's frame waits until room 1's settles, so it
    can't queue behind the turn room 1's frame starts."""
    room2 = w.store.create_room("#other", "alice", 60, 6)
    p, m, _c = claude(w)
    m2 = w.store.create_membership(room2.id, p.id, "claude-1", "h2")
    w.human("room 1 message")
    [p1] = pushes(w)
    assert p1.room == "#build"
    msg2 = w.store.insert_message(room2.id, sender_name="alice", sender_kind="human", via="web", text="room 2")
    w.actions += w.engine.on_message(msg2.id)
    assert pushes(w) == [] and w.engine.parked_reason(m2.id) is None
    w.store.mark_posted(p1.batch_id)
    # room 1's frame starts the turn: room 2's message rides that hook as context
    out = w.hook(p, "UserPromptSubmit", tokens=(tok(p1.text),), permission_mode="default")
    assert pushes(w) == []
    assert out is not None and f"id={msg2.id} " in out.text
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    # the turn ends unanswered: both rooms' items come back once, room 1's first
    w.hook(p, "Stop")
    [p3] = pushes(w)
    assert p3.room == "#build" and "again=yes" in p3.text
    w.store.mark_posted(p3.batch_id)
    w.actions += w.engine.on_expire(p3.batch_id, "send_error")
    [p4] = pushes(w)  # still one at a time (room 1's item is pending again, and first)
    assert p4.room == "#build"
    # when room 1's frame settles and room 1 has nothing to send, room 2's goes out
    # at once (the whole session is re-evaluated, not only room 1)
    w.store.set_held(m.id, True)
    w.actions += w.engine.on_expire(p4.batch_id, "send_error")
    [p5] = pushes(w)
    assert p5.room == "#other" and f"id={msg2.id} " in p5.text


def test_a_frame_to_a_starting_member_expires(w: World, clock: FakeClock) -> None:
    """After a broker restart or MCP reconnect the member is 'starting' (idle for
    routing); an idle Claude fires no hook, so the expiry must count it idle too."""
    p, m, _c = claude(w, status="starting")
    msg = w.human("hello after restart")
    [push] = pushes(w)
    assert w.store.get_batch(push.batch_id).wake_kind == "idle_wake"
    w.store.mark_posted(push.batch_id)
    clock.advance(w.cfg.claude.inbox_idle_expire_s + 0.1)
    reg(w, p, "idle")
    w.actions += w.engine.tick()
    b = w.store.get_batch(push.batch_id)
    assert b.state == "expired" and b.expire_reason == "idle_no_token"
    assert w.states(m)[msg.id] == "pending" and w.p(p).push_expiries == 1


def test_pause_cancels_an_unposted_frame_but_not_one_handed_over(w: World, clock: FakeClock) -> None:
    p, m, _c = claude(w)
    w.human("first")
    [push] = pushes(w)
    w.actions += w.engine.pause_room(w.room.id, "pause")
    assert w.store.get_batch(push.batch_id).state == "cancelled"  # never reached the transport
    w.store.set_paused(w.room.id, False, None)
    w.actions += w.engine.on_command(w.room.id, "resume")
    [push2] = pushes(w)
    w.store.mark_posted(push2.batch_id)  # the runner handed it to the MCP server
    w.actions += w.engine.pause_room(w.room.id, "pause")
    assert w.store.get_batch(push2.batch_id).state == "offered"  # can't be recalled
    w.hook(p, "UserPromptSubmit", tokens=(tok(push2.text),))
    assert w.store.get_batch(push2.batch_id).state == "confirmed"


# ------------------------------------------------------------ the poller
class _RunnerStub:
    def __init__(self, w: World) -> None:
        self.state = type("S", (), {"engine": w.engine, "store": w.store, "clock": w.clock})()
        self.acts: list[Any] = []

    def execute(self, acts: list[Any]) -> None:
        self.acts += acts


def _poller(w: World, tmp_path: Path) -> tuple[ClaudeAdapter, _RunnerStub, Path]:
    import dataclasses

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    a = ad(w)
    a.cfg = a.cfg.replace(claude=dataclasses.replace(a.cfg.claude, sessions_dir=str(sessions)))
    r = _RunnerStub(w)
    a.runner, a.clock = r, w.clock
    return a, r, sessions


@pytest.mark.parametrize("bad", ["pid", "socket"])
def test_poll_once_ignores_a_registry_file_that_is_not_this_session(w: World, clock: FakeClock,
                                                                    tmp_path: Path, bad: str) -> None:
    p, m, _c = claude(w, status="busy", registry=None)
    a, r, sessions = _poller(w, tmp_path)
    data = {"pid": p.agent_pid, "status": "waiting", "messagingSocketPath": SOCK}
    if bad == "pid":
        data["pid"] = p.agent_pid + 1
    else:
        data["messagingSocketPath"] = "/tmp/someone-else.sock"
    (sessions / f"{p.agent_pid}.json").write_text(json.dumps(data))
    a.poll_once()
    assert ("", p.agent_pid) not in a.registry and w.p(p).status == "busy"
    # the matching file is read, and sets the approval hold
    data.update(pid=p.agent_pid, messagingSocketPath=SOCK)
    (sessions / f"{p.agent_pid}.json").write_text(json.dumps(data))
    a.poll_once()
    assert a.registry[("", p.agent_pid)].status == "waiting" and w.p(p).status == "waiting-approval"
