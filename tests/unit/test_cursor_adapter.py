"""The Cursor adapter with a FakeClock (DESIGN.md §9.4, §8.7): binding-gated tiers,
the stop park (status gating, one live park, supersede, release on any hook,
/pause and /kick), the follow-up's two-phase confirmation, and "degraded" after
unconfirmed follow-ups."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock
from engine_world import World

from switchboard.adapters.cursor import FOLLOWUP_CONFIRM_S, FOLLOWUP_RACE_S, CursorAdapter
from switchboard.config import Config
from switchboard.models import HookEvent, Notice, Release, ResolveSink


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def ad(w: World) -> CursorAdapter:
    a = w.engine.adapters["cursor"]
    assert isinstance(a, CursorAdapter)
    return a


def cursor(
    w: World, name: str = "cursor-1", *, status: str = "idle", bound: bool = True, room_id: int | None = None
) -> tuple[Any, Any]:
    p, m = w.agent(name, harness="cursor", status=status, hooks=True, room_id=room_id)
    if bound:
        p = w.store.update_participant(p.id, session_key=f"cursor:conv-{name}", bind_state="bound")
    else:
        p = w.store.update_participant(
            p.id, session_key=f"cursor:agent:{p.agent_pid}@1.00", bind_state="pending"
        )
    return p, m


def stop(w: World, p: Any, status: str = "completed", loop: int = 0, max_wait: float | None = 630.0) -> Any:
    return w.hook(p, "stop", status=status, loop_count=loop, max_wait_s=max_wait)


def park_result(w: World, sink_id: int) -> dict[str, Any] | None:
    s = w.engine.sinks.get(sink_id)
    return None if s is None or s.open else (s.result or {})


def batch_of(w: World, m: Any, msg: Any) -> Any:
    bid = w.delivery(m, msg)["batch_id"]
    return w.store.get_batch(bid) if bid else None


# --------------------------------------------------- binding (moved from agents.py, #170)
def test_join_fields_marks_a_fresh_or_unbound_row_pending(w: World) -> None:
    """A row with no existing session, or one not yet bound to a conversation, starts
    pending until the join's postToolUse hook binds it (DESIGN.md §6.3)."""
    a = ad(w)
    assert a.join_fields(None) == {"bind_state": "pending"}
    pending, _m = cursor(w, bound=False)
    assert a.join_fields(w.p(pending)) == {"bind_state": "pending"}
    bound_p, _m2 = cursor(w, "cursor-2")
    assert a.join_fields(w.p(bound_p)) == {}


def test_conversation_kicked_needs_p_bound_to_the_kicked_conversation(w: World) -> None:
    """A kick sticks to a resumed Cursor conversation (§9.4): a new row bound to the
    same conversation counts; a pending row, or a bound row of another conversation,
    doesn't (the adapter must not report a kick it isn't actually carrying)."""
    a = ad(w)
    key = "cursor:conv-x"
    old = w.store.upsert_participant("cursor", key, status="idle")
    m = w.store.create_membership(w.room.id, old.id, "cursor-x", "h-x")
    w.store.end_membership(m.id, "kick", kicked=True)
    w.store.update_participant(old.id, session_key=f"{key}#ended-{old.id}")
    resumed = w.store.upsert_participant("cursor", key, bind_state="bound", status="idle")
    assert a.conversation_kicked(w.store, w.room.id, w.p(resumed)) is True
    pending = w.store.upsert_participant("cursor", f"{key}-p", bind_state="pending", status="idle")
    assert a.conversation_kicked(w.store, w.room.id, w.p(pending)) is False
    other = w.store.upsert_participant("cursor", "cursor:conv-other", bind_state="bound", status="idle")
    assert a.conversation_kicked(w.store, w.room.id, w.p(other)) is False


