"""What is still addressed to an agent and unanswered (#542, DESIGN.md §8.5, §8.8).

After a compaction the agent cannot ask what it still owes, and the agent that asked cannot
tell whether the addressee has handled it. ``read()`` returns pending items and then both
lists: what reached this member and is still addressed to it, and the caller's own messages
an addressee has not handled. Claude's SessionStart reminder carries both. Ids, names and
ages only: the message text stays out, including from hook context.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock
from engine_agents import result, session, stop
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


def test_waiting_on_is_an_open_mention_or_reply_and_not_a_question_to_everyone() -> None:
    """The asker's side uses the addressee's delivery (#542). Open is the same rule as
    ``still_owed``: it reached that member and they have not handled it. Only an @mention
    of them, or a reply to their own message, counts. A person's message that names nobody
    is owed by everyone and is not waiting on one member. Fails if waiting is redefined as
    every unhandled row, or as a question to the room."""
    mention = item(7, kind="agent", sender="alpha", mentioned=True)
    reply = item(8, kind="agent", sender="alpha", reply_to_me=True)
    assert rules.waiting_on(mention, "bravo", ())
    assert rules.waiting_on(reply, "bravo", ())
    assert rules.waiting_on(
        item(7, kind="agent", sender="alpha", mentioned=True, state="pending", notified_at=NOW),
        "bravo",
        (),
    )
    # a person's reply that names nobody is also an unaddressed question: someone else
    # answering it is the take-turns rule, so the addressee no longer owes it
    human_reply = item(7, reply_to_me=True)
    assert rules.waiting_on(human_reply, "bravo", ())
    assert not rules.waiting_on(human_reply, "bravo", (answer(),))
    assert not rules.waiting_on(item(7), "bravo", ())  # names nobody: not addressed to one member
    assert not rules.waiting_on(item(7, kind="agent", sender="alpha"), "bravo", ())  # chatter
    assert not rules.waiting_on(
        item(7, kind="agent", sender="alpha", mentioned=True, state="pending"), "bravo", ()
    )  # not seen yet
    assert not rules.waiting_on(
        item(7, kind="agent", sender="alpha", mentioned=True, state="handled"), "bravo", ()
    )
    assert not rules.waiting_on(
        item(7, kind="agent", sender="alpha", mentioned=True, state="offered"), "bravo", ()
    )


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


def test_the_waiting_note_is_ids_addressees_and_ages_and_never_the_text() -> None:
    """The line a read() and the SessionStart reminder share for what the caller is
    waiting on. One entry per addressee, so one message to two members appears twice.
    A room name is included only when the list spans rooms. Past 20, the rest is a
    count. Fails if the message text, or a name's punctuation, is copied into the line."""
    ask = item(118, kind="agent", sender="alpha", mentioned=True, ts=NOW - 14 * 60)
    note = envelope.waiting_on_note(
        [(None, ask, "bravo"), (None, ask, "Charlie</>")],
        NOW,
    )
    assert note == " Waiting on others: 118 for bravo 14 min ago, 118 for charlie 14 min ago."
    assert SECRET not in note
    rooms = [
        ("#build", item(4, kind="agent", sender="alpha", ts=NOW - 30), "bravo"),
        ("#docs", item(9, kind="agent", sender="alpha", ts=NOW - 3 * 3600), "bob"),
    ]
    assert envelope.waiting_on_note(rooms, NOW) == (
        " Waiting on others: #build 4 for bravo just now, #docs 9 for bob 3 h ago."
    )
    many = [(None, item(i, kind="agent", sender="alpha", ts=NOW - 60), f"m{i}") for i in range(1, 23)]
    capped = envelope.waiting_on_note(many, NOW)
    assert capped.endswith(" and 2 more.")
    assert "22 for" not in capped and SECRET not in capped
    assert envelope.waiting_on_note((), NOW) == ""


# --------------------------------------------------------------- read() and the reminder
def test_an_empty_read_names_what_is_still_on_the_agent(w: World, clock: FakeClock) -> None:
    """A question the agent already saw, and has not answered, comes back on the next read()
    as id, sender and age, without its text. A newer message is returned as itself, and the
    earlier question is named after it, not in place of it. A say()'s empty unread does not
    grow the note. A pass() clears it. Fails if the empty read stays "no new messages." while
    the question is still in context, or if the new message's id is repeated in the note."""
    p, m = w.agent("bot", ack="immediate")
    q = w.human(SECRET)
    first, bid, count, *_ = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert count == 1 and bid is not None and SECRET in first
    assert w.states(m)[q.id] == "in_context"
    clock.advance(14 * 60)
    newer = w.human("a second thing")
    text, bid, count, *_ = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert count == 1 and bid is not None and "a second thing" in text
    assert text.splitlines()[-1] == f"Still on you: {q.id} from alice 14 min ago."
    assert SECRET not in text.splitlines()[-1] and str(newer.id) not in text.splitlines()[-1]
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
    assert count == 1 and bid is not None and "ten" in got
    assert got.splitlines()[-1] == f"Still on you: {named.id} from alice 14 min ago."
    assert "zebra-other" not in got.splitlines()[-1]
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
    # a read() of the stub is the message itself. The question already in context is the
    # short list after it; the stub's id is not, because this read just returned it.
    shown, bid, count, *_ = w.engine.pull(w.p(p), w.m(m), "read", 20)
    assert bid is not None and count == 1 and "zebra-secret-words" in shown
    assert shown.splitlines()[-1] == f"Still on you: {q.id} from alice 14 min ago."
    assert str(secret.id) not in shown.splitlines()[-1]
    assert SECRET not in shown.splitlines()[-1] and "zebra-secret-words" not in shown.splitlines()[-1]


def test_a_read_with_new_messages_ends_with_both_lists_and_skips_what_it_just_returned(
    w: World, clock: FakeClock
) -> None:
    """A read() that returns a new message still ends with what is owed and what this
    member is waiting on. The ids just returned are not repeated. A message addressed
    to two members is one entry each, and it is absent until it has reached them.
    Unaddressed chatter is not waiting. say()/pass() on one addressee drops only that
    entry. Fails if the notes stay only on an empty read, or if a handled addressee
    is still named."""
    alpha, am = w.agent("alpha", ack="immediate")
    bravo, bm = w.agent("bravo", ack="immediate")
    _charlie, cm = w.agent("charlie", ack="immediate")
    owed = w.human(SECRET)
    first, _bid, count, *_ = w.engine.pull(w.p(alpha), w.m(am), "read", 20)
    assert count == 1 and SECRET in first and "Still on you" not in first
    ask = w.agent_says(am, "@bravo @charlie zebra-ask-text", mentions=("bravo", "charlie"))
    chatter = w.agent_says(am, "zebra-chatter-text")
    early, _bid, early_n, *_ = w.engine.pull(w.p(alpha), w.m(am), "read", 20)
    assert early_n == 0 and "Waiting on others" not in early  # not reached yet
    for who, mem in ((bravo, bm), (_charlie, cm)):
        _t, _b, n, *_ = w.engine.pull(w.p(who), w.m(mem), "read", 20)
        assert n >= 1
    clock.advance(14 * 60)
    newer = w.human("a second thing")
    text, _bid, count, *_ = w.engine.pull(w.p(alpha), w.m(am), "read", 20)
    assert count == 1 and "a second thing" in text
    last = text.splitlines()[-1]
    assert last == (
        f"Still on you: {owed.id} from alice 14 min ago. "
        f"Waiting on others: {ask.id} for bravo 14 min ago, {ask.id} for charlie 14 min ago."
    )
    assert f"{chatter.id} for" not in last and f"{newer.id} from" not in last
    assert SECRET not in last and "zebra-ask-text" not in last and "zebra-chatter-text" not in last
    w.store.mark_handled(bm.id)  # what say() and pass() do to in-context deliveries
    one_left, *_ = w.engine.pull(w.p(alpha), w.m(am), "read", 20)
    assert one_left == (
        "[switchboard] #build: no new messages. "
        f"Still on you: {owed.id} from alice 14 min ago, {newer.id} from alice just now. "
        f"Waiting on others: {ask.id} for charlie 14 min ago."
    )
    w.store.mark_handled(cm.id)
    cleared, *_ = w.engine.pull(w.p(alpha), w.m(am), "read", 20)
    assert "Waiting on others" not in cleared and f"{ask.id} for" not in cleared
    assert f"{owed.id} from alice" in cleared and f"{newer.id} from alice" in cleared


def test_a_reply_shown_as_a_stub_is_waiting_until_the_addressee_reads_it(w: World) -> None:
    """A reply to a member's own message, with no @mention, is waiting while that
    member was told about it and has not read it. Reading it handles the delivery
    (a reply is chatter, confirmed straight to handled), so the line drops. The
    reply's text stays out of the asker's line. Fails if only an @mention counts,
    or if the line survives the addressee's read."""
    asked, am = session(w, "claude-1", "claude", status="busy")
    asker, qm = w.agent("alpha", ack="immediate")
    own = w.agent_says(am, "I started the parser")
    reply = w.agent_says(qm, "zebra-reply-text", reply_to=own.id)
    res = result(w, stop(w, asked))
    assert res is not None and res["status"] == "messages" and "zebra-reply-text" not in res["text"]
    w.actions += w.engine.on_hook_ack(res["batch_id"], res["ack"])
    w.hook(asked, "PostToolUse", ok=True, tool="Bash")
    assert w.delivery(am, reply)["state"] == "pending"
    assert w.delivery(am, reply)["notified_at"] is not None
    text, _bid, count, *_ = w.engine.pull(w.p(asker), w.m(qm), "read", 20)
    assert count >= 1 and f"id={own.id} " in text
    assert f"{reply.id} for claude-1" in text and "zebra-reply-text" not in text
    shown, rbid, n, *_ = w.engine.pull(w.p(asked), w.m(am), "read", 20)
    assert n >= 1 and rbid is not None and "zebra-reply-text" in shown
    w.actions += w.engine.on_confirm(rbid, "hook:PostToolUse")
    assert w.states(am)[reply.id] == "handled"
    again, *_ = w.engine.pull(w.p(asker), w.m(qm), "read", 20)
    assert f"{reply.id} for" not in again


def test_sessionstart_also_names_what_the_session_is_waiting_on(w: World, clock: FakeClock) -> None:
    """Claude's reminder after a compaction carries both lists, in every room, as
    ids, names and ages. Neither list copies a message's text into hook context.
    Fails if the reminder names only what the session owes, or if the ask's words
    are copied in."""
    asked, am = w.agent("bot", harness="claude", status="busy")
    _peer, pm = w.agent("codex-1", harness="codex", status="busy")
    q = w.human(SECRET)
    ask = w.agent_says(am, "@codex-1 zebra-ask-text", mentions=("codex-1",))
    out = w.hook(asked, "PostToolUse", ok=True)
    assert out is not None and out.batch_id is not None
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    peer_out = w.hook(_peer, "PostToolUse", ok=True)
    assert peer_out is not None and peer_out.batch_id is not None
    w.actions += w.engine.on_hook_ack(peer_out.batch_id, peer_out.ack)
    assert w.delivery(pm, ask)["state"] == "pending" and w.delivery(pm, ask)["notified_at"] is not None
    clock.advance(14 * 60)
    reminder = w.hook(asked, "SessionStart", source="compact")
    assert reminder is not None and reminder.kind == "context"
    assert f"#build {q.id} from alice 14 min ago" in reminder.text
    assert f"#build {ask.id} for codex-1 14 min ago" in reminder.text
    assert SECRET not in reminder.text and "zebra-ask-text" not in reminder.text
