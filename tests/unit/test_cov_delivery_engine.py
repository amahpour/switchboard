"""Engine edge cases with a FakeClock (DESIGN.md §8.2, §8.5, §8.7, §9.4, §9.5):
stale or unknown ids handed back to the engine (a late unwait or wait timer, an
ack after a leave, a call after a kick, a second expiry), pull answers and their
confirmation rules, a dropped connection, /hold, one live Cursor park per
conversation, the Devin taint memory, and the guards that keep an ended session,
a paused room or a held member out of delivery and the watchdog."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeClock
from engine_world import World
from test_claude_adapter import claude
from test_cursor_adapter import cursor, park_result, stop
from test_devin_adapter import devin
from switchboard.adapters.cursor import FOLLOWUP_CONFIRM_S
from switchboard.config import Config
from switchboard.delivery.engine import TAINTED_GENS_MAX
from switchboard.models import HookEvent, Notice, Push, ResolveSink, Snapshot, closed_room_name


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def cursor_stop() -> HookEvent:
    return HookEvent(harness="cursor", event="stop", status="completed", loop_count=0)


def leave(w: World, membership_id: int, reason: str = "leave") -> None:
    """What the broker does on leave/kick: the store first, then the engine."""
    w.store.end_membership(membership_id, reason, kicked=reason == "kick")
    w.actions += w.engine.on_membership_ended(membership_id, reason)


# ------------------------------------------------------------ stale sinks
def test_a_late_unwait_after_the_member_left_only_refreshes_the_room(w: World) -> None:
    p, m = w.agent("bot")
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    w.actions += acts
    leave(w, m.id)
    assert w.resolved(sink.id) == [{"status": "left", "text": "[switchboard] you are no longer in this room."}]
    # the harness cancels that wait() afterwards: nothing to close, take back or offer
    assert w.engine.unwait(w.p(p), "w1") == [Snapshot(w.room.id)]
    assert w.engine.parked_reason(m.id) is None


def test_unwait_of_an_unknown_wait_id_changes_nothing(w: World) -> None:
    p, m = w.agent("bot")
    sink, _acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    assert w.engine.unwait(w.p(p), "never-opened") == []
    assert w.engine.sinks.get(sink.id).open


def test_a_wait_timer_that_fires_after_the_wait_was_answered_does_nothing(w: World) -> None:
    p, m = w.agent("bot")
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    w.actions += acts
    msg = w.human("hi")
    [res] = w.resolved(sink.id)
    assert res["status"] == "messages"
    # the broker's call_later(timeout) still fires: the sink is closed, nothing changes
    assert w.engine.sink_timeout(sink.id) == []
    assert w.engine.sink_timeout(sink.id + 1000) == []  # never opened
    assert w.engine.sinks.get(sink.id).result["status"] == "messages"
    assert w.states(m)[msg.id] == "offered"


def test_a_dropped_connection_cancels_only_its_own_waits(w: World) -> None:
    p, m = w.agent("bot")
    q, qm = w.agent("bot2")
    mine, _ = w.engine.open_wait(w.p(p), w.m(m), "w1", 50, conn_id=7)
    other, _ = w.engine.open_wait(w.p(q), w.m(qm), "w1", 50, conn_id=8)
    assert w.engine.close_conn_sinks(7) == [ResolveSink(mine.id, {"status": "cancelled"}), Snapshot(w.room.id)]
    assert w.engine.sinks.get(mine.id).close_reason == "disconnect"
    assert w.engine.sinks.get(other.id).open
    assert w.engine.close_conn_sinks(7) == []


# ---------------------------------------------------- pulls and expiries
def test_read_with_nothing_pending_is_just_the_header(w: World) -> None:
    p, m = w.agent("bot")
    text, bid, count, more, _acts = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert (bid, count, more) == (None, 0, False)
    assert "#build" in text and "id=" not in text


def test_a_reply_names_the_message_it_answers(w: World) -> None:
    p, m = w.agent("bot")
    _q, qm = w.agent("peer")
    question = w.human("which parser?")
    reply = w.store.insert_message(w.room.id, sender_name="peer", sender_kind="agent", via="mcp",
                                   text="the new one", sender_membership_id=qm.id, sender_harness="test",
                                   reply_to=question.id)
    w.actions += w.engine.on_message(reply.id)
    text, _bid, count, _more, _acts = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert count == 2
    [line] = [x for x in text.splitlines() if f"id={reply.id} " in x]
    assert f"reply_to={question.id} " in line
    [line] = [x for x in text.splitlines() if f"id={question.id} " in x]
    assert "reply_to=" not in line


def test_an_agent_that_acks_otherwise_is_not_confirmed_by_its_next_call(w: World) -> None:
    p, m = w.agent("bot", ack="never")
    msg = w.human("hi")
    _text, bid, _n, _more, _acts = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert w.engine.before_call(w.p(p)) == []
    assert w.store.get_batch(bid).state == "offered" and w.states(m)[msg.id] == "offered"


def test_a_stop_older_than_a_read_answer_does_not_take_it_back(w: World, clock: FakeClock) -> None:
    """A turn boundary expires the pull answers made before it (§8.7); one that
    reached the broker late (``t`` before the answer was made) leaves the answer be."""
    p, m = w.agent("bot", ack="never")
    w.human("hi")
    t_before = clock.now()
    clock.advance(1)
    _text, bid, _n, _more, _acts = w.engine.pull(w.p(p), w.m(m), "read", 20)
    w.hook(p, "Stop", t=t_before)
    assert w.store.get_batch(bid).state == "offered"
    w.hook(p, "Stop", t=clock.advance(1))
    b = w.store.get_batch(bid)
    assert (b.state, b.expire_reason) == ("expired", "hook:Stop")


def test_a_second_expiry_of_a_settled_offer_changes_nothing(w: World) -> None:
    p, m = w.agent("bot", ack="never")
    msg = w.human("hi")
    _text, bid, _n, _more, _acts = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert w.engine.on_expire(bid, "newer_read")  # back to pending, re-evaluated
    attempts = w.delivery(m, msg)["attempts"]
    assert w.engine.on_expire(bid, "no_confirm") == []
    b = w.store.get_batch(bid)
    assert (b.state, b.expire_reason) == ("expired", "newer_read")
    assert w.delivery(m, msg)["attempts"] == attempts


# ------------------------------------------------------------ Cursor parks
def test_hold_unparks_the_member(w: World) -> None:
    _p, m = cursor(w, status="idle")
    w.human("@cursor-1 look", mentions=("cursor-1",))
    assert w.engine.parked_reason(m.id)
    w.store.set_held(m.id, True)
    assert w.engine.on_command(w.room.id, "hold", m.id) == [Snapshot(w.room.id)]
    assert w.engine.parked_reason(m.id) is None
    assert w.store.count_events("unparked") == 1


def test_open_park_keeps_one_live_park_per_conversation(w: World) -> None:
    p, _m = cursor(w, status="idle")
    s1, _acts = w.engine.open_park(w.p(p), cursor_stop(), 60)
    s2, acts = w.engine.open_park(w.p(p), cursor_stop(), 60)
    # the older park resolves with no continuation
    assert ResolveSink(s1.id, {"status": "cancelled"}) in acts
    old = w.engine.sinks.get(s1.id)
    assert old.closed and old.close_reason == "superseded" and old.result is None
    assert w.engine.sinks.parks_for(p.id) == [s2] and s2.open


def test_a_park_filled_while_the_session_already_shows_busy_leaves_its_status(w: World) -> None:
    p, m = cursor(w, status="busy")
    msg = w.human("please look")  # busy: a wake waits for a stop hook
    before = (w.p(p).status, w.p(p).status_src, w.store.count_events("status"))
    sink, _acts = w.engine.open_park(w.p(p), cursor_stop(), 60)
    res = park_result(w, sink.id)
    assert res is not None and res["status"] == "messages" and f"id={msg.id}" in res["text"]
    # the follow-up starts a turn; the session is busy already: no status write, no event
    assert (w.p(p).status, w.p(p).status_src, w.store.count_events("status")) == before


def test_a_call_after_a_kick_does_not_confirm_the_cancelled_followup(w: World) -> None:
    d = w.cfg.delivery
    other = w.store.create_room("#other", "alice", d.budget_per_hour, d.hop_limit)
    p, m = cursor(w, status="busy")
    w.store.create_membership(other.id, p.id, "cursor-1", "h2")
    w.human("x")
    res = park_result(w, stop(w, p).sink_id)
    w.engine.on_hook_ack(res["batch_id"], res["ack"])  # printed by the stop hook
    leave(w, m.id, "kick")  # kicked from #build; still in #other
    confirms = w.store.count_events("confirm")
    # the session's next tool call (in #other) is not the cancelled follow-up's turn
    assert w.engine.before_call(w.p(p)) == []
    b = w.store.get_batch(res["batch_id"])
    assert b.state == "cancelled" and b.turn_start_at is None and b.first_action_at is None
    assert w.store.count_events("confirm") == confirms
    assert w.store.count_events("first_action") == 0


def test_an_acked_followup_of_a_session_that_ended_leaves_it_offline(w: World, clock: FakeClock) -> None:
    p, _m = cursor(w, status="busy")
    w.human("x")
    res = park_result(w, stop(w, p).sink_id)
    w.engine.on_hook_ack(res["batch_id"], res["ack"])
    assert w.p(p).status == "busy" and w.p(p).status_src == "stop:followup"
    for ended in w.store.end_participant(p.id, "session_end"):
        w.actions += w.engine.on_membership_ended(ended.id, "session_end")
    clock.advance(FOLLOWUP_CONFIRM_S + 1)
    w.actions += w.engine.tick()
    # the no-hook expiry would undo the follow-up's busy; an ended session stays offline
    assert res["batch_id"] not in w.engine.continues
    assert w.p(p).status == "offline" and not w.p(p).active
    assert w.store.get_batch(res["batch_id"]).state == "cancelled"


# ------------------------------------------------------------- hook acks
def test_a_context_ack_after_the_member_left_confirms_nothing(w: World) -> None:
    p, m = devin(w)
    w.human("mid-task note")
    out = w.hook(p, "PostToolUse", tool="read", ok=True)
    assert out is not None and out.kind == "context" and out.batch_id in w.engine.hook_acks
    leave(w, m.id)
    confirms = w.store.count_events("confirm")
    assert w.engine.on_hook_ack(out.batch_id, out.ack) == []
    b = w.store.get_batch(out.batch_id)
    assert b.state == "cancelled" and b.evidence is None
    assert w.store.count_events("confirm") == confirms
    assert out.batch_id not in w.engine.hook_acks  # nothing left for the tick to expire


# ------------------------------------------------------------- the rest
def test_only_chat_messages_are_delivered(w: World) -> None:
    p, m = w.agent("bot")
    sink, _acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    notice = w.store.insert_message(w.room.id, sender_name="switchboard", sender_kind="system",
                                    via="system", kind="notice", text="alice paused the room")
    assert w.engine.on_message(notice.id) == []
    assert w.engine.on_message(notice.id + 1000) == []  # no such message
    assert w.engine.sinks.get(sink.id).open  # a notice never fills a wait()


def test_leaving_drops_the_peer_mark_of_the_cancelled_chatter_batch(w: World) -> None:
    p, m = w.agent("bot", ack="never")
    _q, qm = w.agent("peer")
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    w.actions += acts
    w.agent_says(qm, "some chatter")
    [res] = w.resolved(sink.id)
    bid = res["batch_id"]
    assert w.engine.peer_marks.get(bid) == w.p(p).boundary_seq  # used once it is confirmed
    leave(w, m.id)
    assert w.store.get_batch(bid).state == "cancelled"
    assert bid not in w.engine.peer_marks


def test_refresh_tier_leaves_harnesses_without_a_derived_tier_alone(w: World) -> None:
    p, _m, _c = claude(w)
    before = w.p(p)
    assert w.engine.refresh_tier(p.id) == []
    after = w.p(p)
    assert (after.tier, after.tier_note) == (before.tier, before.tier_note)
    assert w.store.count_events("tier") == 0
    assert w.engine.refresh_tier(p.id + 1000) == []  # no such participant


def test_a_permission_mode_change_alone_refreshes_the_room(w: World) -> None:
    p, _m, _c = claude(w, mode="prompting")
    w.take()
    w.hook(p, "PreToolUse", tool="Bash", permission_mode="bypassPermissions")
    assert w.p(p).approval_mode == "bypass"
    assert Snapshot(w.room.id) in w.take()  # the member list shows the new mode
    w.hook(p, "PreToolUse", tool="Bash", permission_mode="bypassPermissions")
    assert w.take() == []  # unchanged: nothing to refresh


def test_no_reminder_after_clear_for_a_session_in_no_room(w: World) -> None:
    p, m, _c = claude(w)
    leave(w, m.id)
    assert w.hook(p, "SessionStart", source="clear", sid="new-session-id") is None
    assert w.p(p).session_id == "new-session-id"  # the hook itself was still taken


# ---------------------------------------------------------- Devin taint
def test_a_background_subagent_with_no_prompt_id_taints_only_the_current_prompt(w: World) -> None:
    p, _m = devin(w)
    assert w.p(p).gen is None  # no UserPromptSubmit seen yet
    w.hook(p, "PreToolUse", tool="run_subagent", subagent_bg=True)
    assert w.p(p).gen_tainted
    assert not w.engine.tainted_gens.get(p.id)  # no prompt id to remember
    w.hook(p, "UserPromptSubmit", gen="p1")
    assert not w.p(p).gen_tainted


def test_the_taint_memory_keeps_only_the_newest_prompts(w: World) -> None:
    p, _m = devin(w)
    n = TAINTED_GENS_MAX + 1
    for i in range(n):
        w.hook(p, "UserPromptSubmit", gen=f"p{i}")
        w.hook(p, "PreToolUse", gen=f"p{i}", tool="run_subagent", subagent_bg=True)
    assert list(w.engine.tainted_gens[p.id]) == [f"p{i}" for i in range(1, n)]
    w.hook(p, "UserPromptSubmit", gen="p0")  # the oldest was forgotten: a reused id starts clean
    assert not w.p(p).gen_tainted
    w.hook(p, "UserPromptSubmit", gen="p1")  # still remembered
    assert w.p(p).gen_tainted


# ------------------------------------------------ guards: ended, paused, held
def test_an_ended_session_is_offered_nothing_even_with_a_membership_row_left(
        w: World, clock: FakeClock) -> None:
    """The store ends a session's memberships with it (``end_participant``). Should a
    membership row ever outlive its participant, the engine still offers it nothing
    and the watchdog leaves its @mentions alone."""
    p, m = w.agent("bot")
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 3600)
    w.actions += acts
    first = w.human("@bot please look", mentions=("bot",))
    assert w.resolved(sink.id)[0]["status"] == "messages"
    w.engine.before_call(w.p(p))  # --ack next_call: the mention is in context
    assert w.states(m)[first.id] == "in_context"
    w.store.update_participant(p.id, ended_at=clock.now())
    w.take()
    second = w.human("anyone?")
    assert w.states(m)[second.id] == "pending"
    assert not [a for a in w.take() if isinstance(a, (Push, ResolveSink))]
    clock.advance(w.cfg.delivery.watchdog_s + 1)
    assert w.engine.watchdog() == []
    d = w.delivery(m, first)
    assert (d["state"], d["reminders"]) == ("in_context", 0)


@pytest.mark.parametrize("how", ["paused", "held"])
def test_the_parked_escalation_skips_a_paused_room_or_a_held_member(w: World, clock: FakeClock, how: str) -> None:
    """Paused rooms and held members are left alone by the watchdog (§8.5), even
    while the member is still recorded as parked."""
    p, m = cursor(w, status="idle")  # stopped, no stop hook waiting: nothing can reach it
    msg = w.human("@cursor-1 look", mentions=("cursor-1",))
    assert "not parked" in (w.engine.parked_reason(m.id) or "")
    clock.advance(w.cfg.delivery.watchdog_s + 1)
    if how == "paused":
        w.store.set_paused(w.room.id, True, "x")
    else:
        w.store.set_held(m.id, True)
    assert not [a for a in w.engine.watchdog() if isinstance(a, Notice)]
    assert w.store.count_events("watchdog_escalate") == 0
    # live again: the same parked member is escalated
    if how == "paused":
        w.store.set_paused(w.room.id, False)
    else:
        w.store.set_held(m.id, False)
    [n] = [a for a in w.engine.watchdog() if isinstance(a, Notice)]
    assert "cursor-1 is parked" in n.text and f"#{msg.id}" in n.text
    assert w.store.count_events("watchdog_escalate") == 1


# ------------------------------------------------------------ /close (§28.3)
def close(w: World, *membership_ids: int) -> None:
    """What ``RoomService.close_room`` does: end each membership keeping its credential,
    rename the room, then tell the engine."""
    for mid in membership_ids:
        w.store.end_membership(mid, "closed", keep_cred=True)
    w.store.rename_room(w.room.id, closed_room_name("#build", w.room.id), expect="#build")
    for mid in membership_ids:
        w.actions += w.engine.on_membership_ended(mid, "closed")


def test_a_close_resolves_an_open_wait_with_the_rooms_display_name(w: World) -> None:
    p, m = w.agent("bot")
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    w.actions += acts
    close(w, m.id)
    assert w.store.room_by_id(w.room.id).name == f"#build~closed-{w.room.id}"
    assert w.resolved(sink.id) == [{
        "status": "closed",
        "text": f"[switchboard] #build was closed by {w.cfg.human_name}; you are no longer in it.",
    }]
    ended = w.store.get_membership(m.id)
    assert ended.left_reason == "closed" and not ended.kicked


def test_a_close_names_this_room_when_the_room_row_is_gone(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    p, m = w.agent("bot")
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    w.actions += acts
    w.store.end_membership(m.id, "closed", keep_cred=True)
    monkeypatch.setattr(w.store, "room_by_id", lambda rid: None)
    w.actions += w.engine.on_membership_ended(m.id, "closed")
    assert w.resolved(sink.id) == [{
        "status": "closed",
        "text": f"[switchboard] this room was closed by {w.cfg.human_name}; you are no longer in it.",
    }]


def test_a_close_releases_the_parks_of_a_session_with_no_room_left(w: World) -> None:
    p, m = cursor(w, status="idle")
    s, _acts = w.engine.open_park(w.p(p), cursor_stop(), 60)
    assert w.engine.sinks.parks_for(p.id) == [s]
    close(w, m.id)
    assert w.engine.sinks.parks_for(p.id) == []
    assert park_result(w, s.id) == {}