def test_nonce_ok_matches_only_its_own_pending_nonce(w: World) -> None:
    a = ad(w)
    p = w.p(w.store.upsert_participant("cursor", "cursor:agent:1@1.00", bind_nonce="abc123", status="idle"))
    ev = lambda nonce: HookEvent(harness="cursor", event="PostToolUse", join_nonce=nonce)  # noqa: E731
    assert a.nonce_ok(p, ev("abc123")) is True
    assert a.nonce_ok(p, ev("wrong")) is False
    assert a.nonce_ok(p, ev(None)) is False
    no_nonce = w.p(w.store.upsert_participant("cursor", "cursor:agent:2@1.00", status="idle"))
    assert a.nonce_ok(no_nonce, ev("abc123")) is False


def test_bound_to_checks_the_exact_conversation(w: World) -> None:
    a = ad(w)
    p, _m = cursor(w, "cursor-1")  # bound to "cursor:conv-cursor-1"
    assert a.bound_to(w.p(p), "conv-cursor-1") is True
    assert a.bound_to(w.p(p), "conv-other") is False
    assert a.bound_to(w.p(p), None) is False
    pending, _m2 = cursor(w, "cursor-2", bound=False)
    assert a.bound_to(w.p(pending), "conv-cursor-2") is False


def test_conversation_key_rejects_a_malformed_conversation_id(w: World) -> None:
    a = ad(w)
    p, _m = cursor(w, "cursor-1")
    assert a.conversation_key(w.p(p), "conv-9") == "cursor:conv-9"
    assert a.conversation_key(w.p(p), "") is None  # empty: CURSOR_SID_RE needs at least one char
    assert a.conversation_key(w.p(p), None) is None


# ------------------------------------------------------------------ tiers
def test_tier_is_provisional_once_bound_and_degraded_after_misses(w: World) -> None:
    a = ad(w)
    p, _m = cursor(w, bound=False)
    assert a.tier(w.p(p)) == ("mcp-only", "binding")
    assert a.context_events(w.p(p)) == frozenset()
    p2, _m2 = cursor(w, "cursor-2")
    assert a.tier(w.p(p2)) == ("cursor:stop-park", "provisional")
    assert a.context_events(w.p(p2)) == frozenset({"PostToolUse", "PostToolUseFailure"})
    p2 = w.store.update_participant(p2.id, unconfirmed_followups=2)
    assert a.tier(p2) == ("cursor:stop-park", "provisional, degraded")


def test_route_matrix(w: World, clock: FakeClock) -> None:
    a = ad(w)
    wake = Release(items=(), kind="wake", counted=True, reason="human")
    prio = Release(items=(), kind="priority", counted=False, reason="human")
    pend, _ = cursor(w, bound=False)
    assert a.route(w.p(pend), prio, None, clock.now()).kind == "none"  # no hook can carry it yet
    p, _m = cursor(w, "cursor-2")
    assert a.route(w.p(p), prio, None, clock.now()).kind == "pull"
    r = a.route(w.p(p), wake, None, clock.now())
    assert r.kind == "none" and "not parked" in (r.reason or "")
    pd = w.store.update_participant(p.id, unconfirmed_followups=2)
    assert "degraded" in (a.route(pd, wake, None, clock.now()).reason or "")


# ------------------------------------------------------------- stop gating
@pytest.mark.parametrize("status", ["aborted", "error", None])
def test_a_stop_that_did_not_complete_never_parks(w: World, status: str | None) -> None:
    p, m = cursor(w, status="busy")
    w.human("hello")
    out = stop(w, p, status=status)  # type: ignore[arg-type]
    assert out is None and w.engine.sinks.parks() == []
    assert w.p(p).status == "idle"
    assert [b.path for b in w.store.offered_batches(m.id)] == []


def test_no_park_without_the_hooks_own_wait_budget_or_binding(w: World) -> None:
    p, _m = cursor(w, status="busy")
    assert stop(w, p, max_wait=None) is None  # installed without --max-wait: answer at once
    assert stop(w, p, max_wait=20.0) is None  # less than the 30 s margin
    pend, _ = cursor(w, "cursor-2", status="busy", bound=False)
    assert stop(w, pend) is None
    assert w.engine.sinks.parks() == []


def test_park_length_is_capped_by_config_and_the_hooks_wait(w: World, clock: FakeClock) -> None:
    p, _m = cursor(w, status="busy")
    out = stop(w, p)
    assert out.kind == "park"
    s = w.engine.sinks.get(out.sink_id)
    assert s.kind == "park" and s.path == "stop_followup" and s.deadline - clock.now() == 600.0
    out2 = stop(w, p, max_wait=45.0)
    assert w.engine.sinks.get(out2.sink_id).deadline - clock.now() == 15.0


