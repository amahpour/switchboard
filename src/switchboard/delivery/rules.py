"""Pure delivery-policy functions (DESIGN.md §8).

Everything here is a function of its arguments: no store, no clock, no I/O,
so every rule is unit-tested deterministically. The engine gathers the
inputs, calls these, and acts on the verdicts.

M2 shipped classification helpers, release (quiet period, max hold, batch
cap, human first, one peer batch per turn boundary, busy = priority only),
budget, rate limit and the loop guard. M3 adds requeue (re-deliver once);
M6 the watchdog (remind about an unanswered @mention, then tell the human)
and the parked escalation. The read-first rule for pass() (§24) is here too.
Issue #111 adds broadcast mentions (``@here``, ``@everyone``): who a person's
broadcast reaches, not whether it may (the caller's job). Issue #138 adds
``@humans``, the opposite direction: it addresses every person, never an
agent, and (unlike ``@here``/``@everyone``) works the same from a person's
message or an agent's own ``say()``. Issue #507 adds taking turns on a person's message that
names nobody: which says are a second answer to it, and which post was the first (§8.8).
Issue #542 adds which of a member's open deliveries are still owed, and which of
its own messages an addressee has not handled: the same open states, rendered as
ids, names and ages. Issue #548 adds ``addressed_only``: a quiet membership, for
which unaddressed chatter never starts a turn (§50). Issue #543 adds whether a SessionStart
reminder carries the standing guidance in full or a one-line pointer (§8.6).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from switchboard import envelope
from switchboard.models import Answer, Item, Release, Room

MENTION_RE = re.compile(r"(?<![\w@])@([a-z][a-z0-9_-]{0,23})(?![a-z0-9_-])", re.IGNORECASE)


def since_id(value: object, last_id: int) -> int:
    """Check an opt-in say cursor before it can reach SQLite; negative ids mean the start."""
    if type(value) is not int:
        raise ValueError("since must be a message id")
    cursor = min(max(value, 0), 2**63 - 1)
    if cursor > last_id:
        raise ValueError("since is newer than the room's last message")
    return cursor


# Header, token line and footer of a batch, for the longest room and human
# names (the envelope's fit_batch checks the real rendering; this only makes
# the release's estimate conservative).
FRAME_RESERVE = envelope.frame_reserve("#" + "x" * 39, "y" * 40, 99)


def parse_mentions(text: str, active_names: Iterable[str]) -> list[str]:
    """Lower-cased active screen names @mentioned in ``text``. There is no @all (see
    ``parse_broadcast`` below for ``@here``/``@everyone``, which are not screen names)."""
    names = {n.lower() for n in active_names}
    found = []
    for m in MENTION_RE.finditer(text or ""):
        n = m.group(1).lower()
        if n in names and n not in found:
            found.append(n)
    return found


# Broadcast mentions (DESIGN.md §8.1, issue #111): a safe start lets only a person use them
# (checked by the caller, broker/service.py human_say -- an agent's @here/@everyone is plain
# text, ROOM_RULES rule 6). "all" and "channel" are reserved screen names too (RESERVED_NAMES)
# but match nothing here, same as @all always has.
BROADCAST_NAMES = ("everyone", "here")


def parse_broadcast(text: str) -> str | None:
    """'everyone' or 'here' when ``@everyone``/``@here`` appears in ``text`` (the same
    ``@word`` shape as ``MENTION_RE``, so a mid-word ``foo@here`` doesn't count). 'everyone'
    wins when both appear, since it is the wider reach. None otherwise."""
    found = {m.group(1).lower() for m in MENTION_RE.finditer(text or "")}
    for name in BROADCAST_NAMES:
        if name in found:
            return name
    return None


def broadcast_targets(kind: str, members: Iterable[tuple[str, str]]) -> list[str]:
    """Screen names a broadcast of ``kind`` ('everyone' or 'here') reaches (DESIGN.md §8.1):
    'everyone' is every member passed in; 'here' only those whose status isn't 'offline' (the
    same presence ``who()`` shows). ``members``: (screen_name, status) of every active
    membership in the room. Expanding ``mentions`` with these names is the whole mechanism: a
    human message is already prio=2 for everyone regardless of ``mentions`` (``classify``,
    above), so a broadcast changes no delivery an ``@name`` mention wouldn't already make --
    only which members' ``mentioned`` flag is set (the mention style, the room badges, and the
    watchdog, §8.5)."""
    if kind == "everyone":
        return [n for n, _ in members]
    if kind == "here":
        return [n for n, s in members if s != "offline"]
    return []


# @humans (DESIGN.md §8.1, issue #138): the opposite direction from @here/@everyone above --
# it addresses every *person* in the room, never an agent -- and, unlike them, it works the
# same whether a person or an agent typed it (both callers, human_say and agents.py's say(),
# check this). "humans" is reserved (RESERVED_NAMES) so it can never be a real agent's screen
# name: adding the literal word to a message's own ``mentions`` (what both callers do) can
# therefore never match a membership's screen_name in store.insert_message's per-recipient
# check, so it never sets any agent's ``mentioned``, raises its prio, or wakes it -- the web UI
# (app.js's addressesHumans) is the only thing that ever reads it back off ``mentions``.
HUMANS_MENTION = "humans"


def mentions_humans(text: str) -> bool:
    """True when ``@humans`` appears in ``text`` (the same ``@word`` shape as ``MENTION_RE``,
    so a mid-word ``foo@humans.example`` doesn't count)."""
    return any(m.group(1).lower() == HUMANS_MENTION for m in MENTION_RE.finditer(text or ""))


def classify(sender_kind: str, recipient: str, mentions: Iterable[str]) -> tuple[int, bool]:
    """(prio, mentioned) for one recipient (DESIGN.md §8.1): 2 human, 1 @mention, 0 chatter.

    ``Store.insert_message`` classifies inline and also counts a recipient whose live claim the
    text names as @mentioned (#541, ``claims.watchers``); this is the @mention part alone."""
    mentioned = recipient.lower() in {m.lower() for m in mentions}
    if sender_kind == "human":
        return 2, mentioned
    return (1 if mentioned else 0), mentioned


def effective_status(status: str, has_open_sink: bool) -> str:
    """An open wait()/park sink counts as idle; approval holds and offline always win."""
    if status in ("waiting-approval", "offline"):
        return status
    return "idle" if has_open_sink else status


def is_idle(eff: str) -> bool:
    return eff in ("idle", "starting")


def human_first(items: Iterable[Item]) -> list[Item]:
    """Human items first, then mentions, then replies to the member's own messages, then the
    rest of the chatter; each group by message id. A reply releases chatter at once under the
    hold (#137), so a batch cap must keep it, not the older chatter it brought along."""
    return sorted(items, key=lambda i: (-i.prio, not i.reply_to_me, i.message_id))


def item_cost(item: Item, limit: int | None = envelope.ITEM_LIMIT, *, peers_stubbed: bool = False) -> int:
    """Characters the item's rendered envelope line takes.

    Inline, escaping can make text up to six times longer than it was typed.
    ``peers_stubbed`` (a Stop-path batch, #548): untrusted text (an agent's, a call's,
    or a kind this code doesn't know) is the stub line that batch actually sends.
    A person's message is never stubbed, so it keeps its inline cost either way.
    """
    if peers_stubbed and envelope.untrusted_sender(item.sender_kind):
        return len(envelope.render_item(item, "", "", inline=False)) + 1
    shown_limit = envelope.delivery_limit(item, "", limit)
    return len(envelope.render_item(item, "", "", inline=True, limit=shown_limit)) + 1


def cap(items: Sequence[Item], max_msgs: int, max_chars: int, *, peers_stubbed: bool = False) -> list[Item]:
    """Keep a prefix of at most ``max_msgs`` items whose rendered batch fits
    ``max_chars`` (header and footer included).

    Always keeps at least one item (the envelope cuts a single oversized item
    to fit, see ``envelope.fit_batch``), so a huge message can never block
    the queue. ``peers_stubbed`` charges agent items their stub line (#548).
    """
    out: list[Item] = []
    used = FRAME_RESERVE
    for it in items:
        if len(out) >= max(1, max_msgs):
            break
        c = item_cost(it, peers_stubbed=peers_stubbed)
        if out and used + c > max_chars:
            break
        out.append(it)
        used += c
    return out


def wake_reason(items: Iterable[Item]) -> str:
    """What drove a release, for the report: a new human message first, then a
    watchdog reminder, a new @mention, else chatter."""
    items = list(items)
    if any(i.prio == 2 and not i.reminded for i in items):
        return "human"
    if any(i.prio >= 1 and i.reminded for i in items):
        return "reminder"
    if any(i.prio == 1 for i in items):
        return "mention"
    return "chatter"


def peer_ok(peer_batch_boundary: int, boundary_seq: int) -> bool:
    """At most one peer (chatter) batch per turn boundary."""
    return peer_batch_boundary < boundary_seq


_CLOSERS = "\"'\u201d\u2019)]}\u00bb"


def asks(text: str) -> bool:
    """True when a call line ends in a question mark, once trailing whitespace and closing quotes or
    brackets are trimmed (§52.1). Only the end counts: "Is it done? I think so." does not ask.

    The caller has already limited this to call lines, which are people's speech: the bot's own
    spoken line never comes back as one (``role`` is checked at ingest, §52)."""
    return text.rstrip().rstrip(_CLOSERS).rstrip().endswith("?")


def chatter_due(
    room: Room,
    chatter: Sequence[Item],
    now: float,
    quiet_s: float,
    max_hold_s: float,
    hold_s: float = 0.0,
    *,
    addressed_only: bool = False,
    listening_call: int | None = None,
    listen_quiet_s: float = 15.0,
    listen_hold_s: float = 40.0,
    listen_ask_s: float = 3.0,
) -> bool:
    """Chatter goes out once the room has been quiet for ``quiet_s``, or the oldest
    item has waited ``max_hold_s``.

    With ``hold_s`` set (issue #137), only chatter that addresses the member (a reply to its
    own message) goes by that rule. The rest waits until its oldest item has waited ``hold_s``:
    agents' chatter collects into one batch instead of a turn per message, and the member
    still gets it sooner at any wake it has for something else.

    ``addressed_only`` (a quiet membership, #548, §50) is stricter than the hold: unaddressed
    chatter never becomes due, including when ``hold_s`` is 0 (an open ``wait()``). A reply
    still does, and the caller sends the other chatter along with it.

    Only lines from ``listening_call`` can independently become due at the listening
    deadlines (§52.1), even for a quiet member. Other call chatter only rides a real wake."""
    call = [
        c
        for c in chatter
        if listening_call is not None and c.sender_kind == "meeting" and c.meeting_id == listening_call
    ]
    if call:
        quiet = room.last_msg_at is None or now - room.last_msg_at >= listen_quiet_s
        if quiet or now - min(c.ts for c in call) >= listen_hold_s:
            return True
        # a person's question is answered after a short quiet instead (§52.1): the same clock, a
        # shorter wait, and the same budget and cap
        if any(asks(c.text) for c in call):
            if room.last_msg_at is None or now - room.last_msg_at >= listen_ask_s:
                return True
    chatter = [c for c in chatter if not _meeting_aside(c)]
    if not chatter:
        return False
    if hold_s > 0 or addressed_only:
        addressed = [c for c in chatter if c.reply_to_me]
    else:
        addressed = list(chatter)
    if addressed:
        quiet = room.last_msg_at is None or now - room.last_msg_at >= quiet_s
        if quiet or now - addressed[0].ts >= max_hold_s:
            return True
    if addressed_only:
        return False
    return hold_s > 0 and any(now - c.ts >= hold_s for c in chatter if not c.reply_to_me)


def chatter_wakes_at(
    pending: Sequence[Item],
    hold_s: float,
    *,
    addressed_only: bool = False,
    listening_call: int | None = None,
    room: Room | None = None,
    listen_quiet_s: float = 15.0,
    listen_hold_s: float = 40.0,
    listen_ask_s: float = 3.0,
) -> float | None:
    """Next ordinary chatter hold or listening deadline, using the same clocks as chatter_due."""
    eligible = [d for d in pending if d.notified_at is None]
    if any(d.prio >= 1 or d.reply_to_me for d in eligible):
        return None
    deadlines: list[float] = []
    call = [
        d
        for d in eligible
        if listening_call is not None and d.sender_kind == "meeting" and d.meeting_id == listening_call
    ]
    if call and room is not None:
        last = room.last_msg_at if room.last_msg_at is not None else min(d.ts for d in call)
        quiet_for = listen_ask_s if any(asks(d.text) for d in call) else listen_quiet_s
        deadlines.append(min(last + quiet_for, min(d.ts for d in call) + listen_hold_s))
    chatter = [d for d in eligible if not _meeting_aside(d)]
    if not addressed_only and hold_s > 0 and chatter:
        deadlines.append(min(d.ts for d in chatter) + hold_s)
    return min(deadlines) if deadlines else None


# Agent messages in a row, in a room with a live call, with no person speaking on the call or writing in
# the room, after which agents wake only for a person (#713, §52.2). Tighter than ``hop_limit``, which a
# call line does not reset.
VOICE_LOOP_LINES = 8


def voice_loop_run(senders: Sequence[str], limit: int = VOICE_LOOP_LINES) -> int:
    """How many agent messages are in a row at the end of a room's chat (#713), counted up to ``limit``.

    ``senders`` are the ``sender_kind`` of the room's chat messages, newest first. Only a person's message
    (``human``) or a participant's line from the call (``meeting``) ends the run: a kind this code doesn't
    know neither ends it nor counts, so it can't be used to reset the count."""
    run = 0
    for kind in senders:
        if kind == "agent":
            run += 1
            if run >= limit:
                break
        elif kind in ("human", "meeting"):
            break
    return run


def voice_loop_tripped(senders: Sequence[str], call_live: bool, limit: int = VOICE_LOOP_LINES) -> bool:
    """True when a room with a live call has ``limit`` agent messages in a row (#713)."""
    return call_live and limit > 0 and voice_loop_run(senders, limit) >= limit


def releasable(
    pending: Sequence[Item],
    *,
    room: Room,
    eff: str,
    peer_batch_boundary: int,
    boundary_seq: int,
    now: float,
    quiet_s: float,
    max_hold_s: float,
    batch_max_msgs: int,
    max_chars: int,
    hold_s: float = 0.0,
    wake_now: bool = False,
    peers_stubbed: bool = False,
    addressed_only: bool = False,
    listening_call: int | None = None,
    listen_quiet_s: float = 15.0,
    listen_hold_s: float = 40.0,
    listen_ask_s: float = 3.0,
    voice_loop: bool = False,
) -> Release | None:
    """What may go out to one member now (DESIGN.md §8.2), or None.

    - Stubs that were already notified are pull-only: they never wake again.
    - Busy (mid-task): priority items only, never chatter, not counted.
    - Idle: priority items wake at once (humans even at budget 0), plus
      chatter if this turn boundary hasn't had a peer batch yet; chatter
      alone waits for the quiet period (or the max hold, or ``hold_s`` when
      it doesn't address the member) and needs budget.
    - ``wake_now``: a person asked for this member's chatter now (``/wake``, #137): like
      their own message, it skips the hold, the budget and the one-peer-batch rule.
    - ``peers_stubbed``: this batch will show agent text as stubs (a Stop path, #548),
      so the cap charges the stub line rather than the inline one.
    - ``addressed_only``: a quiet member (#548, §50). Unaddressed chatter never starts
      the turn. A person's message, an @mention, a reply and ``wake_now`` still do,
      and they take that chatter along. Busy stays priority only: chatter is not added.
    - ``voice_loop``: the room has a live call and ``VOICE_LOOP_LINES`` agent messages in a row (#713).
      Only a person's message (priority 2) is released, to a busy or an idle member, and chatter is
      released only with it or on ``wake_now``: the budget, whatever is left of it, is not the gate.
    """
    eligible = [d for d in pending if d.notified_at is None]
    prio = [d for d in eligible if d.prio >= 1]
    if voice_loop:
        prio = [d for d in prio if d.prio == 2]
    chatter = [d for d in eligible if d.prio == 0]
    ok = peer_ok(peer_batch_boundary, boundary_seq)

    def done(items: list[Item], kind: str, counted: bool, *, listen_wake: bool = False) -> Release:
        kept = cap(human_first(items), batch_max_msgs, max_chars, peers_stubbed=peers_stubbed)
        return Release(
            items=tuple(kept), kind=kind, counted=counted, reason=wake_reason(kept), listen_wake=listen_wake
        )

    if eff == "busy":
        return done(prio, "priority", False) if prio else None
    if not is_idle(eff):
        return None
    if prio and (any(d.prio == 2 for d in prio) or room.budget_remaining > 0):
        return done(prio + (chatter if ok or wake_now else []), "wake", True)
    if chatter and wake_now:
        return done(chatter, "wake", True)
    if chatter and ok and room.budget_remaining > 0 and not voice_loop:
        ordinary_due = chatter_due(
            room, chatter, now, quiet_s, max_hold_s, hold_s, addressed_only=addressed_only
        )
        if ordinary_due:
            return done(chatter, "wake", True)
        if chatter_due(
            room,
            chatter,
            now,
            quiet_s,
            max_hold_s,
            hold_s,
            addressed_only=addressed_only,
            listening_call=listening_call,
            listen_quiet_s=listen_quiet_s,
            listen_hold_s=listen_hold_s,
            listen_ask_s=listen_ask_s,
        ):
            return done(chatter, "wake", True, listen_wake=True)
    return None


def budget_blocked(
    pending: Sequence[Item],
    *,
    room: Room,
    eff: str,
    peer_batch_boundary: int,
    boundary_seq: int,
    addressed_only: bool = False,
    listening_call: int | None = None,
) -> bool:
    """True when an idle member has wake-eligible non-human items that only the
    empty budget holds back (drives the once-per-window budget_exhausted notice).

    Unaddressed chatter a quiet member is holding (#548) does not count: the budget
    is not what keeps it. A reply still would, once the quiet period let it go."""
    if not is_idle(eff) or room.budget_remaining > 0:
        return False
    eligible = [d for d in pending if d.notified_at is None]
    if any(d.prio == 2 for d in eligible):
        return False
    if any(d.prio == 1 for d in eligible):
        return True
    chatter = [
        d
        for d in eligible
        if d.prio == 0
        and (
            (listening_call is not None and d.sender_kind == "meeting" and d.meeting_id == listening_call)
            or (not _meeting_aside(d) and (d.reply_to_me or not addressed_only))
        )
    ]
    return bool(chatter) and peer_ok(peer_batch_boundary, boundary_seq)


def pull_items(pending: Sequence[Item], limit: int) -> tuple[list[Item], bool]:
    """read()/say()/wait() content: every pending item, notified stubs included,
    oldest first, up to ``limit``. Returns (items, more)."""
    ordered = sorted(pending, key=lambda i: i.message_id)
    return ordered[:limit], len(ordered) > limit


def check_rate_limit(
    last_say_at: float | None, now: float, rate_limit_s: float, exempt: bool
) -> float | None:
    """None if the say may go ahead, else seconds until it may (DESIGN.md §8.4)."""
    if exempt or last_say_at is None or rate_limit_s <= 0:
        return None
    left = rate_limit_s - (now - last_say_at)
    return round(left, 1) if left > 0 else None


def rate_limit_exempt(
    *, reply_to_kind: str | None, reply_to_mentions: Iterable[str], sender_name: str, unhandled_priority: int
) -> bool:
    """Replying to the human, or to a message that mentions the sender, or while
    priority items are in context but unanswered, is never rate-limited."""
    if reply_to_kind == "human":
        return True
    if sender_name.lower() in {m.lower() for m in reply_to_mentions}:
        return True
    return unhandled_priority > 0


# ------------------------------------------------- take turns on an unaddressed message (§8.8)
def is_unaddressed_question(item: Item) -> bool:
    """A person's message that names nobody (no @mention, no ``@here``/``@everyone``, which name
    everyone): every agent in the room hears it as ``to_you=yes``, and each is free to answer."""
    return item.sender_kind == "human" and not item.mentions


def questions_answered(in_context: Sequence[Item], reply_to: int | None) -> list[Item]:
    """The unaddressed questions a say answers, of the items that reached the sayer's context.

    - A say that replies to one of them answers that one; a reply to anything else (a peer's message,
      a question that named someone) answers none of them.
    - A say that replies to nothing answers all of them, unless something addressed to the sayer is
      waiting too (an @mention of it, a reply to its own message): that is more likely what it
      answers, and a say is only ever held when it can be nothing but a second answer.
    """
    questions = [i for i in in_context if is_unaddressed_question(i)]
    if reply_to is not None:
        return [q for q in questions if q.message_id == reply_to]
    if any(i.mentioned or i.reply_to_me for i in in_context):
        return []
    return questions


def reply_taken(question: Item, sayer: str, answers: Sequence[Answer]) -> Answer | None:
    """The first answer another agent gave to ``question`` (§8.8), or None: the reply is free.

    An answer is a chat post after the question that replies to it or to nothing, whose author had
    the question in context before posting. Not one that @mentions the sayer: that is addressed to
    it, a hand-off, not a reply to defer to. ``answers`` are the other agents' posts after the
    question (the store's), in any order."""
    me = sayer.lower()
    for a in sorted(answers, key=lambda a: a.message_id):
        if a.message_id <= question.message_id or a.reply_to not in (None, question.message_id):
            continue
        if a.seen_at is None or a.seen_at > a.ts or me in {n.lower() for n in a.mentions}:
            continue
        return a
    return None


def still_owed(item: Item, sayer: str, answers: Sequence[Answer] = ()) -> bool:
    """True when this delivery is still on ``sayer`` (#542, DESIGN.md §8.5, §8.8).

    It reached the member and was not handled: ``in_context``, or a notified stub
    (``pending`` with ``notified_at``). It is addressed to the member: ``to_you``
    (an @mention, or a person's message that names nobody) or a reply to the member's
    own message. An unaddressed question another agent already answered
    (``reply_taken``) is not still owed: that is the take-turns rule, not a new one.
    Anything else addressed stays owed until ``say()`` or ``pass()`` handles the
    delivery. ``answers`` are that question's, from the store; they matter only for
    an unaddressed question.
    """
    reached = item.state == "in_context" or (item.state == "pending" and item.notified_at is not None)
    if not reached or not (envelope.to_you(item, sayer) or item.reply_to_me):
        return False
    return not (is_unaddressed_question(item) and reply_taken(item, sayer, answers) is not None)


def waiting_on(item: Item, addressee: str, answers: Sequence[Answer] = ()) -> bool:
    """True when ``addressee`` still has this message open (#542, DESIGN.md §8.5).

    The message is addressed to that member: an @mention of them, or a reply to
    their own message. A person's message that names nobody is not addressed to
    one member, so it is never waiting, even though ``still_owed`` is true for
    every agent until someone answers it. Open is ``still_owed``: the delivery
    reached them and they have not handled it. ``answers`` matter only when the
    message is also an unaddressed question.
    """
    if not (item.mentioned or item.reply_to_me):
        return False
    return still_owed(item, addressee, answers)


def hop_tripped(room: Room) -> bool:
    """The loop guard: ``hop_limit`` agent messages in a row with no human message."""
    return (not room.paused) and room.hop_limit > 0 and room.hop_count >= room.hop_limit


# A Devin over HTTPS whose open wait() lost its connection and then made no call for this long is gone
# (#448): one wait round it may be in (``DEVIN_WAIT_CAP_S``, 100 s) and a margin.
WAIT_DROP_GRACE_S = 130.0


def wait_drop_expired(now: float, dropped_at: float) -> bool:
    """True once a wait() that dropped its connection at ``dropped_at`` has had no call or hook after it
    for ``WAIT_DROP_GRACE_S``: a live agent re-arms its wait at once, a killed one never does (#448)."""
    return now - dropped_at >= WAIT_DROP_GRACE_S


def clamp_wait(timeout_s: float | int | None, cap_s: int, default: int = 50) -> int:
    """wait() timeout clamped to [1, harness cap]."""
    try:
        t = int(timeout_s if timeout_s is not None else default)
    except (TypeError, ValueError):
        t = default
    return max(1, min(t, cap_s))


# --------------------------------------------------------------- requeue (M3)
def redeliver_ids(in_context: Iterable[Item]) -> list[int]:
    """Re-deliver once (DESIGN.md §8.5, FINDINGS §12: delivery is not handling).

    When a turn ends (Stop) the member's ``in_context`` priority items that it
    neither answered (say) nor passed on, and that were not re-delivered
    before, go back to pending for one more idle wake, marked ``again=yes``.
    Chatter is handled on confirmation and never comes back; the once-only
    mark is the ``redelivered`` column, independent of ``attempts``.
    """
    return [i.message_id for i in in_context if i.state == "in_context" and i.prio >= 1 and not i.redelivered]


# -------------------------------------------------------------- watchdog (M6)
def watch_since(item: Item) -> float | None:
    """When the watchdog's clock started for an @mention: when it reached the
    model (in context), or when it was notified as a "call read()" stub."""
    if item.state == "in_context":
        return item.in_context_at
    if item.state == "pending" and item.notified_at is not None:
        return item.notified_at
    return None


def watchdog_verdict(
    item: Item, *, now: float, watchdog_s: float, watchdog_max: int, answered_at: float | None, idle: bool
) -> str | None:
    """The watchdog for one @mention (DESIGN.md §8.5): None, 'remind', 'escalate' or 'done'.

    - Only @mentions (``mentioned``) that reached the member (in context, or a
      notified "call read()" stub) count, from that moment; the watchdog's state
      is ``reminders`` (reminders sent, plus ``WATCHDOG_DONE`` once finished).
    - Nothing happens while the member is busy in a turn (it is working on it,
      and a wake it can't take would only queue behind that turn), waiting on
      an approval prompt or offline: the watchdog acts once it is effectively
      idle (idle, or listening in wait() or a Cursor park).
    - A say() or pass() by the member since then answers it ('done', silently).
      say/pass already mark in-context items handled and notified stubs done;
      this is the backstop.
    - Otherwise, after ``watchdog_s``: a reminder while fewer than
      ``watchdog_max`` were sent, else the escalation to the human.
    """
    if watchdog_s <= 0 or not item.mentioned or item.watch_done:
        return None
    since = watch_since(item)
    if since is None or now - since < watchdog_s or not idle:
        return None
    if answered_at is not None and answered_at >= since:
        return "done"
    return "remind" if item.reminders < watchdog_max else "escalate"


def stalled(
    items: Sequence[Item],
    *,
    now: float,
    watchdog_s: float,
    watchdog_max: int,
    answered_at: float | None = None,
) -> list[Item]:
    """@mentions a member that isn't idle (busy in a turn, waiting on an approval
    prompt, offline) has held unanswered for ``(watchdog_max + 1) * watchdog_s``,
    as long as reminding and escalating an idle member takes: the human is told
    (a notice only; DESIGN.md §20). A say() or pass() since it arrived answers it."""
    if watchdog_s <= 0:
        return []
    limit = (watchdog_max + 1) * watchdog_s
    out = []
    for i in items:
        since = watch_since(i)
        if not i.mentioned or i.watch_done or since is None or now - since < limit:
            continue
        if answered_at is not None and answered_at >= since:
            continue
        out.append(i)
    return out


def parked_escalation(
    pending: Sequence[Item], *, parked_since: float, now: float, watchdog_s: float
) -> list[Item]:
    """@mentions that are waiting only because the member is parked (nothing can
    reach it), for ``watchdog_s`` since both the message and the parking: the
    human is told the member needs a poke (DESIGN.md §8.5)."""
    if watchdog_s <= 0:
        return []
    return [
        i
        for i in pending
        if i.mentioned and i.notified_at is None and now - max(i.ts, parked_since) >= watchdog_s
    ]


# ------------------------------------------------------------ read first (§24)
def _meeting_aside(item: Item) -> bool:
    """A call line that names nobody (§52). It is not a reason to wake, to spend a
    budget slot on its own, or to show a future wake time."""
    return item.sender_kind == "meeting" and item.prio == 0


def unaddressed_chatter(item: Item) -> bool:
    """Chatter that does not address this member (#548): prio 0, and not a reply to
    one of its own messages. A person's message is prio 2 and an @mention is prio 1,
    so neither is. A call line is the same kind of untrusted peer text (§52): one that
    names nobody does not block pass() either. The maintainer approved skipping
    unaddressed chatter on 2026-11-01 (§24)."""
    return envelope.untrusted_sender(item.sender_kind) and item.prio == 0 and not item.reply_to_me


def unread_stubs(pending: Iterable[Item]) -> list[int]:
    """The read-first rule for pass() (DESIGN.md §24): ids of peer messages the
    member was told about only as a "call read()" stub and has never been shown.

    - Only ``pending`` items with ``notified_at`` count: a stub that reached its
      context, which read() will show. Offered items don't (the read() answer the
      agent holds right now; a push read() couldn't show). An acked stop
      continuation is confirmed by the agent's next call (``Engine.before_call``),
      so its stubs are pending by the time that call is checked.
    - ``in_context_at`` set means an inline batch with its text was confirmed
      once (a read(), a wait() fill, say()'s unread, the Claude inbox, even a
      text cut to fit there): a re-delivered or escalated stub of it doesn't
      block again.
    - Human items never block: they are never stubbed.
    - Unaddressed chatter does not block (#548, maintainer's yes of 2026-11-01).
      An @mention and a reply to the member still do.
    """
    return sorted(
        i.message_id
        for i in pending
        if i.state == "pending"
        and i.notified_at is not None
        and envelope.untrusted_sender(i.sender_kind)
        and i.in_context_at is None
        and not unaddressed_chatter(i)
    )


# ----------------------------------------------- the standing guidance, once (#543, §8.6)
# The SessionStart sources (Claude Code's) after which a session still has what the broker last gave
# it: a fresh start and /clear. Claude Code keeps the switchboard instructions in its system prompt
# through both (mcp/server.py INSTRUCTIONS), and the reminder's pointer names them. Everything else
# rebuilds the context from a summary or an old transcript ("compact", "resume") or is not a source
# this code knows (missing, empty, new in a later Claude Code): the guidance goes out in full.
REMINDER_POINTER_SOURCES = frozenset({"startup", "clear"})
# The only harness whose SessionStart prints context (§7.3), and so the only one with a reminder. The
# others (Codex, Devin, Cursor) print nothing there, and their guidance is the join() text, in full each
# time; a harness not named here is never given a pointer.
REMINDER_POINTER_HARNESSES = frozenset({"claude"})


def reminder_is_full(harness: str, source: str | None, *, given_in_full: bool, rules_behind: bool) -> bool:
    """Whether a SessionStart reminder carries the standing guidance in full (#543, DESIGN.md §8.6).

    False (a one-line pointer instead) only when every one of these holds, so any doubt is a full
    reminder:

    - the harness is one whose SessionStart reaches the model (``REMINDER_POINTER_HARNESSES``) and says
      what began (``source``) in a word this code knows, one that leaves the switchboard instructions in
      place (``REMINDER_POINTER_SOURCES``), never a compaction or a resume;
    - ``given_in_full``: the broker knows it gave this session the full text, at its join or in an earlier
      full reminder (memory only, so a broker restart makes it unknown);
    - not ``rules_behind``: no room of the session has custom rules (§35) the session has not been shown,
      so the pointer never stands for rules it has not seen.

    The untrusted-peer line is no part of this decision: ``envelope.render_reminder`` puts it in both.
    """
    return not (
        harness in REMINDER_POINTER_HARNESSES
        and source in REMINDER_POINTER_SOURCES
        and given_in_full
        and not rules_behind
    )
