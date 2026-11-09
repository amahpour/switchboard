"""The home page's data (DESIGN.md §55, #626): what is waiting on one person across their rooms,
what got done since they last looked, and what is live now.

It is a query and a view over what the broker already stores (messages, the review boards, calls,
members and claims), never a new kind of record. What it keeps is per person: a "looked at" time in
``meta``, how far they have read each room, and which boards they set aside (§55.8, #663). Everything is
for one viewer: nobody sees a private room they are not in,
and each person's "waiting on you" is their own. It never decides anything for an agent: a card
only points at an answer the person gives through the routes the room already has (``say``, the
board's person moves).

Text that agents wrote (a message, a finding's title) is cut to a length here and drawn with
``textContent`` by the page, like everywhere else.
"""

from __future__ import annotations

import re
from typing import Any

from switchboard import reviews
from switchboard.broker.service import ServiceError
from switchboard.envelope import clean
from switchboard.models import Message, Room

BOARD_LINE = "Review board"  # how the board starts the lines it posts for an agent
LIST_MARK = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
MENTION_LEAD = re.compile(r"@([A-Za-z0-9_-]+)[\s,:;-]*")
WAIT_DAYS = 7  # a question older than this is not "waiting": it is history
FIRST_LOOK_S = 24 * 3600  # a person who never looked sees the last day of done work
DAY_S = 24 * 3600
TEXT_MAX = 200  # a message's first line, as a card shows it
TITLE_MAX = 160
CANDIDATES = 300  # chat messages read per room when looking for what is addressed to someone
DONE_MAX = 30
LIVE_MAX = 30
WAITING_MAX = 50


def seen_key(person_key: int) -> str:
    return f"home_seen:{person_key}"


def _plain(line: str) -> tuple[str, bool]:
    """A line as plain text, and whether it was a Markdown heading: a heading's ``#`` and a code span's
    backticks are Markdown the card does not draw, so they are not shown."""
    line = line.strip()
    return line.lstrip("#").replace("`", "").strip(), line.startswith("#")


def _first_line(text: str, limit: int = TEXT_MAX) -> str:
    """What a card says of a message: its first line with words in it, as plain text. A heading, or a lead-in
    that ends with a colon, says nothing alone ("Two issues:"), so it is joined to the next line with words
    before the cut to ``limit``."""
    lines = [x for x in (_plain(raw) for raw in clean(text).splitlines()) if x[0]]
    if not lines:
        return ""
    line, heading = lines[0]
    if len(lines) > 1 and (heading or line.endswith(":")):
        under = LIST_MARK.sub("", lines[1][0])  # a list's "- " or "1. " is not part of what it says
        line = f"{line}: {under}" if heading and not line.endswith(":") else f"{line} {under}"
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _question_text(text: str, me: str) -> str:
    """The card's line for a message to ``me``, without the @mention it opens with: the card already says
    it is for them. Only the viewer's own mention and ``@humans``, and only at the start; a message that is
    nothing else keeps it."""
    line = _first_line(text)
    out = line
    while (m := MENTION_LEAD.match(out)) and m.group(1).lower() in (me.lower(), "humans"):
        out = out[m.end() :]
    return out or line


def addressed_to(m: Message, me: str, parent_from_me: bool) -> bool:
    """Is ``m`` addressed to the person ``me``: the test the web page's unread count uses. An @mention
    of them, ``@humans``, or a reply to something they said; never their own message, and another
    person's message only when it names them (or everyone)."""
    me = me.lower()
    if m.sender_kind == "human" and m.sender_name.lower() == me:
        return False
    mentions = {x.lower() for x in m.mentions if isinstance(x, str)}
    if me in mentions or "humans" in mentions:
        return True
    return m.sender_kind != "human" and parent_from_me


def _question_cards(me: str, room: Room, questions: list[tuple[Message, bool]]) -> list[dict[str, Any]]:
    """The newest message addressed to ``me`` in ``room`` that they have not answered since, with how
    many more are waiting under it. Saying anything in the room answers all of them. ``questions`` is
    what ``Store.home_lines`` found: the SQL only narrows, and the page's own test decides here."""
    found = [m for m, to_me in questions if addressed_to(m, me, to_me)]
    if not found:
        return []
    m = found[-1]
    return [
        {
            "kind": "question",
            "room": room.name,
            "slug": room.slug,
            "id": m.id,
            "from": m.sender_name,
            "from_kind": m.sender_kind,
            "harness": m.sender_harness,
            "host": m.sender_host,
            "text": _question_text(m.text, me),
            "ts": m.ts,
            "more": len(found) - 1,
        }
    ]