# ---------------------------------------------------------- park and fill
def test_a_pending_message_fills_the_park_at_once_as_a_counted_followup(w: World) -> None:
    p, m = cursor(w, status="busy")
    budget = w.store.room_by_id(w.room.id).budget_remaining
    msg = w.human("please look at the parser")
    out = stop(w, p)
    res = park_result(w, out.sink_id)
    assert res is not None and res["status"] == "messages"
    assert f"id={msg.id}" in res["text"] and "please look at the parser" in res["text"]
    b = batch_of(w, m, msg)
    assert (b.path, b.kind, b.wake_kind, b.budget_counted) == ("stop_followup", "wake", "stop_cont", True)
    assert res["batch_id"] == b.id and isinstance(res["ack"], str) and len(res["ack"]) == 32
    assert w.store.room_by_id(w.room.id).budget_remaining == budget - 1
    assert w.p(p).status == "busy"  # the follow-up starts a turn


def test_a_message_that_arrives_later_fills_the_open_park(w: World) -> None:
    p, m = cursor(w, status="busy")
    out = stop(w, p)
    assert park_result(w, out.sink_id) is None  # still parked
    assert w.p(p).status == "idle"
    msg = w.human("ping")
    res = [a.result for a in w.take() if isinstance(a, ResolveSink) and a.sink_id == out.sink_id]
    assert res and res[-1]["status"] == "messages" and f"id={msg.id}" in res[-1]["text"]


def test_peer_text_in_a_followup_is_a_read_stub(w: World) -> None:
    p, m = cursor(w, status="busy")
    _q, qm = w.agent("codex-1", harness="test")
    peer = w.agent_says(qm, "SECRET-PEER-TEXT @cursor-1", mentions=("cursor-1",))
    out = stop(w, p)
    res = park_result(w, out.sink_id)
    assert res["status"] == "messages" and f"id={peer.id}" in res["text"]
    assert "SECRET-PEER-TEXT" not in res["text"] and "read(" in res["text"]


def test_followup_is_confirmed_by_ack_then_the_next_posttooluse(w: World, clock: FakeClock) -> None:
    p, m = cursor(w, status="busy")
    msg = w.human("do the thing")
    res = park_result(w, stop(w, p).sink_id)
    bid = res["batch_id"]
    # the next hook before the ack confirms nothing
    assert w.engine.on_hook_ack(bid, "0" * 32) == []  # wrong nonce
    assert w.store.get_batch(bid).state == "offered"
    w.engine.on_hook_ack(bid, res["ack"])
    assert w.store.get_batch(bid).state == "offered"  # printed, not yet seen in a turn
    clock.advance(1.5)
    w.hook(p, "postToolUse", ok=True, t=clock.now())
    b = w.store.get_batch(bid)
    assert b.state == "confirmed" and b.evidence == "hook:PostToolUse" and b.turn_start_at == clock.now()
    assert w.delivery(m, msg)["state"] == "in_context"


def test_followup_is_confirmed_by_the_next_stop_with_loop_count_plus_one(w: World) -> None:
    p, m = cursor(w, status="busy")
    w.human("just answer")
    res = park_result(w, stop(w, p, loop=3).sink_id)
    w.engine.on_hook_ack(res["batch_id"], res["ack"])
    out = stop(w, p, loop=4)
    assert w.store.get_batch(res["batch_id"]).state == "confirmed"
    assert out is not None and out.kind == "park"  # and the next park is open


