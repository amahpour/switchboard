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
from engine_agents import fills, session, stop
from engine_agents import result as sink_result
from engine_world import World

from switchboard.config import Config


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0, unaddressed_hold_s=0.0))


def ids(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"^- id=(\d+) ", text, re.M)]


def result(w: World, sink: Any) -> dict[str, Any] | None:
    s = w.engine.sinks.get(sink.id)
    return None if s is None or s.open else s.result


@pytest.mark.parametrize("harness", ["claude", "codex", "cursor"])
def test_busy_to_idle_rechecks_pending_at_the_stop_hook(w: World, harness: str) -> None:
    """The chatter that came while the agent worked goes out when its turn ends: its Stop hook parks
    and the park is answered at once. Fails if the busy to idle edge doesn't look at what is pending."""
    p, m = session(w, f"{harness}-1", harness, status="busy")
    _pp, peer = w.agent("peer")
    chat = w.agent_says(peer, "chatter while busy")  # not deliverable mid-task
    assert not fills(w)
    out = stop(w, p)  # busy -> idle
    assert ids(sink_result(w, out)["text"]) == [chat.id]


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


def test_an_unacknowledged_followup_is_offered_again_at_the_next_stop(w: World, clock: FakeClock) -> None:
    """A follow-up the hook never acknowledged (it died, or the harness dropped it) goes back to
    pending, and the session's next Stop gets it again; this time it runs, and the next hook of the
    turn it starts confirms it. Fails if the message is lost with the first follow-up."""
    p, m = session(w, status="busy")
    msg = w.human("did you get this?")
    first = stop(w, p)
    res = sink_result(w, first)
    assert f"id={msg.id} " in res["text"]
    clock.advance(w.cfg.delivery.hook_ack_s + 0.1)
    w.actions += w.engine.tick()
    assert w.store.get_batch(res["batch_id"]).expire_reason == "no_ack"
    assert w.delivery(m, msg)["state"] == "pending" and w.delivery(m, msg)["attempts"] == 1
    again = sink_result(w, stop(w, p))
    assert f"id={msg.id} " in again["text"] and again["batch_id"] != res["batch_id"]
    w.engine.on_hook_ack(again["batch_id"], again["ack"])
    w.hook(p, "UserPromptSubmit", gen="g1")
    assert w.delivery(m, msg)["state"] == "in_context"


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
