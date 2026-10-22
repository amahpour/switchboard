"""Engine core with a FakeClock: offers, two-phase confirmation, expiry, sinks,
pause, hook claims (DESIGN.md §8.2, §8.5, §8.7)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import FakeClock
from engine_agents import listener
from engine_world import KEY, World

from switchboard import envelope
from switchboard.config import Config
from switchboard.models import Participant, Snapshot


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


def test_pause_answers_open_waits_and_ends_a_held_stop_hook(w: World) -> None:
    """``/pause`` returns an open ``wait()`` as paused and ends a held Stop hook with nothing, and the
    message that arrives after it waits (nothing is offered into a paused room). Fails if a pause leaves
    a wait open or a held Stop hook parked."""
    p, m = w.agent("bot")
    s1, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    w.actions += acts
    p2, m2, park = listener(w, "lis")
    w.store.set_paused(w.room.id, True, "paused by alice")
    w.actions += w.engine.on_command(w.room.id, "pause")
    assert w.resolved(s1.id)[-1]["status"] == "paused"
    assert w.engine.sinks.get(park.sink_id).open is False
    msg = w.human("x")
    assert w.states(m2)[msg.id] == "pending"


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
    assert count == 2 and more and 'More: read("#build").' in text


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
    w.hook(p, "SessionEnd", reason="prompt_input_exit")
    assert w.p(p).status == "offline"


def test_no_hook_output_while_waiting_approval(w: World) -> None:
    """The guard holds for a session that was put in the state some other way (a store write): a
    Notification of another type doesn't clear it, so the hook that would carry the message says
    nothing. Fails if a notification that says nothing about the prompt ends the hold."""
    p, m = w.agent("bot", harness="claude", status="waiting-approval", hooks=True)
    w.human("x")
    assert w.hook(p, "Notification", notification="idle_prompt") is None
    assert w.p(p).status == "waiting-approval"


# ------------------------------------------------- an open approval prompt, shown to the room (#472)
def prompt(w: World, p: Participant) -> object:
    """Claude Code's Notification for an open permission prompt (the recorded payload's type)."""
    return w.hook(p, "Notification", notification="permission_prompt")


def test_an_open_permission_prompt_shows_the_session_as_waiting_approval_and_holds_delivery(w: World) -> None:
    """Claude Code's Notification of type permission_prompt is how the room learns that a session is
    paused at its person's approval prompt (#472): the status is information for the other agents, who
    decide for themselves whether to wait or move on. It is never answered (§7.3), the status event
    names the hook, and a message for the session stays queued: nothing is offered while the prompt is
    open. Fails if the hook sets nothing, answers with context, or lets a message through."""
    p, m = w.agent("bot", harness="claude", status="busy", hooks=True)
    msg = w.human("hello @bot", mentions=("bot",))
    assert prompt(w, p) is None
    assert w.p(p).status == "waiting-approval"
    assert w.states(m)[msg.id] == "pending"
    [ev] = [e for e in w.store.recent_events(kinds=["status"]) if e.data.get("to") == "waiting-approval"]
    assert ev.data["frm"] == "busy" and ev.data["src"] == "hook:Notification"


@pytest.mark.parametrize(
    ("event", "status"),
    [
        ("PostToolUse", "busy"),  # the person approved and the tool ran
        ("PostToolUseFailure", "busy"),
        ("UserPromptSubmit", "busy"),  # they declined and typed what to do instead
        ("Stop", "idle"),
        ("SessionStart", "idle"),
        ("SessionEnd", "offline"),
    ],
)
def test_the_sessions_next_hook_clears_the_approval_status(w: World, event: str, status: str) -> None:
    """The prompt has no hook of its own when it is answered (Claude Code 2.1.305 and 2.1.307: approved,
    the tool's PostToolUse comes; declined, nothing comes until the person's next prompt), so any next
    hook of the session ends the status, by the rule of its own event. Fails if the status outlives the
    hook that proves the turn moved on."""
    p, m = w.agent("bot", harness="claude", status="busy", hooks=True)
    prompt(w, p)
    assert w.p(p).status == "waiting-approval"
    w.hook(p, event, **({"ok": True} if event == "PostToolUse" else {}))
    assert w.p(p).status == status
    kinds = [(e.data["frm"], e.data["to"]) for e in w.store.recent_events(kinds=["status"], limit=2)]
    assert ("waiting-approval", status) in kinds


def test_a_message_waits_through_the_prompt_and_arrives_in_the_hook_that_clears_it(w: World) -> None:
    """Nothing is offered while the prompt is open, and nothing is lost: the tool's PostToolUse that
    ends the status carries the priority message that waited. Fails if the hook that clears the status
    still holds back the context (the old guard, which looked at the status before the hook set it)."""
    p, m = w.agent("bot", harness="claude", status="busy", hooks=True)
    prompt(w, p)
    msg = w.human("please look @bot", mentions=("bot",))
    assert w.states(m)[msg.id] == "pending"
    out = w.hook(p, "PostToolUse", ok=True)
    assert out is not None and out.kind == "context" and f"id={msg.id}" in out.text


def test_a_prompt_in_an_idle_session_ends_its_listener_like_any_other_hook(w: World) -> None:
    """An idle Claude Code is listening (a parked Stop hook). A permission prompt can open in it while a
    background subagent works; the Notification is a hook of the session, so it ends the listener as every
    hook does (§49.4), and nothing is delivered to the session while it waits. Fails if the park stays open
    and a message could be pushed to a session whose person has a prompt open."""
    p, _m = w.agent("bot", harness="claude", status="busy", hooks=True)
    out = w.hook(p, "Stop", max_wait_s=900.0)
    assert out is not None and out.kind == "park" and out.sink_id is not None
    assert w.p(p).status == "idle"
    prompt(w, p)
    assert w.p(p).status == "waiting-approval"
    [res] = w.resolved(out.sink_id)
    # the listener's round ends, as for any hook; the next Stop starts one
    assert res["status"] == "cancelled"
    w.human("anyone there? @bot", mentions=("bot",))
    assert len([r for r in w.resolved() if r.get("status") == "messages"]) == 0


def test_a_tool_that_ran_in_a_subagent_ends_the_hold_and_changes_nothing_else(w: World) -> None:
    """A subagent's hooks settle nothing for the session (#446), but a tool finishing in one says the
    session's prompt was answered (#472). It moves waiting-approval to busy, and a session in any other
    state stays as it is: an idle one is not woken or made busy by a subagent. Fails if it does more than
    end the hold, or less (a prompt in a subagent that runs for minutes would keep the session waiting)."""
    p, m = w.agent("bot", harness="claude", status="idle", hooks=True)
    assert w.engine.subagent_tool_ran(w.p(p)) == [] and w.p(p).status == "idle"
    w.store.set_status(p.id, "busy", "test")
    prompt(w, p)
    msg = w.human("hello @bot", mentions=("bot",))
    w.actions += w.engine.subagent_tool_ran(w.p(p))
    assert w.p(p).status == "busy" and w.states(m)[msg.id] == "pending"  # nothing was offered or confirmed


def test_a_notification_of_another_type_is_not_a_prompt_and_clears_nothing(w: World) -> None:
    """Claude Code sends Notifications for other things too (idle_prompt, auth_success, ...). The plugin
    asks for permission_prompt only, and the engine checks again: no other type sets the status, and
    none ends it. Fails if any Notification counts as an approval prompt."""
    p, _m = w.agent("bot", harness="claude", status="busy", hooks=True)
    for kind in ("idle_prompt", "auth_success", "", None):
        w.hook(p, "Notification", notification=kind)
        assert w.p(p).status == "busy", kind


@pytest.mark.parametrize("harness", ["codex", "cursor", "devin"])
def test_only_claude_code_has_a_hook_for_its_approval_prompt(w: World, harness: str) -> None:
    """Codex and Devin's only hook at an approval prompt is PermissionRequest and Cursor has none
    (docs/USAGE.md): switchboard registers no permission hook (§11), so no Notification of theirs counts.
    Fails if another harness's adapter takes a Notification for a prompt."""
    p, _m = w.agent(f"a-{harness}", harness=harness, status="busy", hooks=True)
    w.hook(p, "Notification", notification="permission_prompt")
    assert w.p(p).status == "busy"