def test_unconfirmed_followups_degrade_the_member_until_the_next_prompt(w: World) -> None:
    p, m = cursor(w, status="busy")
    w.human("try")
    out = stop(w, p, loop=0)
    for n in (1, 2):
        res = park_result(w, out.sink_id)
        assert res["status"] == "messages"
        w.engine.on_hook_ack(res["batch_id"], res["ack"])
        w.take()
        out = stop(w, p, loop=0)  # loop_count reset: the follow-up never ran (the stop parks anew)
        b = w.store.get_batch(res["batch_id"])
        assert b.state == "expired" and b.expire_reason == "loop_reset"
        assert w.p(p).unconfirmed_followups == n
    assert out is None  # degraded after the second miss: that stop didn't park
    acts = w.take()
    assert any(isinstance(a, Notice) and "not confirmed" in a.text for a in acts)
    assert w.p(p).tier_note == "provisional, degraded"
    assert stop(w, p) is None  # degraded: no more parks
    assert w.engine.parked_reason(m.id) is not None and "degraded" in w.engine.parked_reason(m.id)
    w.hook(p, "beforeSubmitPrompt", gen="g2")  # the human's next prompt
    assert w.p(p).unconfirmed_followups == 0 and w.p(p).tier_note == "provisional"


def test_a_followup_the_hook_never_acked_expires(w: World, clock: FakeClock) -> None:
    p, m = cursor(w, status="busy")
    msg = w.human("x")
    res = park_result(w, stop(w, p).sink_id)
    clock.advance(w.cfg.delivery.hook_ack_s + 0.1)
    w.actions += w.engine.tick()
    b = w.store.get_batch(res["batch_id"])
    assert b.state == "expired" and b.expire_reason == "no_ack"
    assert w.delivery(m, msg)["state"] in ("pending", "offered")  # back in the queue
    assert w.p(p).unconfirmed_followups == 1


def test_an_acked_followup_with_no_further_hook_expires(w: World, clock: FakeClock) -> None:
    p, _m = cursor(w, status="busy")
    w.human("x")
    res = park_result(w, stop(w, p).sink_id)
    w.engine.on_hook_ack(res["batch_id"], res["ack"])
    clock.advance(FOLLOWUP_CONFIRM_S - 1)
    w.engine.tick()
    assert w.store.get_batch(res["batch_id"]).state == "offered"
    clock.advance(2)
    w.engine.tick()
    assert w.store.get_batch(res["batch_id"]).expire_reason == "no_hook"


@pytest.mark.parametrize("acked", [True, False])
def test_a_followup_that_expires_with_no_hook_leaves_the_member_parked_not_busy(
    w: World, clock: FakeClock, acked: bool
) -> None:
    """Cursor dropped the follow-up (or the hook died before printing it): no turn
    started, so the member is idle again and shown parked, not busy for good."""
    p, m = cursor(w, status="busy")
    msg = w.human("x")
    res = park_result(w, stop(w, p).sink_id)
    assert w.p(p).status == "busy"  # the follow-up should start a turn
    if acked:
        w.engine.on_hook_ack(res["batch_id"], res["ack"])
        clock.advance(FOLLOWUP_CONFIRM_S + 1)
    else:
        clock.advance(w.cfg.delivery.hook_ack_s + 0.1)
    w.actions += w.engine.tick()
    assert w.store.get_batch(res["batch_id"]).expire_reason == ("no_hook" if acked else "no_ack")
    assert w.p(p).status == "idle"
    assert w.delivery(m, msg)["state"] == "pending"
    assert "not parked" in (w.engine.parked_reason(m.id) or "")
    # the next completed stop parks again and gets it
    assert park_result(w, stop(w, p).sink_id)["status"] == "messages"


def test_a_followup_cancelled_by_a_kick_still_undoes_its_busy(w: World, clock: FakeClock) -> None:
    d = w.cfg.delivery
    other = w.store.create_room("#other", "alice", d.budget_per_hour, d.hop_limit)
    p, m = cursor(w, status="busy")
    w.store.create_membership(other.id, p.id, "cursor-1", "h2")
    w.human("x")
    res = park_result(w, stop(w, p).sink_id)
    w.engine.on_hook_ack(res["batch_id"], res["ack"])
    w.store.end_membership(m.id, "kick", kicked=True)  # kicked from #build; still in #other
    w.engine.on_membership_ended(m.id, "kick")
    assert w.store.get_batch(res["batch_id"]).state == "cancelled"
    clock.advance(FOLLOWUP_CONFIRM_S + 1)
    w.actions += w.engine.tick()
    assert w.p(p).status == "idle" and res["batch_id"] not in w.engine.continues


