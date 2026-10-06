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
message or an agent's own ``say()``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from switchboard import envelope
from switchboard.models import Item, Release, Room

MENTION_RE = re.compile(r"(?<![\w@])@([a-z][a-z0-9_-]{0,23})(?![a-z0-9_-])", re.IGNORECASE)

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
    """(prio, mentioned) for one recipient (DESIGN.md §8.1): 2 human, 1 @mention, 0 chatter."""
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
    """Human items first, then mentions, then chatter; each group by message id."""
    return sorted(items, key=lambda i: (-i.prio, i.message_id))


def item_cost(item: Item, limit: int | None = envelope.ITEM_LIMIT) -> int:
    """Characters the item's rendered (inline, sanitized) envelope line takes:
    escaping can make text up to six times longer than it was typed."""
    return len(envelope.render_item(item, "", "", inline=True, limit=limit)) + 1


def cap(items: Sequence[Item], max_msgs: int, max_chars: int) -> list[Item]:
    """Keep a prefix of at most ``max_msgs`` items whose rendered batch fits
    ``max_chars`` (header and footer included).

    Always keeps at least one item (the envelope cuts a single oversized item
    to fit, see ``envelope.fit_batch``), so a huge message can never block
    the queue.
    """
    out: list[Item] = []
    used = FRAME_RESERVE
    for it in items:
        if len(out) >= max(1, max_msgs):
            break
        c = item_cost(it)
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


def chatter_due(room: Room, chatter: Sequence[Item], now: float, quiet_s: float, max_hold_s: float) -> bool:
    """Chatter goes out once the room has been quiet for ``quiet_s``, or the oldest
    item has waited ``max_hold_s``."""
    if not chatter:
        return False
    quiet = room.last_msg_at is None or now - room.last_msg_at >= quiet_s
    held_long = now - chatter[0].ts >= max_hold_s
    return quiet or held_long


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
) -> Release | None:
    """What may go out to one member now (DESIGN.md §8.2), or None.

    - Stubs that were already notified are pull-only: they never wake again.
    - Busy (mid-task): priority items only, never chatter, not counted.
    - Idle: priority items wake at once (humans even at budget 0), plus
      chatter if this turn boundary hasn't had a peer batch yet; chatter
      alone waits for the quiet period (or the max hold) and needs budget.
    """
    eligible = [d for d in pending if d.notified_at is None]
    prio = [d for d in eligible if d.prio >= 1]
    chatter = [d for d in eligible if d.prio == 0]
    ok = peer_ok(peer_batch_boundary, boundary_seq)

    def done(items: list[Item], kind: str, counted: bool) -> Release:
        kept = cap(human_first(items), batch_max_msgs, max_chars)
        return Release(items=tuple(kept), kind=kind, counted=counted, reason=wake_reason(kept))

    if eff == "busy":
        return done(prio, "priority", False) if prio else None
    if not is_idle(eff):
        return None
    if prio and (any(d.prio == 2 for d in prio) or room.budget_remaining > 0):
        return done(prio + (chatter if ok else []), "wake", True)
    if chatter and ok and room.budget_remaining > 0:
        if chatter_due(room, chatter, now, quiet_s, max_hold_s):
            return done(chatter, "wake", True)
    return None


def budget_blocked(
    pending: Sequence[Item], *, room: Room, eff: str, peer_batch_boundary: int, boundary_seq: int
) -> bool:
    """True when an idle member has wake-eligible non-human items that only the
    empty budget holds back (drives the once-per-window budget_exhausted notice)."""
    if not is_idle(eff) or room.budget_remaining > 0:
        return False
    eligible = [d for d in pending if d.notified_at is None]
    if any(d.prio == 2 for d in eligible):
        return False
    if any(d.prio == 1 for d in eligible):
        return True
    return any(d.prio == 0 for d in eligible) and peer_ok(peer_batch_boundary, boundary_seq)


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


def hop_tripped(room: Room) -> bool:
    """The loop guard: ``hop_limit`` agent messages in a row with no human message."""
    return (not room.paused) and room.hop_limit > 0 and room.hop_count >= room.hop_limit


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
    """
    return sorted(
        i.message_id
        for i in pending
        if i.state == "pending"
        and i.notified_at is not None
        and i.sender_kind == "agent"
        and i.in_context_at is None
    )
