"""The home page's data (DESIGN.md §55, #626): what is waiting on one person across their rooms,
what got done since they last looked, and what is live now.

It is a query and a view over what the broker already stores (messages, the review boards, calls,
members and claims), never a new kind of record. The one new row is a per-person "looked at"
time, kept in ``meta``. Everything is for one viewer: nobody sees a private room they are not in,
and each person's "waiting on you" is their own. It never decides anything for an agent: a card
only points at an answer the person gives through the routes the room already has (``say``, the
board's person moves).

Text that agents wrote (a message, a finding's title) is cut to a length here and drawn with
``textContent`` by the page, like everywhere else.
"""

from __future__ import annotations

from typing import Any

from switchboard import reviews
from switchboard.envelope import clean
from switchboard.models import Message, Room

BOARD_LINE = "Review board"  # how the board starts the lines it posts for an agent
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


def _first_line(text: str, limit: int = TEXT_MAX) -> str:
    """The first line with words in it, as plain text: a heading's ``#`` and a code span's backticks are
    Markdown the card does not draw, so they are not shown."""
    for line in clean(text).splitlines():
        line = line.strip().lstrip("#").replace("`", "").strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


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


def _question_cards(state: Any, room: Room, me: str, now: float) -> list[dict[str, Any]]:
    """The newest message addressed to ``me`` in ``room`` that they have not answered since, with how
    many more are waiting under it. Saying anything in the room answers all of them."""
    mine = state.store.last_message_id_of_human(room.id, me)
    candidates = state.store.chat_after(room.id, mine, now - WAIT_DAYS * DAY_S, CANDIDATES)
    parents: dict[int, bool] = {}

    def from_me(message_id: int) -> bool:
        if message_id not in parents:
            p = state.store.get_message(message_id)
            parents[message_id] = (
                p is not None
                and p.room_id == room.id
                and p.sender_kind == "human"
                and p.sender_name.lower() == me.lower()
            )
        return parents[message_id]

    found = [
        m
        for m in candidates
        if m.sender_kind != "meeting" and addressed_to(m, me, m.reply_to is not None and from_me(m.reply_to))
    ]
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
            "text": _first_line(m.text),
            "ts": m.ts,
            "more": len(found) - 1,
        }
    ]


def _board_cards(state: Any, room: Room) -> list[dict[str, Any]]:
    """One card per open board with something for a person to call: the questions, contested findings
    and unconfirmed fixes that need them (``needs_person``, the board's own test), each with the
    moves a person may make on it, which are the moves the room's board page offers."""
    out = []
    for board in state.boards.boards(room):
        wait = [i for i in board["items"] if i["needs_person"]]
        if not wait:
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


def _done_turns(state: Any, room: Room, me: str, since: float, now: float) -> list[dict[str, Any]]:
    """What each agent said in the room since ``since``, its latest message only. A turn that ended
    with a ``pass`` said nothing, so it is not here: only a result someone can read is a line. A
    message addressed to ``me`` waits under Waiting on you instead."""
    out: dict[str, Message] = {}
    for m in state.store.chat_after(room.id, 0, max(since, now - WAIT_DAYS * DAY_S), CANDIDATES):
        if m.sender_kind != "agent" or m.text.startswith(BOARD_LINE):
            continue  # a move on the review board is the board's, and shows there
        if "humans" in m.mentions or me.lower() in {x.lower() for x in m.mentions if isinstance(x, str)}:
            continue
        out[m.sender_name] = m
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
        for m in out.values()
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


def _live(state: Any, room: Room, w_person: int | None, admin: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    detail = state.service.call_detail(room, w_person, admin)
    members = state.service.members(room.name)
    if detail is not None:
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
                "agents": len(members),
            }
        )
    for m in members:
        claim = min(m["claims"], key=lambda c: c["expires_at"]) if m["claims"] else None
        if m["status"] != "busy" and claim is None:
            continue
        out.append(
            {
                "kind": "agent",
                "room": room.name,
                "slug": room.slug,
                "agent": m["name"],
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
    since = max(seen if seen is not None else now - FIRST_LOOK_S, now - WAIT_DAYS * DAY_S)

    waiting: list[dict[str, Any]] = []
    done: list[dict[str, Any]] = []
    live: list[dict[str, Any]] = []
    for room in rooms:
        waiting += _question_cards(state, room, name, now)
        waiting += _board_cards(state, room)
        done += _done_boards(state, room, since)
        done += _done_turns(state, room, name, since, now)
        live += _live(state, room, person_id, admin)
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
