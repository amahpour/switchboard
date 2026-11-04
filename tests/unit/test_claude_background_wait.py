"""Background wait results begin a new Claude turn (#558, DESIGN.md §6.1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock
from engine_world import World


@pytest.mark.parametrize("arrives_at", [130, 315, 599])
def test_background_wait_prompt_confirms_only_its_returned_batch(
    tmp_path: Path, clock: FakeClock, arrives_at: int
) -> None:
    """A wait survives the backgrounding Stop, then its result starts a prompt. That prompt must
    confirm the wait before expiry can offer the same message as hook_ups. Other pull results,
    participants, and a foreground wait must still need their ordinary confirmation."""
    w = World(tmp_path, clock)
    p, m = w.agent("claude-1", harness="claude", hooks=True)
    other, om = w.agent("claude-2", harness="claude", hooks=True)
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "long", 600)
    w.actions += acts
    clock.advance(121)
    w.hook(p, "Stop")
    assert sink.open
    clock.advance(arrives_at - 121)
    msg = w.human("please answer once")
    [res] = w.resolved(sink.id)
    bid = res["batch_id"]
    _, other_bid, *_ = w.engine.pull(w.p(other), w.m(om), "read", 20)
    clock.advance(0.1)
    out = w.hook(p, "UserPromptSubmit")
    assert w.store.get_batch(bid).state == "confirmed"
    assert out is None  # no hook_ups carrying the same text
    assert w.delivery(m, msg)["attempts"] == 0
    assert w.states(m)[msg.id] == "in_context"
    assert w.store.get_batch(other_bid).state == "offered"
    clock.advance(w.cfg.delivery.pull_ack_s)
    w.engine.tick()
    assert w.store.get_batch(bid).state == "confirmed"
    assert bid not in w.engine.background_waits

    # A fresh foreground wait's answer is not proof that an unrelated prompt delivered it.
    short, acts = w.engine.open_wait(w.p(p), w.m(m), "short", 50)
    w.actions += acts
    fresh = w.human("ordinary wait")
    [answer] = w.resolved(short.id)
    clock.advance(0.1)
    w.hook(p, "UserPromptSubmit")
    assert w.store.get_batch(answer["batch_id"]).state == "expired"
    assert w.delivery(m, fresh)["attempts"] == 1

    # A prompt before the result is ready is not delivery of that result. It clears the mark.
    # Use a fresh session so the unanswered foreground message cannot fill this wait immediately.
    pending, pm = w.agent("claude-pending", harness="claude", hooks=True)
    open_sink, acts = w.engine.open_wait(w.p(pending), w.m(pm), "pending", 600)
    w.actions += acts
    clock.advance(121)
    w.hook(pending, "Stop")
    w.hook(pending, "UserPromptSubmit")
    assert open_sink.open
    pending_msg = w.human("after the intervening prompt")
    [pending_answer] = w.resolved(open_sink.id)
    clock.advance(0.1)
    w.hook(pending, "UserPromptSubmit")
    assert w.store.get_batch(pending_answer["batch_id"]).state == "expired"
    assert w.delivery(pm, pending_msg)["attempts"] == 1

    # Cancelling a returned background answer must restore it to pending, not confirm it later.
    cancelled, cm = w.agent("claude-cancelled", harness="claude", hooks=True)
    cancel_sink, acts = w.engine.open_wait(w.p(cancelled), w.m(cm), "cancel", 600)
    w.actions += acts
    clock.advance(121)
    w.hook(cancelled, "Stop")
    cancelled_msg = w.human("a cancelled answer")
    [cancelled_answer] = w.resolved(cancel_sink.id)
    w.engine.unwait(w.p(cancelled), "cancel")
    assert cancelled_answer["batch_id"] not in w.engine.background_waits
    assert w.states(cm)[cancelled_msg.id] == "pending"
    clock.advance(0.1)
    w.hook(cancelled, "UserPromptSubmit")
    assert w.store.get_batch(cancelled_answer["batch_id"]).state == "expired"
    assert w.delivery(cm, cancelled_msg)["attempts"] == 1
