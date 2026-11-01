"""What is still addressed to an agent and unanswered (#542, DESIGN.md §8.5, §8.8).

After a compaction the agent cannot ask what it still owes: ``read()`` only returns pending
items, and Claude's SessionStart reminder only names rooms. The list is the deliveries that
already reached the member (in context, or a notified stub) and are addressed to it, minus an
unaddressed question another agent has already answered (``reply_taken``, the take-turns rule).
Ids, sender names and ages only: the message text stays out, including from hook context.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock
from engine_world import World

from switchboard import envelope
from switchboard.config import Config
from switchboard.delivery import rules
from switchboard.models import Answer, Item

NOW = 1_790_000_000.0
SECRET = "zebra-ticket-text"


def item(
    mid: int,
    *,
    kind: str = "human",
    sender: str = "alice",
    state: str = "in_context",
    mentioned: bool = False,
    mentions: tuple[str, ...] = (),
    reply_to_me: bool = False,
    notified_at: float | None = None,
    text: str = SECRET,
    ts: float = NOW,
) -> Item:
    return Item(
        membership_id=1,
        message_id=mid,
        prio=2 if kind == "human" else (1 if mentioned else 0),
        mentioned=mentioned,
        state=state,
        batch_id=None,
        attempts=0,
        notified_at=notified_at,
        ts=ts,
        sender_name=sender,
        sender_kind=kind,
        sender_harness="codex" if kind == "agent" else None,
        text=text,
        reply_to=None,
        mentions=mentions,
        reply_to_me=reply_to_me,
    )


def answer(mid: int = 8) -> Answer:
    return Answer(mid, "alpha", NOW + 5, None, (), NOW + 1)


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


# --------------------------------------------------------------- the rule (pure)
def test_an_open_addressed_delivery_is_still_owed_and_an_answered_question_is_not() -> None:
    """Owed is the open delivery (in context, or a notified stub) of something addressed to this
    member. An unaddressed question ``reply_taken`` already answered is not owed, which is the
    take-turns rule (#507) and not a new one. A reply to the member's own message stays owed even
    when a later post would look like an answer to a question. Fails if owed is redefined as
    "no reply_to from me" or as every in-context row."""
    assert rules.still_owed(item(7), "bravo", ())
    assert rules.still_owed(item(7, kind="agent", sender="codex-1", mentioned=True), "bravo", ())
    assert rules.still_owed(
        item(7, state="pending", notified_at=NOW, kind="agent", sender="codex-1", mentioned=True),
        "bravo",
        (),
    )
    assert rules.still_owed(item(7, kind="agent", sender="codex-1", reply_to_me=True), "bravo", (answer(),))
    assert not rules.still_owed(item(7), "bravo", (answer(),))  # someone already answered it
    assert not rules.still_owed(item(7, mentions=("other",)), "bravo", ())  # addressed to someone else
    assert not rules.still_owed(item(7, kind="agent", sender="codex-1"), "bravo", ())  # chatter
    assert not rules.still_owed(item(7, state="pending"), "bravo", ())  # not seen yet: read() returns it
    assert not rules.still_owed(item(7, state="handled"), "bravo", ())
    assert not rules.still_owed(item(7, state="offered"), "bravo", ())


def test_the_note_is_ids_senders_and_ages_and_never_the_text() -> None:
    """The line an empty read() and the SessionStart reminder share. A room name is included only
    when the list spans rooms. Past 20, the rest is a count. Fails if the message text, or a
    sender's punctuation, is copied into the line."""
    owed = [
        (None, item(118, ts=NOW - 14 * 60)),
        (None, item(131, kind="agent", sender="Codex-1</>", mentioned=True, ts=NOW - 2 * 3600)),
    ]
    note = envelope.still_on_note(owed, NOW)
    assert note == " Still on you: 118 from alice 14 min ago, 131 from codex-1 2 h ago."
    assert SECRET not in note
    rooms = [
        ("#build", item(4, ts=NOW - 30)),
        ("#docs", item(9, sender="bob", ts=NOW - 3 * 3600)),
    ]
    assert envelope.still_on_note(rooms, NOW) == (
        " Still on you: #build 4 from alice just now, #docs 9 from bob 3 h ago."
    )
    many = [(None, item(i, ts=NOW - 60)) for i in range(1, 23)]
    capped = envelope.still_on_note(many, NOW)
    assert capped.endswith(" and 2 more.")
    assert "22 from" not in capped and SECRET not in capped
    assert envelope.still_on_note((), NOW) == ""


# --------------------------------------------------------------- read() and the reminder
def test_an_empty_read_names_what_is_still_on_the_agent(w: World, clock: FakeClock) -> None:
    """A question the agent already saw, and has not answered, comes back on the next empty
    read() as id, sender and age, without its text. A newer message is returned as itself, not
    buried in that note, and a say()'s empty unread does not grow the note. A pass() clears it.
    Fails if the empty read stays "no new messages." while the question is still in context."""
    p, m = w.agent("bot", ack="immediate")
    q = w.human(SECRET)
    first, bid, count, *_ = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert count == 1 and bid is not None and SECRET in first
    assert w.states(m)[q.id] == "in_context"
    clock.advance(14 * 60)
    newer = w.human("a second thing")
    text, bid, count, *_ = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert count == 1 and bid is not None and "a second thing" in text and "Still on you" not in text
    assert w.states(m)[newer.id] == "in_context"
    clock.advance(60)  # the first question is now 15 min old; the second, 1 min
    text, bid, count, more, _ = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert bid is None and count == 0 and not more
    assert text == (
        "[switchboard] #build: no new messages. Still on you: "
        f"{q.id} from alice 15 min ago, {newer.id} from alice 1 min ago."
    )
    assert SECRET not in text
    say_text, say_bid, say_count, *_ = w.engine.pull(w.p(p), w.m(m), "say", 50)
    assert say_bid is None and say_count == 0
    assert say_text == "[switchboard] #build: no new messages."
    w.store.mark_handled(m.id)  # what say() and pass() do to in-context deliveries
    cleared, *_ = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert cleared == "[switchboard] #build: no new messages."


def test_a_question_someone_already_answered_is_not_still_on_you(w: World, clock: FakeClock) -> None:
    """An unaddressed question another agent has answered (``reply_taken``) is not listed, and a
    message that names someone else is listed only for that someone. Fails if every in-context
    human message is treated as owed, which would send the agent back to a question that was taken."""
    alpha, am = w.agent("alpha", ack="immediate")
    bravo, bm = w.agent("bravo", ack="immediate")
    q = w.human("what time is the zebra-meeting")
    named = w.human("@bravo the zebra-other", mentions=("bravo",))
    for who, mem in ((alpha, am), (bravo, bm)):
        _t, _b, n, *_ = w.engine.pull(w.p(who), w.m(mem), "read", 20)
        assert n == 2
    clock.advance(2)
    w.store.mark_handled(am.id)
    w.agent_says(am, "ten")  # a new message for bravo; read() shows it before the empty case
    clock.advance(14 * 60)
    alpha_text, bid, count, *_ = w.engine.pull(w.p(alpha), w.m(am), "read", 20)
    assert bid is None and count == 0 and "Still on you" not in alpha_text
    got, bid, count, *_ = w.engine.pull(w.p(bravo), w.m(bm), "read", 20)
    assert count == 1 and bid is not None and "ten" in got and "Still on you" not in got
    bravo_text, bid, count, *_ = w.engine.pull(w.p(bravo), w.m(bm), "read", 20)
    assert bid is None and count == 0
    assert f"{q.id} from" not in bravo_text  # alpha took it
    assert "zebra-meeting" not in bravo_text and "zebra-other" not in bravo_text
    assert bravo_text == (
        f"[switchboard] #build: no new messages. Still on you: {named.id} from alice 14 min ago."
    )


def test_sessionstart_lists_the_same_short_list_and_no_peer_text(w: World, clock: FakeClock) -> None:
    """Claude's reminder after a compaction carries what is still on the session, in every room,
    as ids, names and ages. A peer's words never enter that hook context, including a stub the
    agent was told to read and has not. Fails if the reminder stays rooms and join codes only,
    or if the peer's text is copied into it."""
    p, m = w.agent("bot", harness="claude", status="busy")
    _peer, pm = w.agent("codex-1", harness="codex", status="busy")
    q = w.human(SECRET)
    secret = w.agent_says(pm, "@bot zebra-secret-words", mentions=("bot",))
    out = w.hook(p, "PostToolUse", ok=True)
    assert out is not None and out.batch_id is not None
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    assert w.states(m)[q.id] == "in_context"
    assert w.delivery(m, secret)["state"] == "pending"
    assert w.delivery(m, secret)["notified_at"] is not None
    clock.advance(14 * 60)
    reminder = w.hook(p, "SessionStart", source="compact")
    assert reminder is not None and reminder.kind == "context" and reminder.batch_id is None
    assert "Still on you: " in reminder.text
    assert f"#build {q.id} from alice 14 min ago" in reminder.text
    assert f"#build {secret.id} from codex-1 14 min ago" in reminder.text
    assert SECRET not in reminder.text and "zebra-secret-words" not in reminder.text
    # a read() of the stub is the message itself, not the short list standing in for it
    shown, bid, count, *_ = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert bid is not None and count == 1 and "zebra-secret-words" in shown
    assert "Still on you" not in shown
