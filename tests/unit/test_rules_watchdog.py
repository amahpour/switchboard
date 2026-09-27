"""Watchdog (DESIGN.md §8.5): an @mention that goes unanswered is
brought back as a reminder a bounded number of times (``watchdog_max``, 2), then the
human is told; an @mention waiting on a parked member ("needs a poke") is escalated
too. Paused rooms and held members are left alone; say() or pass() answers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conftest import FakeClock
from engine_world import KEY, World
from switchboard import envelope
from switchboard.config import Config
from switchboard.delivery import rules
from switchboard.models import WATCHDOG_DONE, Item, Notice

NOW = 1_790_000_000.0
W = 120.0  # the default watchdog_s


def item(**kw: Any) -> Item:
    base: dict[str, Any] = dict(
        membership_id=1, message_id=7, prio=1, mentioned=True, state="in_context", batch_id=None,
        attempts=0, notified_at=None, ts=NOW - 500, sender_name="peer", sender_kind="agent",
        sender_harness="test", text="@bot hi", reply_to=None, in_context_at=NOW - W)
    base.update(kw)
    return Item(**base)


def verdict(it: Item, *, now: float = NOW, answered: float | None = None, idle: bool = True,
            wmax: int = 2, ws: float = W) -> str | None:
    return rules.watchdog_verdict(it, now=now, watchdog_s=ws, watchdog_max=wmax, answered_at=answered, idle=idle)


# ------------------------------------------------------------------ pure rules
def test_verdict_table() -> None:
    assert verdict(item()) == "remind"
    assert verdict(item(in_context_at=NOW - W + 0.5)) is None  # not yet
    assert verdict(item(mentioned=False, prio=0)) is None  # only @mentions are watched
    assert verdict(item(prio=2)) == "remind"  # a human @mention too
    assert verdict(item(reminders=1)) == "remind"
    assert verdict(item(reminders=2)) == "escalate"  # two reminders went unanswered: tell the human
    assert verdict(item(reminders=WATCHDOG_DONE + 2)) is None  # finished (escalated)
    assert verdict(item(reminders=WATCHDOG_DONE)) is None  # finished (answered)
    assert verdict(item(), idle=False) is None  # busy in a turn: working on it
    assert verdict(item(), idle=False, answered=NOW - 1) is None  # (settled once it is idle)
    assert verdict(item(), answered=NOW - 1) == "done"  # said or passed since
    assert verdict(item(), answered=NOW - W - 1) == "remind"  # spoke before it arrived: not an answer
    stub = item(state="pending", in_context_at=None, notified_at=NOW - W)
    assert verdict(stub) == "remind"  # a notified "call read()" stub counts from the notification
    assert verdict(item(state="pending", in_context_at=None)) is None  # not delivered yet
    assert verdict(item(state="offered")) is None
    assert verdict(item(state="handled")) is None
    assert verdict(item(), ws=0) is None  # watchdog_s = 0: off
    assert verdict(item(), wmax=0) == "escalate"  # no reminders: tell the human at once


def test_reminded_marks_and_the_envelope() -> None:
    assert not item().reminded and item(reminders=1).reminded
    assert item(reminders=WATCHDOG_DONE + 2).reminded  # escalated: still unanswered
    assert not item(reminders=WATCHDOG_DONE).reminded  # answered
    text = envelope.render_batch([item(reminders=1)], room="#build", recipient="bot", human_name="alice",
                                 token="yk:b1.00000000", peer_inline=True)
    head = text.splitlines()[0]
    assert head.startswith("[switchboard] #build: " + envelope.REMINDER_HEAD) and envelope.PASS_ADVICE in head
    assert "reminder=yes" in text and envelope.REMINDER_NOTE in text
    plain = envelope.render_batch([item()], room="#build", recipient="bot", human_name="alice",
                                  token="yk:b1.00000000", peer_inline=True)
    assert "reminder" not in plain


def test_wake_reason_is_reminder_unless_a_new_human_message_drove_it() -> None:
    assert rules.wake_reason([item(reminders=1)]) == "reminder"
    assert rules.wake_reason([item(reminders=1), item(message_id=8, prio=2)]) == "human"
    assert rules.wake_reason([item(prio=2, reminders=1)]) == "reminder"
    assert rules.wake_reason([item()]) == "mention"


def test_parked_escalation_waits_for_both_the_message_and_the_parking() -> None:
    pend = [item(state="pending", in_context_at=None, ts=NOW - 200),
            item(message_id=8, state="pending", in_context_at=None, ts=NOW - 10),  # too new
            item(message_id=9, state="pending", in_context_at=None, mentioned=False, prio=0, ts=NOW - 500),
            item(message_id=10, state="pending", in_context_at=None, notified_at=NOW - 300, ts=NOW - 400)]
    got = rules.parked_escalation(pend, parked_since=NOW - 130, now=NOW, watchdog_s=W)
    assert [i.message_id for i in got] == [7]
    assert rules.parked_escalation(pend, parked_since=NOW - 60, now=NOW, watchdog_s=W) == []
    assert rules.parked_escalation(pend, parked_since=NOW - 500, now=NOW, watchdog_s=0) == []


# ------------------------------------------------------------------- engine
@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def listen(w: World, p: Any, m: Any, wid: str) -> Any:
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), wid, 3600)
    w.actions += acts
    return sink


def tick(w: World, dt: float) -> None:
    w.clock.advance(dt)
    w.actions += w.engine.tick()


def notices(w: World) -> list[Notice]:
    return [a for a in w.take() if isinstance(a, Notice)]


def seen(w: World, p: Any, m: Any, sink: Any) -> dict[str, Any]:
    """The wait() was filled and the agent's next call confirms it (test --ack next_call)."""
    [res] = w.resolved(sink.id)
    assert res["status"] == "messages", res
    w.actions += w.engine.before_call(w.p(p))
    return res