@pytest.mark.parametrize("source", ("startup", "clear", "compact"))
def test_session_start_restores_framing_in_claudes_verified_output(w: World, source: str) -> None:
    """Claude's verified SessionStart context carries the warning for each fresh context."""
    p, _m = w.agent("bot", harness="claude", status="busy")
    out = w.hook(p, "SessionStart", source=source)
    assert out is not None and out.batch_id is None and "#build as bot" in out.text
    assert envelope.PEER_WARNING in out.text and envelope.PASS_ADVICE in out.text


def test_devin_and_cursor_posttooluse_carry_priority_context(w: World) -> None:
    for h in ("devin", "cursor"):
        p, _m = w.agent(f"a-{h}", harness=h, status="busy")
        msg = w.human(f"hi @a-{h}", mentions=(f"a-{h}",))
        out = w.hook(p, "PostToolUse", ok=True)
        assert out is not None and out.kind == "context" and f"id={msg.id}" in out.text


def test_codex_posttooluse_carries_priority_context(w: World) -> None:
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


# ---------------------------------------------------- envelope fitting (review M2)
HTMLISH = ('<div class="x">\n<b>hi</b>\n</div>\n' * 60)[:1400]


def printed_lines(text: str) -> dict[int, str]:
    return {int(m.group(1)): line for line in text.splitlines() if (m := re.match(r"- id=(\d+) ", line))}


def test_hook_context_fits_the_hook_limit_and_every_acked_item_was_shown_whole(w: World) -> None:
    """Escape-heavy text used to overflow the hook's 10,000-char cut, and the cut
    items were acked as in_context: a silent skip."""
    from switchboard.broker import hook_replies as hk

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
    long = w.human("y" * 3001)  # above the 3,000-char addressed-message limit
    out = w.hook(p, "PostToolUse", ok=True)
    assert "1501 more chars; read() shows full" in out.text
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    d = w.delivery(m, long)
    assert d["state"] == "pending" and d["notified_at"] is not None  # seen in part: pull-only now
    assert w.hook(p, "PostToolUse", ok=True) is None  # no second push of the same item
    text, *_ = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert "y" * 3001 in text and "more chars" not in text


def test_addressed_inline_message_is_in_context_without_read(w: World) -> None:
    """A 3,000-character human message fits the hook context and is confirmed in full (#298)."""
    p, m = w.agent("claude-1", harness="claude", status="busy", hooks=True)
    msg = w.human("y" * 3000)
    out = w.hook(p, "PostToolUse", ok=True)
    assert "y" * 3000 in out.text and "read() shows full" not in out.text
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    assert w.delivery(m, msg)["state"] == "in_context"


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
    assert count == 1 and more and 'More: read("#build").' in text
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
