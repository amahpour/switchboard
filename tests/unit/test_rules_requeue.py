"""Requeue: re-deliver once (DESIGN.md §8.5, FINDINGS §2 1.6 / §12 Claude #2).

Delivery is not handling: a priority item that reached the model but got no
say() or pass() before the turn ended (Stop) goes back to pending once, and
comes back marked ``again=yes`` with the same message id.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeClock
from engine_world import World
from switchboard import envelope
from switchboard.config import Config
from switchboard.delivery import rules
from switchboard.models import Item


def item(mid: int, prio: int, state: str = "in_context", redelivered: bool = False, attempts: int = 0) -> Item:
    return Item(membership_id=1, message_id=mid, prio=prio, mentioned=prio == 1, state=state, batch_id=None,
                attempts=attempts, notified_at=None, ts=0.0, sender_name="alice", sender_kind="human",
                sender_harness=None, text="t", reply_to=None, redelivered=redelivered)


def test_redeliver_ids_picks_unanswered_priority_items_once() -> None:
    items = [item(1, 2), item(2, 1), item(3, 0), item(4, 2, redelivered=True), item(5, 2, state="handled"),
             item(6, 1, attempts=7)]
    assert rules.redeliver_ids(items) == [1, 2, 6]  # attempts don't matter; the redelivered mark does


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def in_context(w: World, p, m, text: str):
    msg = w.human(text)
    out = w.hook(p, "PostToolUse", ok=True)
    assert out is not None and f"id={msg.id} " in out.text
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    assert w.states(m)[msg.id] == "in_context"
    return msg


def test_stop_requeues_an_unanswered_item_once(w: World, clock: FakeClock) -> None:
    p, m = w.agent("claude-1", harness="claude", status="busy", hooks=True)
    msg = in_context(w, p, m, "please add validation")
    w.hook(p, "Stop")
    d = w.delivery(m, msg)
    assert d["state"] == "pending" and d["redelivered"] == 1 and d["attempts"] == 0
    [ev] = w.store.recent_events(kinds=("requeue",), limit=5)
    assert ev.data["reason"] == "redeliver" and ev.data["n"] == 1
    # it comes back with the same id, marked again=yes
    out = w.hook(p, "UserPromptSubmit")  # the next turn's first hook carries it (hook_ups)
    line = next(x for x in out.text.splitlines() if x.startswith(f"- id={msg.id} "))
    assert "again=yes" in line and envelope.AGAIN_NOTE in out.text
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    # a second unanswered turn: no third delivery
    w.hook(p, "Stop")
    assert w.delivery(m, msg)["state"] == "in_context"


def test_say_or_pass_means_handled_and_nothing_comes_back(w: World) -> None:
    p, m = w.agent("claude-1", harness="claude", status="busy", hooks=True)
    msg = in_context(w, p, m, "one")
    w.store.mark_handled(m.id)  # what agent.say / agent.pass do
    w.hook(p, "Stop")
    assert w.delivery(m, msg)["state"] == "handled"
    assert w.store.recent_events(kinds=("requeue",), limit=5) == []


def test_chatter_is_never_redelivered(w: World) -> None:
    p, m = w.agent("claude-1", harness="claude", status="idle", hooks=True)
    _p2, peer = w.agent("peer")
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 110)
    w.actions += acts
    msg = w.agent_says(peer, "just chatter")
    [res] = w.resolved(sink.id)
    w.hook(p, "PostToolUse", ok=True, tokens=((res["batch_id"], envelope.TOKEN_RE.search(res["text"]).group(2)),))
    assert w.delivery(m, msg)["state"] == "handled"
    w.hook(p, "Stop")
    assert w.delivery(m, msg)["state"] == "handled"


def test_interrupt_and_registry_idle_never_redeliver(w: World) -> None:
    """An aborted turn must not be continued: only a real Stop re-delivers."""
    p, m = w.agent("codex-1", harness="codex", status="busy", hooks=True)
    msg = w.human("x")
    w.store.create_batch(m.id, path="read", kind="pull", items=[(msg.id, True)])
    b = w.store.offered_batches(m.id)[0]
    w.store.confirm_batch(b.id, "test")
    assert w.states(m)[msg.id] == "in_context"
    w.hook(p, "Interrupt")
    assert w.states(m)[msg.id] == "in_context"
    pc, mc = w.agent("claude-1", harness="claude", status="busy", hooks=True)
    m2 = w.human("y")
    b2 = w.store.offered_batches(mc.id)
    assert b2 == []
    w.store.create_batch(mc.id, path="read", kind="pull", items=[(m2.id, True)])
    w.store.confirm_batch(w.store.offered_batches(mc.id)[0].id, "test")
    w.actions += w.engine.set_status(w.p(pc), "idle", "claude:registry", bump=True)
    assert w.states(mc)[m2.id] == "in_context"


def test_requeue_store_only_touches_in_context_rows(w: World) -> None:
    p, m = w.agent("bot", status="busy")
    a = w.human("a")
    b = w.human("b")
    w.store.create_batch(m.id, path="read", kind="pull", items=[(a.id, True)])
    w.store.confirm_batch(w.store.offered_batches(m.id)[0].id, "test")
    assert w.store.requeue(m.id, [a.id, b.id], redeliver=True) == 1
    assert w.delivery(m, a)["redelivered"] == 1 and w.delivery(m, b)["redelivered"] == 0