def test_remind_twice_then_escalate_to_the_human(w: World) -> None:
    p, m = w.agent("bot")
    _pp, peer = w.agent("peer")
    s = listen(w, p, m, "w0")
    msg = w.agent_says(peer, "@bot can you check the parser?", mentions=("bot",))
    first = seen(w, p, m, s)
    assert "reminder" not in first["text"] and w.delivery(m, msg)["state"] == "in_context"
    budget = w.store.room_by_id(w.room.id).budget_remaining
    for n in (1, 2):
        s = listen(w, p, m, f"w{n}")
        tick(w, W - 1)
        assert s.open  # not yet
        tick(w, 1)
        res = seen(w, p, m, s)
        assert f"id={msg.id} " in res["text"] and "reminder=yes" in res["text"]
        assert envelope.REMINDER_HEAD in res["text"] and envelope.PASS_ADVICE in res["text"]
        b = w.store.get_batch(res["batch_id"])
        assert (b.kind, b.wake_kind, b.wake_reason, b.budget_counted) == ("wake", "wait_return", "reminder", True)
        assert w.delivery(m, msg)["reminders"] == n
    assert w.store.count_events("watchdog_remind") == 2
    assert w.store.room_by_id(w.room.id).budget_remaining == budget - 2  # reminders are counted wakes
    s = listen(w, p, m, "w3")
    w.take()
    tick(w, W)
    assert s.open  # no third reminder...
    [note] = notices(w)  # ...the human is told instead
    assert note.level == "warn" and f"bot hasn't answered @mention #{msg.id}" in note.text
    assert note.persist
    d = w.delivery(m, msg)
    assert d["state"] == "pending" and d["notified_at"] is not None and d["reminders"] == WATCHDOG_DONE + 2
    [ev] = w.store.recent_events(kinds=("watchdog_escalate",))
    assert ev.data["why"] == "unanswered" and ev.data["ids"] == [msg.id]
    # it stays readable, but never wakes the agent again
    for _ in range(5):
        tick(w, W)
    assert s.open and not notices(w)
    text, *_ = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert f"id={msg.id} " in text and "can you check the parser?" in text


def test_say_answers_and_nothing_comes_back(w: World) -> None:
    p, m = w.agent("bot")
    _pp, peer = w.agent("peer")
    s = listen(w, p, m, "w0")
    msg = w.agent_says(peer, "@bot ping", mentions=("bot",))
    seen(w, p, m, s)
    w.store.mark_handled(m.id)  # what say() and pass() do
    s = listen(w, p, m, "w1")
    tick(w, 3 * W)
    assert s.open and w.delivery(m, msg)["state"] == "handled"
    assert w.store.count_events("watchdog_remind") == 0 and not notices(w)


