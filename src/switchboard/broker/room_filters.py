"""A room filtered to what a person comes back for (DESIGN.md §64, #914): the messages addressed to them
(Mentions) or the ones that carry a stored file (Files), a page at a time, newest first.

It reads what is stored and writes nothing. The room comes from ``RoomService.room_for``, the check every
room read makes (the window around a search result included, §58.4), which applies ``RoomService.visible``,
the one rule for who sees a room (§44): a private room the person is not in is "no such room". "Addressed
to me" is ``home.addressed_to``, the test the home page and the page's unread count already use, and
``Store.room_addressed`` and ``Store.room_with_files`` are the only code that knows the SQL. What the request
names (the filter, the cursor, the page size) is input from outside: cut, clamped, and an unknown filter is
refused, never read as All.
"""

from __future__ import annotations

from typing import Any

from switchboard.broker import home
from switchboard.broker.service import MAX_MESSAGE_ID, ServiceError

SHOWS = ("mentions", "files")
PAGE = 50
MAX_PAGE = 100
MAX_PARAM = 20  # a cursor or a page size is a few digits; anything longer is cut before it is read


def _cursor(raw: str | None, name: str, default: int) -> int:
    """A non-negative integer from a query parameter, cut to ``MAX_PARAM`` characters first."""
    if raw is None or raw == "":
        return default
    text = raw[:MAX_PARAM]
    if not text.isascii() or not text.isdigit():
        raise ServiceError("bad_request", f"{name} must be a whole number")
    return int(text)


def run(
    state: Any,
    viewer: int,
    me: str,
    slug: str,
    *,
    show: str | None,
    before: str | None = None,
    limit: str | None = None,
) -> dict[str, Any]:
    """One page of ``slug``'s messages under the filter ``show``, as the route answers it: ``messages``
    oldest first (as the log draws them), and ``next``, the cursor for the page before (``None`` on the
    last). ``viewer`` is who asks, as private rooms key them (§44); ``me`` is their screen name."""
    kind = (show or "")[:MAX_PARAM].strip().lower()
    if kind not in SHOWS:
        raise ServiceError("bad_request", "show must be mentions or files")
    room = state.service.room_for(slug, viewer)
    cursor = min(_cursor(before, "before", MAX_MESSAGE_ID), MAX_MESSAGE_ID)
    n = max(1, min(_cursor(limit, "limit", PAGE), MAX_PAGE))
    if kind == "mentions":
        rows = state.store.room_addressed(room.id, me, cursor, n + 1)
        more = len(rows) > n
        rows = rows[:n]
        msgs = [m for m, to_me in rows if home.addressed_to(m, me, to_me)]
        last = rows[-1][0].id if rows else None
    else:
        found = state.store.room_with_files(room.id, cursor, n + 1)
        more = len(found) > n
        msgs = found[:n]
        last = msgs[-1].id if msgs else None
    msgs.reverse()
    return {
        "room": room.name,
        "show": kind,
        "messages": state.service.message_frames(msgs),
        "next": last if more else None,
    }
