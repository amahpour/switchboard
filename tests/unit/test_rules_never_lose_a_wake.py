"""Never lose a wake (DESIGN.md §8.2, §8.7): pending messages
are re-checked on every busy->idle transition; cooldowns (quiet period, budget,
backoffs, hold, pause) defer messages, they never drop them; an offer that isn't
confirmed goes back to pending and is offered again."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeClock
from engine_world import World
from test_claude_adapter import claude, reg, tok
from test_codex_adapter import attach, codex
from switchboard.config import Config
from switchboard.models import Push


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def ids(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"^- id=(\d+) ", text, re.M)]


def pushes(w: World) -> list[Push]:
    return [a for a in w.take() if isinstance(a, Push)]


def result(w: World, sink: Any) -> dict[str, Any] | None:
    s = w.engine.sinks.get(sink.id)
    return None if s is None or s.open else s.result


def test_busy_to_idle_rechecks_pending_on_the_inbox(w: World) -> None:
    p, m, _conn = claude(w, status="busy", registry="busy")
    _pp, peer = w.agent("peer")
    chat = w.agent_says(peer, "chatter while busy")  # not deliverable mid-task
    assert pushes(w) == []
    w.hook(p, "Stop")  # busy -> idle
    reg(w, p, "idle")
    w.actions += w.engine.evaluate_participant(p.id)
    [push] = pushes(w)
    assert ids(push.text) == [chat.id]


def test_busy_to_idle_by_the_codex_link_rechecks_pending(w: World) -> None:
    p, m = codex(w, status="busy")
    attach(w, view="busy")
    _pp, peer = w.agent("peer")
    chat = w.agent_says(peer, "chatter while busy")
    assert pushes(w) == []
    attach(w, view="idle")
    w.actions += w.engine.set_status(w.p(p), "idle", "codex:status", bump=True)
    [push] = pushes(w)
    assert push.path == "turn_start" and ids(push.text) == [chat.id]


def test_a_new_wait_gets_everything_that_arrived_while_busy_human_first(w: World) -> None:
    p, m = w.agent("bot")
    _pp, peer = w.agent("peer")
    chat = w.agent_says(peer, "chatter")
    hum = w.human("alice")
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 50)
    w.actions += acts
    assert ids(result(w, sink)["text"]) == [hum.id, chat.id]


def test_an_unconfirmed_offer_comes_back(w: World, clock: FakeClock) -> None:
    p, m = w.agent("bot", hooks=True)  # busy: hook context
    msg = w.human("x")
    out = w.hook(p, "PostToolUse", ok=True)
    clock.advance(w.cfg.delivery.hook_ack_s)  # the hook never acked (it died, say)
    w.actions += w.engine.tick()
    d = w.delivery(m, msg)
    assert d["state"] == "pending" and d["attempts"] == 1
    out2 = w.hook(p, "PostToolUse", ok=True)
    assert out2 is not None and out2.batch_id != out.batch_id and f"id={msg.id} " in out2.text


def test_a_lost_inbox_frame_is_posted_again(w: World, clock: FakeClock) -> None:
    p, m, _conn = claude(w)
    msg = w.human("did you get this?")
    [push] = pushes(w)
    w.store.mark_posted(push.batch_id)
    got: list[Push] = []
    for _ in range(20):  # 5 s idle with no token: expired; then the backoff; then again
        clock.advance(1)
        reg(w, p, "idle")
        w.actions += w.engine.tick()
        got += pushes(w)
    assert w.store.get_batch(push.batch_id).expire_reason == "idle_no_token"
    assert got and f"id={msg.id} " in got[0].text
    w.hook(p, "UserPromptSubmit", tokens=(tok(got[0].text),))
    assert w.delivery(m, msg)["state"] == "in_context"


def test_a_failed_turn_start_is_retried_after_the_backoff(w: World, clock: FakeClock) -> None:
    p, m = codex(w)
    a = attach(w)
    msg = w.human("x")
    [push] = pushes(w)
    w.store.mark_posted(push.batch_id)
    a._failed(p)  # what send() does on an RPC error
    w.actions += w.engine.on_expire(push.batch_id, "send_error")
    assert pushes(w) == []  # backing off, not dropped
    clock.advance(1.5)
    attach(w)
    w.actions += w.engine.tick()
    [again] = pushes(w)
    assert again.path == "turn_start" and f"id={msg.id} " in again.text


def test_cooldowns_defer_never_drop(w: World, clock: FakeClock) -> None:
    """Pause, hold and an empty budget each hold a wake back; lifting them delivers it."""
    p, m = w.agent("bot")
    _pp, peer = w.agent("peer")
    w.store.set_paused(w.room.id, True, "paused by alice")
    w.actions += w.engine.on_command(w.room.id, "pause")
    w.store.set_held(m.id, True)
    w.store.set_budget(w.room.id, 0)
    sink, acts = w.engine.open_wait(w.p(p), w.m(m), "w1", 3600)  # a wait during the pause stays open
    w.actions += acts
    msg = w.agent_says(peer, "@bot three cooldowns", mentions=("bot",))
    w.store.set_paused(w.room.id, False)
    w.actions += w.engine.on_command(w.room.id, "resume")
    assert sink.open  # still held
    w.store.set_held(m.id, False)
    w.actions += w.engine.on_command(w.room.id, "release", m.id)
    assert sink.open  # still the budget
    clock.advance(30)
    w.actions += w.engine.tick()
    assert sink.open and w.delivery(m, msg)["state"] == "pending"
    w.store.set_budget(w.room.id, 1)
    w.actions += w.engine.on_command(w.room.id, "budget")
    assert ids(result(w, sink)["text"]) == [msg.id]
    assert w.delivery(m, msg)["attempts"] == 0


def test_a_rate_limited_say_loses_nothing_it_was_owed(w: World, clock: FakeClock) -> None:
    """The refused say isn't queued (the agent retries), but its unread messages
    still come back with the refusal (as a say() pull)."""
    p, m = w.agent("bot", ack="immediate")
    w.store.update_participant(p.id, last_say_at=clock.now())
    msg = w.human("unread")
    assert w.engine.check_say(w.p(p), w.m(m), None) is not None
    text, _bid, count, _more, _acts = w.engine.pull(w.p(p), w.m(m), "say", 50)
    assert count == 1 and f"id={msg.id} " in text