def stub_mention(w: World, name: str = "bot") -> tuple[Any, Any, Any]:
    """A peer @mention that reached ``name`` as a "call read()" stub in hook context."""
    p, m = w.agent(name, hooks=True)
    _pp, peer = w.agent(f"peer-{name}")
    msg = w.agent_says(peer, f"@{name} look at this", mentions=(name,))
    out = w.hook(p, "PostToolUse", ok=True)
    assert out is not None and 'call read("#build")' in out.text and "look at this" not in out.text
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    d = w.delivery(m, msg)
    assert d["state"] == "pending" and d["notified_at"] is not None
    return p, m, msg


def test_an_unanswered_stub_is_reminded_inline(w: World) -> None:
    p, m, msg = stub_mention(w)
    # the agent never read() it; when it listens, wait() (a pull) shows it at once (§24)
    s = listen(w, p, m, "w1")
    res = seen(w, p, m, s)
    assert "look at this" in res["text"] and "reminder=yes" not in res["text"]
    assert w.delivery(m, msg)["state"] == "in_context" and w.delivery(m, msg)["reminders"] == 0
    s = listen(w, p, m, "w2")  # still unanswered: the watchdog reminds, inline
    assert s.open
    tick(w, W)
    res = seen(w, p, m, s)
    assert "look at this" in res["text"] and "reminder=yes" in res["text"]
    assert w.delivery(m, msg)["reminders"] == 1 and w.delivery(m, msg)["state"] == "in_context"


def test_an_unread_stub_is_reminded_while_idle(w: World) -> None:
    """Not listening (no wait() to take it): the stub itself is reminded, from its notification."""
    p, m, msg = stub_mention(w)
    w.actions += w.engine.set_status(w.p(p), "idle", "test")
    tick(w, W)
    assert w.store.count_events("watchdog_remind") == 1 and w.delivery(m, msg)["reminders"] == 1


def test_say_answers_a_stub_without_reading_it(w: World) -> None:
    """say() may answer a stub unread (its result shows the unread); pass() may not: it is
    refused until read() has shown the stub (DESIGN.md §24, test_rules_read_before_pass)."""
    p, m, msg = stub_mention(w)
    assert w.engine.check_pass(w.p(p), w.m(m)) == [msg.id]  # pass() here would be refused
    w.store.mark_handled(m.id)  # say() after the stub: handled first, then posted
    d = w.delivery(m, msg)
    assert d["state"] == "pending" and d["reminders"] == WATCHDOG_DONE  # still readable, watchdog done
    mine = w.agent_says(m, "on it")
    # ...and its result shows what it hadn't seen (AgentService.say's pull), the stub included
    text, bid, _n, _more, acts = w.engine.pull(w.p(p), w.m(m), "say", 50, before_id=mine.id)
    w.actions += acts
    assert "look at this" in text and "reminder=yes" not in text
    w.actions += w.engine.before_call(w.p(p))  # the next call (wait) confirms the say() answer
    s = listen(w, p, m, "w1")
    tick(w, 2 * W)
    assert s.open and w.store.count_events("watchdog_remind") == 0
    assert w.delivery(m, msg)["state"] == "in_context" and w.delivery(m, msg)["reminders"] == WATCHDOG_DONE


def test_a_later_say_counts_as_the_answer_for_a_stub(w: World) -> None:
    p, m, msg = stub_mention(w)
    w.clock.advance(5)
    w.agent_says(m, "on it")  # a say from the member (the backstop: no mark_handled here)
    w.actions += w.engine.set_status(w.p(p), "idle", "test")  # idle, not listening
    tick(w, W)
    assert w.delivery(m, msg)["reminders"] == WATCHDOG_DONE
    assert w.store.count_events("watchdog_remind") == 0