def test_a_hook_after_the_followup_keeps_its_status(w: World, clock: FakeClock) -> None:
    """A hook of the follow-up turn arrived before the ack (e.g. a slow ack): the
    no_ack expiry doesn't undo the busy the turn itself reported."""
    p, _m = cursor(w, status="busy")
    w.human("x")
    res = park_result(w, stop(w, p).sink_id)
    clock.advance(1)
    w.hook(p, "postToolUse", ok=True)
    clock.advance(w.cfg.delivery.hook_ack_s)
    w.actions += w.engine.tick()
    assert w.store.get_batch(res["batch_id"]).expire_reason == "no_ack"
    assert w.p(p).status == "busy"


def test_dropped_followups_degrade_the_member_across_human_prompts(w: World, clock: FakeClock) -> None:
    """The real failure: Cursor ignores the follow-up, nothing happens until the
    human types. That prompt must not wipe the count, or it never reaches the limit."""
    p, m = cursor(w, status="busy")
    budget0 = w.store.room_by_id(w.room.id).budget_remaining
    for n in (1, 2):
        w.human(f"msg {n}")
        res = park_result(w, stop(w, p).sink_id)
        assert res["status"] == "messages", n
        w.engine.on_hook_ack(res["batch_id"], res["ack"])
        clock.advance(FOLLOWUP_CONFIRM_S + 1)
        w.actions += w.engine.tick()
        assert w.store.get_batch(res["batch_id"]).expire_reason == "no_hook"
        assert w.p(p).unconfirmed_followups == n
        if n == 1:
            w.hook(p, "beforeSubmitPrompt", gen=f"g{n}")  # the human types
            assert w.p(p).unconfirmed_followups == 1 and w.p(p).tier_note == "provisional"
            out = w.hook(p, "postToolUse", ok=True)
            if out is not None and out.batch_id:
                w.engine.on_hook_ack(out.batch_id, out.ack)
    assert w.p(p).tier_note == "provisional, degraded"
    assert any(isinstance(a, Notice) and "not confirmed" in a.text for a in w.take())
    assert stop(w, p) is None  # degraded: no park
    assert "degraded" in (w.engine.parked_reason(m.id) or "")
    assert budget0 - w.store.room_by_id(w.room.id).budget_remaining == 2
    w.hook(p, "beforeSubmitPrompt", gen="g9")  # the human's next prompt ends the spell
    assert w.p(p).unconfirmed_followups == 0 and w.p(p).tier_note == "provisional"


def test_a_confirmed_followup_breaks_a_run_of_misses(w: World, clock: FakeClock) -> None:
    p, _m = cursor(w, status="busy")
    w.human("one")
    res = park_result(w, stop(w, p).sink_id)
    w.engine.on_hook_ack(res["batch_id"], res["ack"])
    clock.advance(FOLLOWUP_CONFIRM_S + 1)
    w.engine.tick()
    assert w.p(p).unconfirmed_followups == 1
    w.human("two")
    res = park_result(w, stop(w, p).sink_id)
    w.engine.on_hook_ack(res["batch_id"], res["ack"])
    w.hook(p, "postToolUse", ok=True)
    assert w.store.get_batch(res["batch_id"]).state == "confirmed"
    assert w.p(p).unconfirmed_followups == 0


def test_the_human_typing_well_after_a_followup_counts_it(w: World, clock: FakeClock) -> None:
    p, _m = cursor(w, status="busy")
    w.store.update_participant(p.id, unconfirmed_followups=1)
    w.human("x")
    res = park_result(w, stop(w, p).sink_id)
    w.engine.on_hook_ack(res["batch_id"], res["ack"])
    clock.advance(FOLLOWUP_RACE_S + 1)  # printed, nothing ran, then the human typed
    w.hook(p, "beforeSubmitPrompt", gen="g9")
    assert w.store.get_batch(res["batch_id"]).expire_reason == "human_prompt"
    # the second miss: degraded from this prompt until the next one
    assert w.p(p).unconfirmed_followups == 2 and w.p(p).tier_note == "provisional, degraded"
    assert stop(w, p) is None
    w.hook(p, "beforeSubmitPrompt", gen="g10")
    assert w.p(p).tier_note == "provisional"