def _board_cards(
    state: Any, room: Room, snoozed: dict[tuple[int, int], float] | None = None
) -> list[dict[str, Any]]:
    """One card per open board with something for a person to call: the questions, contested findings
    and unconfirmed fixes that need them (``needs_person``, the board's own test), each with the
    moves a person may make on it, which are the moves the room's board page offers. A board the person
    set aside with Not now (``snoozed``: ``(room, board)`` to when) has no card until something on it
    changes after that moment: a new item, or a move on one (#663)."""
    out = []
    for board in state.boards.boards(room):
        wait = [i for i in board["items"] if i["needs_person"]]
        if not wait:
            continue
        pressed = (snoozed or {}).get((room.id, board["id"]))
        if pressed is not None and board_changed_at(state, board) <= pressed:
            continue
        items = []
        for i in wait:
            if i["kind"] == "question" and i["state"] == "open":
                moves = ["answer"]
            elif i["state"] == "contested":
                moves = ["concede", "drop"]
            else:
                moves = ["confirm"]
            items.append(
                {
                    "label": i["label"],
                    "kind": i["kind"],
                    "state": i["state"],
                    "title": clean(i["title"])[:TITLE_MAX],
                    "raised_by": i["raised_by"],
                    "owner": i["owner"],
                    "options": [clean(o)[:TITLE_MAX] for o in i["options"]],
                    "recommend": i["recommend"],
                    "moves": moves,
                    "insist_line": clean(i["insist_line"])[:TITLE_MAX] if i["insist_line"] else "",
                    "updated_at": max(
                        (h["at"] for h in i["history"] if h.get("at")), default=board["opened_at"]
                    ),
                }
            )
        out.append(
            {
                "kind": "board",
                "room": room.name,
                "slug": room.slug,
                "url": board["url"],
                "ts": min(x["updated_at"] for x in items),
                "need": len(items),
                "items": items,
                "agents": list(board["agents"]),
            }
        )
    return out


def board_changed_at(state: Any, board: dict[str, Any]) -> float:
    """When a board last changed: the newest ``updated_at`` among its items (a new item starts with one), or
    when it was opened if it has none."""
    return max(
        (i.updated_at for i in state.store.review_items(board["id"])), default=float(board["opened_at"])
    )


def _done_boards(state: Any, room: Room, since: float) -> list[dict[str, Any]]:
    """A board handed off (its review posted by one agent), or settled and waiting for the handoff."""
    out = []
    for rv in state.store.room_reviews(room.id):
        items = state.store.review_items(rv["id"])
        if rv["posted_at"] is not None and rv["posted_at"] >= since:
            out.append(
                {
                    "kind": "review",
                    "room": room.name,
                    "slug": room.slug,
                    "who": rv["posted_by"],
                    "ts": rv["posted_at"],
                    "text": "posted the review",
                }
            )
        elif rv["posted_at"] is None and rv["closed_at"] is None and items and reviews.settled(items):
            ts = max(i.updated_at for i in items)
            if ts >= since:
                out.append(
                    {
                        "kind": "board",
                        "room": room.name,
                        "slug": room.slug,
                        "who": "",
                        "ts": ts,
                        "text": f"The review board is settled: {len(items)} "
                        + ("item" if len(items) == 1 else "items"),
                    }
                )
    return out


def _done_turns(room: Room, done: list[Message]) -> list[dict[str, Any]]:
    """What each agent said in the room since the person looked, its latest message only. A turn that ended
    with a ``pass`` said nothing, so it is not here: only a result someone can read is a line. A message
    addressed to the person waits under Waiting on you instead, and a review board line is the board's;
    ``Store.home_lines`` has left both out."""
    return [
        {
            "kind": "turn",
            "room": room.name,
            "slug": room.slug,
            "who": m.sender_name,
            "harness": m.sender_harness,
            "ts": m.ts,
            "id": m.id,
            "text": _first_line(m.text, 140),
        }
        for m in done
    ]


def _done_calls(state: Any, visible: dict[int, Room], since: float) -> list[dict[str, Any]]:
    out = []
    for call in state.store.meetings_ended_since(since):
        room = visible.get(call.room_id)
        if room is None or call.ended_at is None:
            continue
        lines, woke = state.store.meeting_lines(call.id)
        out.append(
            {
                "kind": "call",
                "room": room.name,
                "slug": room.slug,
                "who": "",
                "ts": call.ended_at,
                "lines": lines,
                "woke": woke[:6],
            }
        )
    return out