def test_a_busy_member_is_reminded_only_once_idle(w: World) -> None:
    p, m = w.agent("bot", hooks=True)  # busy, working
    msg = w.human("@bot please refactor the parser", mentions=("bot",))
    out = w.hook(p, "PostToolUse", ok=True)
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    assert w.delivery(m, msg)["state"] == "in_context"
    w.take()
    for n in range(4):  # a long turn: tool after tool
        tick(w, W)
        w.hook(p, "PostToolUse", ok=True)
        # it can't be reminded, but after (watchdog_max + 1) * watchdog_s the human is told, once
        assert len(notices(w)) == (1 if n == 2 else 0)
    assert w.store.count_events("watchdog_remind") == 0
    [ev] = w.store.recent_events(kinds=("watchdog_escalate",))
    assert ev.data["why"] == "not_idle" and ev.data["status"] == "busy" and ev.data["ids"] == [msg.id]
    s = listen(w, p, m, "w1")  # done, listening: the unanswered mention comes back at once
    tick(w, 1)
    res = seen(w, p, m, s)
    assert "reminder=yes" in res["text"] and w.delivery(m, msg)["reminders"] == 1


def test_stalled_rule() -> None:
    def ids(items: list[Item], **kw: Any) -> list[int]:
        args: dict[str, Any] = dict(now=NOW, watchdog_s=W, watchdog_max=2)
        args.update(kw)
        return [i.message_id for i in rules.stalled(items, **args)]

    old = item(in_context_at=NOW - 3 * W)
    assert ids([old]) == [7]
    assert ids([item(in_context_at=NOW - 3 * W + 1)]) == []  # not yet: (2 + 1) * W
    assert ids([item(in_context_at=NOW - W)], watchdog_max=0) == [7]
    assert ids([item(state="pending", in_context_at=None, notified_at=NOW - 3 * W)]) == [7]  # a stub
    assert ids([item(state="pending", in_context_at=None)]) == []  # never reached it
    assert ids([item(in_context_at=NOW - 3 * W, mentioned=False, prio=0)]) == []
    assert ids([item(in_context_at=NOW - 3 * W, reminders=WATCHDOG_DONE)]) == []
    assert ids([old], answered_at=NOW - 5) == []  # said or passed since
    assert ids([old], answered_at=NOW - 4 * W) == [7]  # spoke before it arrived
    assert ids([old], watchdog_s=0) == []


@pytest.mark.parametrize("status", ["waiting-approval", "offline"])
def test_a_member_stuck_away_from_idle_gets_the_human_told(w: World, status: str) -> None:
    """A member that can't be reminded (on an approval prompt, offline): the human is
    told once the @mention is overdue; nothing wakes it."""
    p, m = w.agent("bot", hooks=True)
    msg = w.human("@bot please check", mentions=("bot",))
    out = w.hook(p, "PostToolUse", ok=True)
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    w.actions += w.engine.set_status(w.p(p), status, "test")
    w.take()
    tick(w, 3 * W - 1)
    assert not notices(w)
    tick(w, 1)
    [note] = notices(w)
    assert note.level == "warn" and f"bot hasn't answered @mention #{msg.id} for 6 min" in note.text
    assert {"waiting-approval": "it is waiting on an approval prompt", "offline": "it is offline"}[status] in note.text
    tick(w, 5 * W)
    assert not notices(w) and w.store.count_events("watchdog_remind") == 0
    assert w.delivery(m, msg)["state"] == "in_context" and w.delivery(m, msg)["reminders"] == 0


def test_a_busy_member_that_answered_is_not_reported(w: World) -> None:
    p, m = w.agent("bot", hooks=True)
    msg = w.human("@bot please check", mentions=("bot",))
    out = w.hook(p, "PostToolUse", ok=True)
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    w.clock.advance(5)
    w.agent_says(m, "looking")  # a say without mark_handled (the backstop)
    w.take()
    tick(w, 4 * W)
    assert not notices(w) and w.delivery(m, msg)["state"] == "in_context"


def test_no_reminders_in_a_paused_room_or_for_a_held_member(w: World) -> None:
    p, m = w.agent("bot")
    s = listen(w, p, m, "w0")
    msg = w.human("@bot status?", mentions=("bot",))
    seen(w, p, m, s)
    w.actions += w.engine.pause_room(w.room.id, "paused by alice")
    s = listen(w, p, m, "w1")
    tick(w, 2 * W)
    assert s.open and w.delivery(m, msg)["reminders"] == 0 and not notices(w)
    w.store.set_paused(w.room.id, False)
    w.store.set_held(m.id, True)
    w.actions += w.engine.on_command(w.room.id, "resume")
    tick(w, W)
    assert s.open and w.delivery(m, msg)["reminders"] == 0
    w.store.set_held(m.id, False)
    w.actions += w.engine.on_command(w.room.id, "release", m.id)
    tick(w, 1)
    assert "reminder=yes" in seen(w, p, m, s)["text"]