def test_the_human_typing_right_after_a_followup_is_a_race_not_a_miss(w: World) -> None:
    p, _m = cursor(w, status="busy")
    w.human("x")
    res = park_result(w, stop(w, p).sink_id)
    w.engine.on_hook_ack(res["batch_id"], res["ack"])
    w.hook(p, "beforeSubmitPrompt", gen="g9")
    b = w.store.get_batch(res["batch_id"])
    assert b.state == "expired" and b.expire_reason == "human_prompt"
    assert w.p(p).unconfirmed_followups == 0


# ------------------------------------------------------ one live park
def test_a_newer_stop_supersedes_the_old_park(w: World) -> None:
    p, _m = cursor(w, status="busy")
    first = stop(w, p)
    second = stop(w, p, loop=1)
    assert park_result(w, first.sink_id) == {}  # released with no continuation
    assert w.engine.sinks.get(first.sink_id).close_reason == "superseded"
    assert park_result(w, second.sink_id) is None
    assert len(w.engine.sinks.parks()) == 1


def test_any_other_hook_of_the_conversation_releases_the_park(w: World) -> None:
    p, _m = cursor(w, status="busy")
    out = stop(w, p)
    w.hook(p, "beforeSubmitPrompt", gen="g2")  # the human typed while the hook was parked
    assert park_result(w, out.sink_id) == {}
    assert w.engine.sinks.parks() == []


def test_pause_releases_the_park_and_resume_does_not_reopen_it(w: World) -> None:
    p, m = cursor(w, status="busy")
    out = stop(w, p)
    w.engine.pause_room(w.room.id, "test")
    assert park_result(w, out.sink_id) == {}
    w.store.set_paused(w.room.id, False)
    w.human("after resume")
    w.engine.on_command(w.room.id, "resume")
    assert w.engine.parked_reason(m.id) is not None  # stopped, not listening: needs a poke


def test_pause_of_one_room_keeps_a_park_that_serves_another(w: World) -> None:
    d = w.cfg.delivery
    other = w.store.create_room("#other", "alice", d.budget_per_hour, d.hop_limit)
    p, _m = cursor(w, status="busy")
    w.store.create_membership(other.id, p.id, "cursor-1", "h2")
    out = stop(w, p)
    w.engine.pause_room(w.room.id, "test")
    assert park_result(w, out.sink_id) is None  # #other can still wake it
    msg = w.store.insert_message(
        other.id, sender_name="alice", sender_kind="human", via="web", text="in other"
    )
    w.actions += w.engine.on_message(msg.id)
    assert park_result(w, out.sink_id)["status"] == "messages"


def test_leaving_the_last_room_releases_the_park(w: World) -> None:
    p, m = cursor(w, status="busy")
    out = stop(w, p)
    w.store.end_membership(m.id, "kick", kicked=True)
    w.engine.on_membership_ended(m.id, "kick")
    assert park_result(w, out.sink_id) == {}


def test_redeliver_once_only_on_a_completed_stop(w: World) -> None:
    p, m = cursor(w, status="busy")
    msg = w.human("answer me")
    out = w.hook(p, "postToolUse", ok=True)
    w.engine.on_hook_ack(out.batch_id, out.ack)
    assert w.delivery(m, msg)["state"] == "in_context"
    stop(w, p, status="aborted")
    assert w.delivery(m, msg)["state"] == "in_context"  # an abort isn't "ended unanswered"
    res = park_result(w, stop(w, p, status="completed").sink_id)
    assert w.delivery(m, msg)["redelivered"] == 1 and res["status"] == "messages"
    assert "again=yes" in res["text"]


def test_caps_keep_wait_under_cursors_60s_mcp_timeout(w: World) -> None:
    p, _m = cursor(w)
    caps = ad(w).caps(w.p(p))
    assert caps.wait_cap_s == 50 and caps.ctx_max_chars == 8000  # Cursor drops context over 10,000
    assert "stop_followup" not in caps.inline_paths  # a follow-up is a user message: peer text is stubbed