def _live(
    state: Any, rooms: dict[int, Room], person_id: int | None, admin: bool, now: float
) -> list[dict[str, Any]]:
    """Calls and agents at work in the person's rooms. Three queries for every room's agents and one for the
    open calls, and ``call_detail`` only for a room that has one (it is what keeps a private room's host
    from being named to someone outside it)."""
    out: list[dict[str, Any]] = []
    member_rows, claim_rows, counts = state.store.live_agents(now)
    for call in state.store.open_meetings():
        room = rooms.get(call.room_id)
        detail = state.service.call_detail(room, person_id, admin) if room is not None else None
        if room is None or detail is None:
            continue
        out.append(
            {
                "kind": "call",
                "room": room.name,
                "slug": room.slug,
                "ts": detail["started_at"],
                "platform": detail["platform"],
                "state": detail["state"],
                "source": detail["source"],
                "started_by": detail.get("started_by"),
                "agents": counts.get(room.id, 0),
            }
        )
    claims: dict[int, dict[str, Any]] = {}
    for c in claim_rows:  # the one that ends first, as the Members pane shows
        if c["membership_id"] not in claims or c["expires_at"] < claims[c["membership_id"]]["expires_at"]:
            claims[c["membership_id"]] = c
    for m in member_rows:
        room = rooms.get(m["room_id"])
        if room is None:
            continue
        claim = claims.get(m["membership_id"])
        out.append(
            {
                "kind": "agent",
                "room": room.name,
                "slug": room.slug,
                "agent": m["screen_name"],
                "harness": m["harness"],
                "host": m["host"],
                "busy": m["status"] == "busy",
                "note": clean(m["status_line"] or "")[:TITLE_MAX],
                "claim": None
                if claim is None
                else {
                    "key": claim["key"],
                    "note": clean(claim["note"])[:TITLE_MAX],
                    "expires_at": claim["expires_at"],
                },
            }
        )
    return out


def seen_at(state: Any, person_key: int) -> float | None:
    raw = state.store.meta_get(seen_key(person_key))
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


def mark_room_seen(state: Any, person_key: int, room: Room, upto: Any) -> int:
    """Move a person's mark in a room forward to the message id ``upto`` (the caller has checked they may see
    the room). A value that is not a whole number from 0 up is refused; one past the room's newest message
    is cut to it, so the mark can never hide a message that has not been written yet."""
    if isinstance(upto, bool) or not isinstance(upto, int) or not 0 <= upto < 2**53:
        raise ServiceError("bad_request", "give id, a message id")
    return state.store.home_mark_seen(person_key, room.id, upto)


def snooze_board(state: Any, person_key: int, room: Room, url: Any) -> float:
    """Set aside the room's open board at ``url`` for this person until it changes. Only an open board of
    this room can be (the caller has checked they may see the room); anything else is "no such board"."""
    if not isinstance(url, str) or not 0 < len(url) <= 500:
        raise ServiceError("bad_request", "give url, a board's address")
    for rv in state.store.open_reviews(room.id):
        if rv["url"] == url:
            at = state.clock.now()
            state.store.home_snooze(person_key, room.id, rv["id"], at)
            return at
    raise ServiceError("not_found", "no such board in this room")


def mark_seen(state: Any, person_key: int, at: float | None) -> float:
    """Move this person's "looked at" time forward to ``at`` (never into the future, never back)."""
    now = state.clock.now()
    at = now if at is None else min(float(at), now)
    old = seen_at(state, person_key)
    if old is not None and old >= at:
        return old
    state.store.meta_set(seen_key(person_key), repr(at))
    return at


def build(state: Any, name: str, person_id: int | None, admin: bool) -> dict[str, Any]:
    """The home page for the person ``name`` (``person_id`` None is the owner)."""
    now = state.clock.now()
    key = state.service.person_key(person_id)
    rooms = [r for r in state.store.list_rooms() if state.service.visible(r, key)]
    visible = {r.id: r for r in rooms}
    seen = seen_at(state, key)
    read = state.store.home_seen_marks(key)
    snoozed = state.store.home_snoozes(key)
    since = max(seen if seen is not None else now - FIRST_LOOK_S, now - WAIT_DAYS * DAY_S)

    waiting: list[dict[str, Any]] = []
    done: list[dict[str, Any]] = []
    for room in rooms:
        questions, turns = state.store.home_lines(
            room.id, name, now - WAIT_DAYS * DAY_S, since, CANDIDATES, BOARD_LINE, read.get(room.id, 0)
        )
        waiting += _question_cards(name, room, questions)
        waiting += _board_cards(state, room, snoozed)
        done += _done_boards(state, room, since)
        done += _done_turns(room, turns)
    live = _live(state, visible, person_id, admin, now)
    done += _done_calls(state, visible, since)
    waiting.sort(key=lambda c: -c["ts"])
    done.sort(key=lambda c: -c["ts"])
    live.sort(key=lambda c: (c["kind"] != "call", c["room"], c.get("agent", "")))
    return {
        "now": now,
        "since": since,
        "waiting": waiting[:WAITING_MAX],
        "waiting_total": len(waiting),
        "done": done[:DONE_MAX],
        "done_total": len(done),
        "live": live[:LIVE_MAX],
    }