def test_budget_exhausted_only_human_reminders_wake(w: World) -> None:
    pa, ma = w.agent("amy")
    pb, mb = w.agent("bob")
    _pp, peer = w.agent("peer")
    sa, sb = listen(w, pa, ma, "a0"), listen(w, pb, mb, "b0")
    human = w.human("@bob from alice", mentions=("bob",))
    seen(w, pa, ma, sa)  # amy sees it too (not addressed to her: not watched)
    seen(w, pb, mb, sb)
    w.store.mark_handled(ma.id)
    sa = listen(w, pa, ma, "a1")
    peer_msg = w.agent_says(peer, "@amy from a peer", mentions=("amy",))
    seen(w, pa, ma, sa)
    w.store.set_budget(w.room.id, 0)
    sa, sb = listen(w, pa, ma, "a2"), listen(w, pb, mb, "b1")
    tick(w, W)
    assert f"id={human.id} " in seen(w, pb, mb, sb)["text"]  # a human's mention still wakes
    assert sa.open  # a peer's waits for the budget...
    d = w.delivery(ma, peer_msg)
    assert d["state"] == "pending" and d["reminders"] == 1  # ...held back, not dropped
    w.store.set_budget(w.room.id, 5)
    w.actions += w.engine.on_command(w.room.id, "budget")
    assert f"id={peer_msg.id} " in seen(w, pa, ma, sa)["text"]


def parked_claude(w: World, name: str = "claude-1") -> tuple[Any, Any]:
    """An idle Claude session with no inbox and no wait(): nothing can reach it."""
    return w.agent(name, harness="claude", status="idle", hooks=True)


def test_a_mention_waiting_on_a_parked_member_escalates_once_per_spell(w: World) -> None:
    p, m = parked_claude(w)
    msg = w.human("@claude-1 are you there?", mentions=("claude-1",))
    assert w.engine.parked_reason(m.id)
    w.take()
    tick(w, W - 1)
    assert not notices(w)
    tick(w, 1)
    [note] = notices(w)
    assert note.level == "warn" and "claude-1 is parked — needs a poke" in note.text
    assert f"#{msg.id}" in note.text and "call wait() or poke it" in note.text
    [ev] = w.store.recent_events(kinds=("watchdog_escalate",))
    assert ev.data["why"] == "parked" and ev.data["ids"] == [msg.id]
    tick(w, 3 * W)
    assert not notices(w)  # once per parked spell
    # a poke (a wait()) delivers it and ends the spell...
    s = listen(w, p, m, "w1")
    assert w.resolved(s.id)[0]["status"] == "messages" and w.engine.parked_reason(m.id) is None
    w.hook(p, "PostToolUse", ok=True, tool="mcp__switchboard__wait",
           tokens=((w.resolved(s.id)[0]["batch_id"],
                    envelope.batch_token(KEY, w.resolved(s.id)[0]["batch_id"], m.id).split(".")[1]),))
    w.store.mark_handled(m.id)
    w.hook(p, "Stop")
    # ...and a new spell escalates again
    msg2 = w.human("@claude-1 one more", mentions=("claude-1",))
    assert w.engine.parked_reason(m.id)
    w.take()
    tick(w, W)
    [note] = notices(w)
    assert f"#{msg2.id}" in note.text and f"#{msg.id}" not in note.text


def test_parked_with_only_chatter_is_not_escalated(w: World) -> None:
    p, m = parked_claude(w)
    _pp, peer = w.agent("peer")
    w.agent_says(peer, "just chatter")
    assert w.engine.parked_reason(m.id)
    w.take()
    tick(w, 2 * W)
    assert not notices(w)


