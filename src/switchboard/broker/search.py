"""Search across a person's rooms (DESIGN.md §58, #724): the rooms they can see, the safe query, the page of
hits, and the limit on how fast one person may ask.

It reads what is stored and writes nothing. The rooms come from ``RoomService.visible`` (§44), the one rule
for who sees a room, so a private room the person is not in, a room they left and a closed room are never
searched, and are not counted either. The query is input from outside: ``switchboard.search`` makes it safe
and ``Store.search`` is the only code that knows the index. Snippets are text with highlight offsets, never
HTML, and the page draws them with ``textContent``.
"""

from __future__ import annotations

import secrets
from collections import deque
from typing import Any

from switchboard import search
from switchboard.broker.service import ServiceError
from switchboard.clock import Clock
from switchboard.models import Room

PAGE = 50
MAX_NAME = 100  # a screen name is cut to this before it is compared
WINDOW_S = 10.0
MAX_PER_WINDOW = 30  # a person typing asks about 4 times a second at most (the page waits for a pause)
MAX_TRACKED = 1000
MAX_TIME = 1e12


class SearchLimiter:
    """At most ``MAX_PER_WINDOW`` searches per person in any ``WINDOW_S`` seconds. In memory, per person
    (their id as ``room_people`` keys it), on the broker's clock so a test advances it."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self._calls: dict[int, deque[float]] = {}

    def wait_s(self, person: int) -> float:
        """Seconds until ``person`` may search again (0: now); counts this call when it is allowed."""
        now = self.clock.now()
        q = self._calls.setdefault(person, deque())
        while q and q[0] <= now - WINDOW_S:
            q.popleft()
        if len(q) >= MAX_PER_WINDOW:
            return max(0.1, q[0] + WINDOW_S - now)
        q.append(now)
        if len(self._calls) > MAX_TRACKED:
            for k in [k for k, v in self._calls.items() if not v or v[-1] <= now - WINDOW_S]:
                del self._calls[k]
        return 0.0


def _number(raw: str | None, name: str) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        n = float(raw)
    except ValueError:
        raise ServiceError("bad_request", f"{name} must be a number") from None
    if not 0 <= n < MAX_TIME:
        raise ServiceError("bad_request", f"{name} must be a time")
    return n


def visible_rooms(state: Any, viewer: int) -> list[Room]:
    """The open rooms ``viewer`` can see now (§44): the same test every room route uses."""
    return [r for r in state.store.list_rooms() if state.service.visible(r, viewer)]


def run(
    state: Any,
    viewer: int,
    *,
    q: str,
    room: str | None = None,
    sender: str | None = None,
    since: str | None = None,
    until: str | None = None,
    calls: str | None = None,
    before: str | None = None,
) -> dict[str, Any]:
    """One page of hits for ``viewer``, newest first, as the route answers it. ``room`` is a room name the
    viewer must be able to see ("no such room" otherwise, as every room route says it)."""
    rooms = visible_rooms(state, viewer)
    if room:
        wanted = state.service.room_for(room, viewer)  # raises for a room they cannot see, or a closed one
        rooms = [r for r in rooms if r.id == wanted.id]
    by_id = {r.id: r for r in rooms}
    words = search.words_of(q)
    match = search.match_query(q)
    cursor = _number(before, "before")
    lo, hi = _number(since, "since"), _number(until, "until")
    if calls not in (None, "", "0", "1"):
        raise ServiceError("bad_request", "calls must be 0 or 1")
    who = sender.strip()[:MAX_NAME] if sender and sender.strip() else None
    nonce = secrets.token_hex(6)
    got = state.store.search(
        list(by_id),
        match or "",
        nonce=nonce,
        sender=who,
        since=lo,
        until=hi,
        calls=calls != "0",
        before=int(cursor) if cursor is not None else None,
        limit=PAGE,
        count=cursor is None,
    )
    hits = []
    for h in got["hits"]:
        r = by_id[h["room_id"]]
        text, marks = search.parse_snippet(h["snippet"], nonce)
        kind = {"human": "person", "agent": "agent"}.get(h["sender_kind"], "call")
        hits.append(
            {
                "id": h["id"],
                "room": r.name,
                "slug": r.slug,
                "from": h["sender_name"],
                "kind": kind,
                "harness": h["sender_harness"] if kind == "agent" else None,
                "host": h["host"],
                "call": h["sender_kind"] == "meeting",
                "ts": h["ts"],
                "snippet": {"text": text, "marks": marks},
            }
        )
    out: dict[str, Any] = {
        "q": q[: search.MAX_QUERY_CHARS],
        "words": words,
        "searchable": match is not None,
        "hits": hits,
        "next": hits[-1]["id"] if got["more"] and hits else None,
    }
    if "total" in got:
        out["total"], out["capped"] = got["total"], got["capped"]
    return out
