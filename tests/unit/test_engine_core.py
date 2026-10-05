"""Engine core with a FakeClock: offers, two-phase confirmation, expiry, sinks,
pause, hook claims (DESIGN.md §8.2, §8.5, §8.7)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import FakeClock
from engine_world import KEY, World

from switchboard import envelope
from switchboard.config import Config
from switchboard.models import Push, Snapshot


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def token_of(text: str) -> tuple[int, str]:
    m = envelope.TOKEN_RE.search(text)
    assert m, text
    return int(m.group(1)), m.group(2)


# ------------------------------------------------------------------ sinks
def test_open_wait_is_filled_by_a_human_message(w: World) -> None:
    p, m = w.agent("bot")
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    w.actions += acts
    assert w.p(p).status == "idle"  # test harness: an open sink is idle
    msg = w.human("please look")
    [res] = w.resolved(sink.id)
    assert res["status"] == "messages" and "please look" in res["text"] and res["count"] == 1
    b = w.store.get_batch(res["batch_id"])
    assert b.path == "wait" and b.kind == "wake" and b.wake_kind == "wait_return" and b.budget_counted
    assert w.states(m)[msg.id] == "offered"
    assert w.p(p).status == "busy"
    # --ack next_call: the member's next call confirms it
    w.engine.before_call(w.p(p))
    assert w.states(m)[msg.id] == "in_context"
    assert w.m(m).cursor_id == msg.id


def test_a_pending_message_fills_a_new_wait_at_once(w: World) -> None:
    p, m = w.agent("bot")
    msg = w.human("before the wait")
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    w.actions += acts
    [res] = w.resolved(sink.id)
    assert res["status"] == "messages" and f"id={msg.id} " in res["text"]


def test_newer_wait_supersedes_and_takes_back_unconfirmed_answers(w: World) -> None:
    p, m = w.agent("bot", ack="never")
    s1, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    w.actions += acts
    msg = w.human("one")
    assert w.resolved(s1.id)[0]["status"] == "messages"
    assert w.states(m)[msg.id] == "offered"
    s2, acts = w.engine.open_wait(w.p(p), w.m(m), "w2", 50)
    w.actions += acts
    # the unconfirmed answer went back to pending and was offered again to the new wait
    [res2] = w.resolved(s2.id)
    assert f"id={msg.id} " in res2["text"]
    assert w.delivery(m, msg)["attempts"] == 1


def test_newer_wait_supersedes_an_open_one(w: World) -> None:
    p, m = w.agent("bot")
    s1, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    w.actions += acts
    s2, acts = w.engine.open_wait(w.p(p), w.m(m), "w2", 50)
    w.actions += acts
    assert w.resolved(s1.id) == [{"status": "superseded", "text": w.resolved(s1.id)[0]["text"]}]
    assert s2.open and not s1.open


def test_wait_timeout_and_paused_at_timeout(w: World, clock: FakeClock) -> None:
    p, m = w.agent("bot")
    s1, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 5)
    w.actions += acts
    clock.advance(5)
    w.actions += w.engine.tick()
    assert w.resolved(s1.id)[0]["status"] == "timeout"
    w.store.set_paused(w.room.id, True, "paused by alice")
    s2, acts = w.engine.open_wait(w.p(p), w.m(m), "w2", 5)
    w.actions += acts
    w.human("while paused")
    assert w.resolved(s2.id) == []  # a wait issued during a pause stays open...
    clock.advance(5)
    w.actions += w.engine.tick()
    assert w.resolved(s2.id)[0]["status"] == "paused"  # ...and returns paused, never spins


def test_pause_answers_open_waits_and_cancels_unposted_offers(w: World) -> None:
    p, m = w.agent("bot")
    s1, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    w.actions += acts
    p2, m2 = w.agent("pushy")
    msg = w.human("x")
    # an unposted push offer (as the Claude inbox will make in M3)
    w.store.expire_batch  # noqa: B018 (exists)
    items = [(msg.id, True)]
    w.store.con.execute("UPDATE deliveries SET state='pending' WHERE membership_id=?", (m2.id,))
    b = w.store.create_batch(m2.id, path="inbox", kind="wake", items=items)
    w.store.set_paused(w.room.id, True, "paused by alice")
    w.actions += w.engine.on_command(w.room.id, "pause")
    assert w.resolved(s1.id)[-1]["status"] in ("paused", "messages")
    assert w.store.get_batch(b.id).state == "cancelled"
    assert w.states(m2)[msg.id] == "pending"
    assert w.delivery(m2, msg)["attempts"] == 0  # cancelled, not an attempt


def test_unwait_takes_back_what_the_wait_was_given(w: World) -> None:
    p, m = w.agent("bot")
    s1, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    w.actions += acts
    msg = w.human("x")
    assert w.states(m)[msg.id] == "offered"
    w.actions += w.engine.unwait(w.p(p), "w1")
    assert w.states(m)[msg.id] == "pending"
    assert w.store.get_batch(w.resolved(s1.id)[0]["batch_id"]).expire_reason == "unwait"


# --------------------------------------------------------------- pull acks
def test_read_with_ack_never_never_skips(w: World, clock: FakeClock) -> None:
    p, m = w.agent("bot", ack="never")
    msgs = [w.human(f"m{i}") for i in range(3)]
    text, bid, count, more, acts = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert count == 3 and not more
    # a newer read takes back the unconfirmed answer: same ids again
    text2, bid2, count2, _, acts = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert bid2 != bid and count2 == 3
    assert re.findall(r"id=(\d+) ", text2) == [str(x.id) for x in msgs]
    # and with no newer call, it expires after pull_ack_s
    clock.advance(w.cfg.delivery.pull_ack_s)
    w.actions += w.engine.tick()
    assert w.store.get_batch(bid2).state == "expired"
    assert all(s == "pending" for s in w.states(m).values())


def test_ack_immediate_confirms_on_answer(w: World) -> None:
    p, m = w.agent("bot", ack="immediate")
    msg = w.human("x")
    _t, bid, _c, _m, _a = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert w.store.get_batch(bid).state == "confirmed"
    assert w.states(m)[msg.id] == "in_context"


def test_read_limit_and_more(w: World) -> None:
    p, m = w.agent("bot", ack="immediate")
    for i in range(5):
        w.human(f"m{i}")
    text, _bid, count, more, _ = w.engine.pull(w.p(p), w.m(m), "read", 2)
    assert count == 2 and more and 'call read("#build") again' in text


# ------------------------------------------------------------------ hooks
def test_hook_context_priority_only_with_peer_stubs_and_ack(w: World, clock: FakeClock) -> None:
    p, m = w.agent("bot", status="busy")
    _p2, m2 = w.agent("peer")
    chat = w.agent_says(m2, "idle chatter")
    ment = w.agent_says(m2, "@bot what do you think", mentions=("bot",))
    hum = w.human("human says hi")
    out = w.hook(p, "PostToolUse", ok=True)
    assert out is not None and out.kind == "context" and out.ack
    assert f"id={hum.id} " in out.text and "human says hi" in out.text
    assert f"id={ment.id} " in out.text and 'text=(not shown here; call read("#build"))' in out.text
    assert "what do you think" not in out.text  # peer text is never inline on hook paths
    assert f"id={chat.id} " not in out.text  # chatter never mid-task
    b = w.store.get_batch(out.batch_id)
    assert b.path == "hook_ctx" and b.kind == "priority" and not b.budget_counted
    w.actions += w.engine.on_hook_ack(out.batch_id, "wrong")
    assert w.store.get_batch(out.batch_id).state == "offered"
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    assert w.store.get_batch(out.batch_id).state == "confirmed"
    assert w.states(m)[hum.id] == "in_context"
    d = w.delivery(m, ment)
    assert d["state"] == "pending" and d["notified_at"] is not None  # stub: notified, pull-only now
    # read() returns it inline (a pull path), with the chatter
    text, *_ = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert "what do you think" in text and "idle chatter" in text


def test_notified_stub_never_wakes_again(w: World) -> None:
    p, m = w.agent("bot", status="busy")
    _p2, m2 = w.agent("peer")
    ment = w.agent_says(m2, "@bot look", mentions=("bot",))
    out = w.hook(p, "PostToolUse", ok=True)
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    assert w.delivery(m, ment)["notified_at"] is not None
    assert w.hook(p, "PostToolUse", ok=True) is None  # never pushed or given as hook context again
    # but it was never read: a wait() (a pull, like read()) takes it at once, whole (§24)
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w", 50)
    w.actions += acts
    [res] = w.resolved(sink.id)
    assert res["status"] == "messages" and '"@bot look"' in res["text"]
    w.actions += w.engine.before_call(w.p(p))  # --ack next_call: now in context
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w2", 50)
    w.actions += acts
    assert w.resolved(sink.id) == [] and sink.open  # shown once: it doesn't fill a wait again


def test_hook_ack_timeout_expires_back_to_pending(w: World, clock: FakeClock) -> None:
    p, m = w.agent("bot", status="busy")
    msg = w.human("x")
    out = w.hook(p, "PostToolUse", ok=True)
    clock.advance(w.cfg.delivery.hook_ack_s)
    w.actions += w.engine.tick()
    assert w.store.get_batch(out.batch_id).state == "expired"
    d = w.delivery(m, msg)
    assert d["state"] == "pending" and d["attempts"] == 1


def test_tokens_confirm_only_the_members_own_batches(w: World) -> None:
    p, m = w.agent("bot", harness="claude", status="busy", hooks=True)
    p2, m2 = w.agent("other", harness="claude", status="busy", hooks=True)
    w.human("x")
    _t, bid, *_ = w.engine.pull(w.p(p), w.m(m), "read", 20)
    _t2, bid2, *_ = w.engine.pull(w.p(p2), w.m(m2), "read", 20)
    tok = envelope.batch_token(KEY, bid, m.id)
    b, mac = token_of(tok)
    # the other member presents bot's token: nothing happens
    w.hook(p2, "PostToolUse", ok=True, tokens=((b, mac),))
    assert w.store.get_batch(bid).state == "offered"
    # a forged mac: nothing
    w.hook(p, "PostToolUse", ok=True, tokens=((b, "00000000"),))
    assert w.store.get_batch(bid).state == "offered"
    # a failed tool call never confirms
    w.hook(p, "PostToolUseFailure", ok=False, tokens=((b, mac),))
    assert w.store.get_batch(bid).state == "offered"
    w.hook(p, "PostToolUse", ok=True, tokens=((b, mac),))
    assert w.store.get_batch(bid).state == "confirmed"
    assert w.store.get_batch(bid2).state == "offered"


def test_turn_boundary_expires_unconfirmed_pull(w: World, clock: FakeClock) -> None:
    p, m = w.agent("bot", harness="claude", status="busy", hooks=True)
    w.human("x")
    _t, bid, *_ = w.engine.pull(w.p(p), w.m(m), "read", 20)
    clock.advance(1)
    w.hook(p, "Stop")
    assert w.store.get_batch(bid).state == "expired"


def test_hook_status_transitions(w: World) -> None:
    p, m = w.agent("bot", harness="claude", status="starting")
    w.hook(p, "SessionStart", source="startup")
    assert w.p(p).status == "idle"
    w.hook(p, "UserPromptSubmit", gen="g1", permission_mode="default")
    assert w.p(p).status == "busy" and w.p(p).gen == "g1" and w.p(p).approval_mode == "prompting"
    seq = w.p(p).boundary_seq
    w.hook(p, "PostToolUse", ok=True, permission_mode="bypassPermissions")
    assert w.p(p).status == "busy" and w.p(p).approval_mode == "bypass"
    w.hook(p, "Stop")
    assert w.p(p).status == "idle" and w.p(p).boundary_seq == seq + 1
    w.hook(p, "Stop")  # already idle: no second boundary
    assert w.p(p).boundary_seq == seq + 1
    w.hook(p, "SessionEnd", reason="clear")
    assert w.p(p).status == "idle"  # /clear keeps the session
    w.store.set_status(p.id, "waiting-approval", "claude:registry")
    w.hook(p, "PostToolUse", ok=True)
    assert w.p(p).status == "waiting-approval"  # hooks never touch the approval hold
    w.hook(p, "SessionEnd", reason="prompt_input_exit")
    assert w.p(p).status == "offline"


def test_no_hook_output_while_waiting_approval(w: World) -> None:
    p, m = w.agent("bot", harness="claude", status="waiting-approval", hooks=True)
    w.human("x")
    assert w.hook(p, "PostToolUse", ok=True) is None


def test_session_start_clear_prints_a_reminder_only(w: World) -> None:
    p, m = w.agent("bot", harness="claude", status="busy")
    assert w.hook(p, "SessionStart", source="startup") is None
    out = w.hook(p, "SessionStart", source="clear")
    assert out is not None and out.batch_id is None and "#build as bot" in out.text
    assert out.text.startswith("[switchboard]")


def test_devin_and_bound_cursor_posttooluse_carry_priority_context_from_m5(w: World) -> None:
    for h in ("devin", "cursor"):
        p, _m = w.agent(f"a-{h}", harness=h, status="busy")
        msg = w.human(f"hi @a-{h}", mentions=(f"a-{h}",))
        out = w.hook(p, "PostToolUse", ok=True)
        assert out is not None and out.kind == "context" and f"id={msg.id}" in out.text
    # a Cursor session not bound to its conversation yet gets nothing through hooks
    p, _m = w.agent("a-pend", harness="cursor", status="busy")
    w.store.update_participant(p.id, bind_state="pending", session_key="cursor:agent:1@1.00")
    w.human("hi @a-pend", mentions=("a-pend",))
    assert w.hook(p, "PostToolUse", ok=True) is None


def test_codex_posttooluse_carries_priority_context_from_m4(w: World) -> None:
    p, _m = w.agent("a-codex", harness="codex", status="busy")
    msg = w.human("hi @a-codex", mentions=("a-codex",))
    out = w.hook(p, "PostToolUse", ok=True)
    assert out is not None and out.kind == "context" and f"id={msg.id}" in out.text
    for ev in ("UserPromptSubmit", "Stop", "Interrupt", "SessionEnd"):
        assert w.hook(p, ev) is None


def test_idle_without_a_listener_is_parked(w: World) -> None:
    p, m = w.agent("bot", harness="claude", status="idle", hooks=True)
    w.human("anyone there?")
    assert w.engine.parked_reason(m.id)
    assert any(isinstance(a, Snapshot) for a in w.take())
    # a wait() un-parks and delivers
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w", 50)
    w.actions += acts
    assert w.resolved(sink.id)[0]["status"] == "messages"
    assert w.engine.parked_reason(m.id) is None


def test_held_member_gets_nothing_until_release(w: World) -> None:
    p, m = w.agent("bot")
    w.store.set_held(m.id, True)
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w", 50)
    w.actions += acts
    w.human("x")
    assert w.resolved(sink.id) == []
    w.store.set_held(m.id, False)
    w.actions += w.engine.on_command(w.room.id, "release", m.id)
    assert w.resolved(sink.id)[0]["status"] == "messages"


def test_one_offer_at_a_time_and_backstop(w: World, clock: FakeClock) -> None:
    p, m = w.agent("bot", harness="claude", status="busy", hooks=True)
    w.human("first")
    out = w.hook(p, "PostToolUse", ok=True)
    w.human("second")
    assert w.hook(p, "PostToolUse", ok=True) is None  # the first offer is still in flight
    w.engine.hook_acks.clear()  # simulate a lost ack timer: the backstop still expires it
    clock.advance(w.cfg.delivery.offer_backstop_s)
    w.actions += w.engine.tick()
    assert w.store.get_batch(out.batch_id).expire_reason == "backstop"


def test_membership_end_answers_waits(w: World) -> None:
    p, m = w.agent("bot")
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w", 50)
    w.actions += acts
    w.store.end_membership(m.id, "kick", kicked=True)
    w.actions += w.engine.on_membership_ended(m.id, "kick")
    assert w.resolved(sink.id)[0]["status"] == "kicked"


def test_quiet_period_holds_chatter_for_a_waiting_agent(tmp_path: Path, clock: FakeClock) -> None:
    w = World(tmp_path, clock)  # default quiet_s=3, max_hold_s=60
    p, m = w.agent("bot")
    _p2, m2 = w.agent("peer")
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w", 50)
    w.actions += acts
    w.agent_says(m2, "chatter")
    assert w.resolved(sink.id) == []
    clock.advance(2.9)
    w.actions += w.engine.tick()
    assert w.resolved(sink.id) == []
    clock.advance(0.2)
    w.actions += w.engine.tick()
    assert w.resolved(sink.id)[0]["status"] == "messages"


def test_no_push_without_an_attached_inbox(w: World) -> None:
    p, m = w.agent("bot", harness="claude", status="idle", hooks=True)
    w.human("x")
    assert not [a for a in w.take() if isinstance(a, Push)]


def test_three_push_expiries_in_a_row_warn(w: World) -> None:
    from switchboard.models import Notice

    p, m = w.agent("bot")
    for i in range(3):
        msg = w.human(f"m{i}")
        w.store.con.execute(
            "UPDATE deliveries SET state='pending', batch_id=NULL WHERE membership_id=?", (m.id,)
        )
        w.store.con.execute(
            "UPDATE batches SET state='cancelled' WHERE membership_id=? AND state='offered'", (m.id,)
        )
        b = w.store.create_batch(m.id, path="inbox", kind="wake", items=[(msg.id, True)])
        w.actions += w.engine.on_expire(b.id, "no_confirm")
    warns = [a for a in w.take() if isinstance(a, Notice) and "not confirmed" in a.text]
    assert len(warns) == 1 and w.p(p).push_expiries == 3


# ---------------------------------------------------- envelope fitting (review M2)
HTMLISH = ('<div class="x">\n<b>hi</b>\n</div>\n' * 60)[:1400]


def printed_lines(text: str) -> dict[int, str]:
    return {int(m.group(1)): line for line in text.splitlines() if (m := re.match(r"- id=(\d+) ", line))}


def test_hook_context_fits_the_hook_limit_and_every_acked_item_was_shown_whole(w: World) -> None:
    """Escape-heavy text used to overflow the hook's 10,000-char cut, and the cut
    items were acked as in_context: a silent skip."""
    from switchboard.hook import switchboard_hook as hk

    p, m = w.agent("claude-1", harness="claude", status="busy", hooks=True)
    msgs = [w.human(HTMLISH) for _ in range(4)]
    seen: set[int] = set()
    for _ in range(6):
        out = w.hook(p, "PostToolUse", ok=True)
        if out is None:
            break
        shaped = hk.render("claude", "PostToolUse", {"kind": out.kind, "text": out.text})
        assert shaped is not None, len(out.text)  # the hook never has to refuse it
        printed = shaped["hookSpecificOutput"]["additionalContext"]
        assert printed == out.text and len(printed) <= w.engine.adapter(w.p(p)).caps().ctx_max_chars
        lines = printed_lines(printed)
        w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
        for mid, state in w.states(m).items():
            if state == "in_context" and mid in lines:
                assert envelope.sanitize(HTMLISH) in lines[mid]  # whole, not cut
                seen.add(mid)
    assert seen == {x.id for x in msgs}
    assert set(w.states(m).values()) == {"in_context"}


def test_a_cut_item_is_notified_not_delivered_and_read_shows_it_whole(w: World) -> None:
    p, m = w.agent("claude-1", harness="claude", status="busy", hooks=True)
    long = w.human("y" * 3000)  # over the 1,500-char item limit of hook context
    out = w.hook(p, "PostToolUse", ok=True)
    assert "1500 more chars; read() shows full" in out.text
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    d = w.delivery(m, long)
    assert d["state"] == "pending" and d["notified_at"] is not None  # seen in part: pull-only now
    assert w.hook(p, "PostToolUse", ok=True) is None  # no second push of the same item
    text, *_ = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert "y" * 3000 in text and "more chars" not in text


def test_a_single_huge_escape_heavy_item_is_cut_to_fit(w: World) -> None:
    p, m = w.agent("claude-1", harness="claude", status="busy", hooks=True)
    msg = w.human("<" * 1500)  # 9,000+ rendered characters for one 1,500-char message
    out = w.hook(p, "PostToolUse", ok=True)
    cap = w.engine.adapter(w.p(p)).caps().ctx_max_chars
    assert len(out.text) <= cap and f"id={msg.id} " in out.text and "more chars" in out.text
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    assert w.delivery(m, msg)["state"] == "pending"


def test_read_is_capped_by_rendered_size_and_says_more(tmp_path: Path, clock: FakeClock) -> None:
    w = World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0, pull_max_chars=12000))
    p, m = w.agent("bot", status="busy")
    msgs = [w.human("<" * 1000) for _ in range(5)]  # ~6,100 rendered chars each
    text, bid, count, more, acts = w.engine.pull(w.p(p), w.m(m), "read", 50)
    assert count == 1 and more and "More messages are waiting" in text
    assert envelope.sanitize("<" * 1000, None) in text  # whole text, never cut
    assert w.states(m)[msgs[1].id] == "pending"


def test_a_wait_loop_gets_chatter_on_every_new_wait(w: World, clock: FakeClock) -> None:
    """Hooked members bump boundary_seq only on Stop; a wait() loop inside one
    prompt used to get one chatter batch and then only timeouts."""
    p, m = w.agent("claude-1", harness="claude", status="busy", hooks=True)
    _, peer = w.agent("peer")
    w.hook(p, "UserPromptSubmit", gen="g1")
    s1, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 110)
    w.actions += acts
    a = w.agent_says(peer, "chatter A")
    [r1] = w.resolved(s1.id)
    assert r1["status"] == "messages" and f"id={a.id} " in r1["text"]
    clock.advance(1)
    w.hook(p, "PostToolUse", ok=True, tokens=(token_of(r1["text"]),))
    s2, acts = w.engine.open_wait(w.p(p), w.m(m), "w2", 110)
    w.actions += acts
    clock.advance(20)
    b = w.agent_says(peer, "chatter B")
    w.actions += w.engine.tick()
    [r2] = w.resolved(s2.id)
    assert r2["status"] == "messages" and f"id={b.id} " in r2["text"]


@pytest.mark.parametrize(
    "mode,want",
    [
        ("bypassPermissions", "bypass"),
        ("default", "prompting"),
        ("acceptEdits", "prompting"),
        ("plan", "prompting"),
        ("auto", "prompting"),
        ("dontAsk", "prompting"),
        ("never", "unknown"),
        ("x-future", "unknown"),
    ],
)
def test_approval_mode_fails_closed_on_unknown_values(w: World, mode: str, want: str) -> None:
    """Every mode Claude's and Codex's hooks report maps to what it means: Auto and Don't-ask run
    nothing nobody approved, so they are approvals on, not "unknown" (#73). A value no recording
    has shown is still unknown, shown like approvals off (§6.3)."""
    p, _m = w.agent("claude-1", harness="claude", status="busy", hooks=True)
    w.hook(p, "PostToolUse", ok=True, permission_mode=mode)
    assert w.p(p).approval_mode == want
