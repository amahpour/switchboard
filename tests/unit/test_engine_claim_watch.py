"""A claim is also what its holder watches, in the delivery engine (DESIGN.md §8.1, §47, §52, #541).

A message that names a live claim is classified for the holder as an @mention of it: priority 1
(a person's stays 2), ``mentioned``, so ``to_you=yes``, a wake that spends the budget, and the
watchdog. Everything below runs on a ``FakeClock`` and the engine, with no broker. The matching
rule alone is ``test_claim_watch.py``; scripted agents through a broker are
``tests/integration/test_claim_watch_http.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock
from engine_agents import result, waiting, woke
from engine_world import World

from switchboard import envelope
from switchboard.config import Config
from switchboard.models import Message

W = 120.0  # the default watchdog_s


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    # An open wait() takes chatter as soon as the room has been quiet for ``quiet_s``, so the
    # quiet period is long here: only what is addressed to a member starts its turn before it.
    cfg = Config(human_name="alice").with_delivery(quiet_s=60.0, max_hold_s=300.0, unaddressed_hold_s=5.0)
    return World(tmp_path, clock, cfg)


def hold(w: World, m: Any, key: str, ttl: int = 7200) -> None:
    decision = w.store.move_claim(w.room.id, m.id, key, ttl)
    assert decision.action == "acquired", decision


def row(w: World, m: Any, msg: Message) -> dict[str, Any]:
    return w.delivery(m, msg)


def budget(w: World) -> int:
    room = w.store.room_by_id(w.room.id)
    assert room is not None
    return room.budget_remaining


def test_a_message_naming_a_live_claim_wakes_its_holder_as_an_at_mention_does(w: World) -> None:
    """claude-1 holds owner/repo#12 and waits. A peer's message that names it, with no @, wakes
    claude-1 at once with to_you=yes and prio=mention, and spends one wake; codex-1 holds nothing,
    so for it the same message is chatter that waits. Fails if the holder is left to the chatter
    hold, the line says to_you=no, no wake is counted, or the claim wakes the whole room."""
    _c, cm, claude = waiting(w, "claude-1")
    _x, xm, codex = waiting(w, "codex-1")
    _p, peer = w.agent("peer")
    hold(w, cm, "owner/repo#12")
    before = budget(w)
    msg = w.agent_says(peer, "CI is red on owner/repo#12, can someone look?")
    assert woke(w, claude) and not woke(w, codex)
    text = result(w, claude)["text"]
    assert f"id={msg.id} " in text and "to_you=yes" in text and "prio=mention" in text
    assert (row(w, cm, msg)["prio"], row(w, cm, msg)["mentioned"]) == (1, 1)
    assert (row(w, xm, msg)["prio"], row(w, xm, msg)["mentioned"]) == (0, 0)
    assert budget(w) == before - 1
    assert w.store.get_message(msg.id).mentions == []  # the message's own @mentions are not touched


def test_a_longer_id_or_another_case_of_the_same_id_is_decided_by_the_whole_token(w: World) -> None:
    """The boundary, through the engine: SYSINT-6450 and a project's !567 leave the holder of
    SYSINT-645 and !56 unaddressed, and a lower-case sysint-645 addresses it. Fails if the
    classification matches a prefix, or is case-sensitive."""
    _c, cm, claude = waiting(w, "claude-1")
    _p, peer = w.agent("peer")
    hold(w, cm, "SYSINT-645")
    hold(w, cm, "!56")
    longer = w.agent_says(peer, "SYSINT-6450 and !567 are unrelated")
    assert (row(w, cm, longer)["prio"], row(w, cm, longer)["mentioned"]) == (0, 0)
    assert not woke(w, claude)
    same = w.agent_says(peer, "what is the state of sysint-645?")
    assert (row(w, cm, same)["prio"], row(w, cm, same)["mentioned"]) == (1, 1)
    assert woke(w, claude)
    assert "id=" + str(longer.id) + " " in result(w, claude)["text"]  # the earlier line rides along
    assert "to_you=no" in result(w, claude)["text"]  # ... as chatter, which it was


def test_an_agent_is_not_addressed_by_its_own_message_and_gets_no_row_for_it(w: World) -> None:
    """Saying "taking !56" while holding !56 sends nothing back to the sayer: no delivery row for
    its own message (the same as any message of its own). Fails if a claim makes a message come
    back to its author."""
    _c, cm, claude = waiting(w, "claude-1")
    _x, xm, _codex = waiting(w, "codex-1")
    hold(w, cm, "!56")
    msg = w.agent_says(cm, "taking !56")
    assert all(d["message_id"] != msg.id for d in w.store.deliveries(cm.id))
    assert not woke(w, claude)
    assert row(w, xm, msg)["mentioned"] == 0  # codex-1 holds nothing named


def test_a_claim_watches_until_it_is_released_or_expires_and_is_not_retroactive(
    w: World, clock: FakeClock
) -> None:
    """Live means before the deadline and not released. Before the claim, after its deadline, and
    after a release, the same text is chatter; a claim made after a message does not reclassify
    it. Fails if an expired or released claim still addresses its holder, or an old message is
    caught by a new claim."""
    _c, cm, _claude = waiting(w, "claude-1")
    _p, peer = w.agent("peer")
    early = w.agent_says(peer, "!56 is red")
    hold(w, cm, "!56", ttl=60)
    assert row(w, cm, early)["mentioned"] == 0  # posted before the claim
    live = w.agent_says(peer, "!56 is still red")
    assert row(w, cm, live)["mentioned"] == 1
    clock.advance(60)  # the deadline itself is the end
    gone = w.agent_says(peer, "!56 is red again")
    assert row(w, cm, gone)["mentioned"] == 0
    hold(w, cm, "!56", ttl=7200)
    assert w.store.move_claim(w.room.id, cm.id, "!56", 0).action == "released"
    released = w.agent_says(peer, "!56 once more")
    assert row(w, cm, released)["mentioned"] == 0


def test_leaving_the_room_ends_the_claims_it_watched(w: World) -> None:
    """A member that left holds nothing and has no deliveries; a member that stays does. Fails if
    a claim outlives its membership and still classifies for it."""
    _c, cm, _claude = waiting(w, "claude-1")
    _x, xm, _codex = waiting(w, "codex-1")
    _p, peer = w.agent("peer")
    hold(w, cm, "!56")
    hold(w, xm, "!57")
    w.store.end_membership(cm.id, "leave")
    msg = w.agent_says(peer, "!56 and !57")
    assert all(d["message_id"] != msg.id for d in w.store.deliveries(cm.id))
    assert row(w, xm, msg)["mentioned"] == 1


def test_a_person_naming_a_claim_stays_priority_2_and_marks_the_holder_mentioned(w: World) -> None:
    """§8.1: a person's message is priority 2 for everyone, and an @mention of a member sets its
    ``mentioned``; a claim does the same. alice @mentions codex-1 and names claude-1's !56: both are
    ``to_you=yes``, bravo-1 (named by nothing) is ``to_you=no``, and nobody's priority changes.
    Fails if a person's message is demoted, the holder is left ``to_you=no``, or an unrelated
    member is marked."""
    _c, cm, claude = waiting(w, "claude-1")
    _x, xm, codex = waiting(w, "codex-1")
    _b, bm, bravo = waiting(w, "bravo-1")
    hold(w, cm, "!56")
    msg = w.human("@codex-1 how far along is the fix for !56?", mentions=("codex-1",))
    assert [(row(w, m, msg)["prio"], row(w, m, msg)["mentioned"]) for m in (cm, xm, bm)] == [
        (2, 1),
        (2, 1),
        (2, 0),
    ]
    assert "to_you=yes" in result(w, claude)["text"] and "to_you=yes" in result(w, codex)["text"]
    assert "to_you=no" in result(w, bravo)["text"]


def test_a_person_naming_only_a_claim_still_names_nobody(w: World) -> None:
    """The message's own ``mentions`` stays empty, so §8.8 still sees a question that names nobody
    (everyone hears it, the first answer takes it) while the holder is also marked mentioned.
    Fails if the claim is written into the message's mentions, which would turn it into a
    message that names one member and end the turn-taking for everyone else."""
    _c, cm, _claude = waiting(w, "claude-1")
    _x, xm, _codex = waiting(w, "codex-1")
    hold(w, cm, "!56")
    msg = w.human("what is the state of !56?")
    assert w.store.get_message(msg.id).mentions == []
    assert (row(w, cm, msg)["prio"], row(w, cm, msg)["mentioned"]) == (2, 1)
    assert (row(w, xm, msg)["prio"], row(w, xm, msg)["mentioned"]) == (2, 0)


def meeting(w: World, text: str) -> Message:
    msg = w.store.insert_message(
        w.room.id, sender_name="call", sender_kind="meeting", via="meeting", text=text
    )
    w.actions += w.engine.on_message(msg.id)
    return msg


def test_a_call_line_naming_a_claim_is_a_call_out_to_its_holder(w: World, clock: FakeClock) -> None:
    """§52: a line that names an agent wakes only that agent, at priority 1, and spends a wake; a
    line that names nobody never starts a turn. A claim is a name for this: the line from the call
    that says "SYSINT-645" wakes its holder, the earlier line rides along, and the hop count
    stays where it was. Fails if the holder is left asleep, the whole room wakes, the line is
    treated as a person's or as an agent's, or the wake is free."""
    _c, cm, claude = waiting(w, "claude-1")
    _x, _xm, codex = waiting(w, "codex-1")
    hold(w, cm, "SYSINT-645")
    first = meeting(w, "gas prices are in Los Angeles")
    clock.advance(1000)
    w.actions += w.engine.tick()
    assert not woke(w, claude) and not woke(w, codex)  # an aside, however long it waits
    before, hops = budget(w), w.store.room_by_id(w.room.id).hop_count
    line = meeting(w, "has anyone looked at SYSINT-645 yet")
    assert woke(w, claude) and not woke(w, codex)
    text = result(w, claude)["text"]
    assert "kind=meeting" in text and "to_you=yes" in text
    assert f"id={first.id} " in text and f"id={line.id} " in text
    assert (row(w, cm, line)["prio"], row(w, cm, line)["mentioned"]) == (1, 1)
    assert budget(w) == before - 1
    assert w.store.room_by_id(w.room.id).hop_count == hops


def test_a_system_notice_never_addresses_a_holder(w: World) -> None:
    """A room-rename notice is delivered (#316) and may carry any room name, but a notice names
    rooms and people, not work: it is not a call-out. Fails if a notice that contains a key is
    classified for the holder."""
    _c, cm, _claude = waiting(w, "claude-1")
    hold(w, cm, "release-2")
    msg = w.store.insert_message(
        w.room.id,
        sender_name="switchboard",
        sender_kind="system",
        via="system",
        kind="notice",
        text="alice renamed #build to #release-2",
        deliver_notice=True,
    )
    assert (row(w, cm, msg)["prio"], row(w, cm, msg)["mentioned"]) == (0, 0)


def test_the_watchdog_reminds_a_holder_who_does_not_answer_a_claim_mention(
    w: World, clock: FakeClock
) -> None:
    """The same watchdog as for an @mention (§8.5): after ``watchdog_s`` with no say() or pass(), the
    message comes back as a reminder, a counted wake. Fails if a claim mention is watched less
    than an @mention, or never reminded."""
    p, cm, claude = waiting(w, "claude-1")
    _pp, peer = w.agent("peer")
    hold(w, cm, "owner/repo#12")
    msg = w.agent_says(peer, "owner/repo#12 needs a rebase")
    [first] = w.resolved(claude.id)
    assert first["status"] == "messages"
    w.actions += w.engine.before_call(w.p(p))  # the agent's next call confirms the batch
    assert row(w, cm, msg)["state"] == "in_context"
    sink, acts = w.engine.open_wait(w.p(p), w.m(cm), "again", 3600)
    w.actions += acts
    budget_then = budget(w)
    clock.advance(W)
    w.actions += w.engine.tick()
    res = result(w, sink)
    assert res is not None and res["status"] == "messages"
    assert f"id={msg.id} " in res["text"] and "reminder=yes" in res["text"]
    assert envelope.REMINDER_NOTE in res["text"]
    assert row(w, cm, msg)["reminders"] == 1 and budget(w) == budget_then - 1


def test_a_quiet_member_is_woken_by_its_claim_but_not_by_chatter(w: World, clock: FakeClock) -> None:
    """§50: a quiet member's unaddressed chatter never starts a turn, and what is addressed to it
    still does. A claim mention is addressed. Fails if quiet hides a claim mention, or if the
    chatter the claim does not name wakes it."""
    _c, cm, claude = waiting(w, "claude-1")
    _p, peer = w.agent("peer")
    w.store.set_quiet(cm.id, True)
    hold(w, cm, "!56")
    w.agent_says(peer, "ordinary chatter about nothing in particular")
    clock.advance(1000)
    w.actions += w.engine.tick()
    assert not woke(w, claude)
    w.agent_says(peer, "!56 is green")
    assert woke(w, claude) and "to_you=yes" in result(w, claude)["text"]