def test_a_paused_room_is_not_parked_and_not_escalated(w: World) -> None:
    p, m = parked_claude(w)
    w.human("@claude-1 hi", mentions=("claude-1",))
    assert w.engine.parked_reason(m.id)
    w.actions += w.engine.pause_room(w.room.id, "paused by alice")
    assert w.engine.parked_reason(m.id) is None and m.id not in w.engine.parked_since
    w.take()
    tick(w, 2 * W)
    assert not notices(w)
    w.store.set_paused(w.room.id, False)
    w.actions += w.engine.on_command(w.room.id, "resume")
    assert w.engine.parked_reason(m.id)
    w.take()
    tick(w, W - 1)
    assert not notices(w)  # the parked spell starts again at /resume
    tick(w, 1)
    assert len(notices(w)) == 1


def test_watchdog_off(tmp_path: Path, clock: FakeClock) -> None:
    w = World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0, watchdog_s=0.0))
    p, m = w.agent("bot")
    s = listen(w, p, m, "w0")
    w.human("@bot x", mentions=("bot",))
    seen(w, p, m, s)
    s = listen(w, p, m, "w1")
    pc, mc = parked_claude(w)
    w.agent_says(m, "@claude-1 y", mentions=("claude-1",))
    assert w.engine.parked_reason(mc.id)
    for _ in range(5):
        tick(w, W)
    assert s.open and not notices(w)


def test_watchdog_max_zero_tells_the_human_without_reminding(tmp_path: Path, clock: FakeClock) -> None:
    w = World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0, watchdog_max=0))
    p, m = w.agent("bot")
    s = listen(w, p, m, "w0")
    msg = w.human("@bot x", mentions=("bot",))
    seen(w, p, m, s)
    s = listen(w, p, m, "w1")
    w.take()
    tick(w, W)
    assert s.open and w.store.count_events("watchdog_remind") == 0
    [note] = notices(w)
    assert f"#{msg.id}" in note.text and "after 0 reminders" in note.text


def escalated(tmp_path: Path, clock: FakeClock, *, hooks: bool = False) -> tuple[World, Any, Any, Any]:
    """``bot`` took a peer's @mention, ignored the one reminder (``watchdog_max = 1``)
    and was escalated; it isn't listening any more."""
    w = World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0, watchdog_max=1))
    p, m = w.agent("bot", hooks=hooks)
    _pp, peer = w.agent("peer")
    s = listen(w, p, m, "w0")
    msg = w.agent_says(peer, "@bot status?", mentions=("bot",))
    seen(w, p, m, s)
    s = listen(w, p, m, "w1")
    tick(w, W)
    assert "reminder=yes" in seen(w, p, m, s)["text"]
    s = listen(w, p, m, "w2")
    w.take()
    tick(w, W)
    assert s.open and len(notices(w)) == 1
    assert w.delivery(m, msg)["reminders"] == WATCHDOG_DONE + 1
    w.actions += w.engine.unwait(w.p(p), "w2")
    w.take()
    return w, p, m, msg


def test_an_answer_after_the_escalation_drops_the_reminder_mark(tmp_path: Path, clock: FakeClock) -> None:
    w, p, m, msg = escalated(tmp_path, clock)
    w.store.mark_handled(m.id)  # say() (or pass()) without reading it first
    text, *_ = w.engine.pull(w.p(p), w.m(m), "say", 50)  # the say() result's unread
    assert f"id={msg.id} " in text  # still unread...
    assert "reminder=yes" not in text and envelope.REMINDER_HEAD not in text  # ...but answered
    assert envelope.REMINDER_NOTE not in text
    assert w.delivery(m, msg)["reminders"] == WATCHDOG_DONE


def test_an_escalated_mention_never_wakes_again_after_read_and_stop(tmp_path: Path, clock: FakeClock) -> None:
    w, p, m, msg = escalated(tmp_path, clock, hooks=True)
    text, bid, _n, _more, acts = w.engine.pull(w.p(p), w.m(m), "read", 20)
    w.actions += acts
    assert f"id={msg.id} " in text and "reminder=yes" in text  # read() says it is still unanswered
    w.actions += w.engine.before_call(w.p(p))  # the next call confirms the read
    assert w.delivery(m, msg)["state"] == "in_context"
    w.hook(p, "Stop")  # the turn ends, still unanswered: no re-delivery for it
    d = w.delivery(m, msg)
    assert d["state"] == "in_context" and d["redelivered"] == 1
    s = listen(w, p, m, "w3")
    for _ in range(4):
        tick(w, W)
    assert s.open and w.store.count_events("watchdog_remind") == 1 and not notices(w)
