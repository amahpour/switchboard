"""What agents cost (DESIGN.md §61, #818): windows and the ranking, with no I/O.

A wake is a ``kind='wake'`` batch. A post is an agent's chat message. The budget on the page
is the room's hourly rate and what it has left this hour, including a refill the next wake has
not written yet (:func:`budget_left`). How often the budget ran out is the ``budget_exhausted``
events in the window: a wake the budget did not send is not a row, so it is not a count here.
The caller decides which rooms are in the picture (§44).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from switchboard.models import Room, room_slug

DAY_S = 86_400.0
HOUR_S = 3600.0  # the same window Store.refill_budget writes when the next wake asks
OFFSET_LIMIT = 14 * 60  # a real zone is within 14 hours of UTC; past that, the window is UTC
_OFFSET_RE = re.compile(r"-?\d{1,4}\Z")


def clamp_offset(value: object) -> int:
    """Minutes east of UTC, or 0 when the value is not a whole number of minutes inside ±14 hours.

    A bool is not an offset (``True`` is an ``int`` in Python, and it is not a zone).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    if value < -OFFSET_LIMIT or value > OFFSET_LIMIT:
        return 0
    return value


def parse_offset(raw: str | None) -> int:
    """The ``offset`` query: an optional minus and one to four digits, then :func:`clamp_offset`.

    Anything else, including a sign other than a leading minus and any space, is UTC. An
    unrecognized value fails closed to the shorter, well-known window, never to all time.
    """
    if raw is None or raw == "":
        return 0
    if not isinstance(raw, str) or _OFFSET_RE.fullmatch(raw) is None:
        return 0
    return clamp_offset(int(raw))


def window_starts(now: float, offset_min: object) -> tuple[float, float]:
    """``(today, week)`` as unix times: local midnight, and the midnight six days before it.

    ``offset_min`` is minutes east of UTC (:func:`clamp_offset`). The seven days include today.
    ``now`` is a unix time; the broker's clock and the tests' clock both are.
    """
    off = clamp_offset(offset_min)
    local = float(now) + off * 60.0
    midnight = local - (local % DAY_S)
    today = midnight - off * 60.0
    return today, today - 6 * DAY_S


def daily(times: Sequence[tuple[float, int]], week_start: float, days: int = 7) -> list[dict[str, Any]]:
    """Counts per local day for the ``days`` days from ``week_start`` (a local midnight, §61).

    ``times`` is ``(unix time, how many)`` pairs. A time before the first midnight or from the
    last day's end on is not in any bar, and a count that is not a whole number is 0, so a bad row
    cannot move a bar. The offset is fixed for the window, so a day is always 24 hours: the page
    sends one offset and the broker does not know the zone's daylight saving changes (§61).
    Each bar is ``{"start": <that day's local midnight>, "wakes": n}``, oldest first, today last.
    """
    bars = [0] * days
    for at, n in times:
        if isinstance(at, bool) or not isinstance(at, (int, float)) or at != at:
            continue
        index = int((float(at) - week_start) // DAY_S) if float(at) >= week_start else -1
        if 0 <= index < days:
            bars[index] += _n(n)
    return [{"start": week_start + i * DAY_S, "wakes": n} for i, n in enumerate(bars)]


def earlier(today_start: float, week_start: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """The two periods the tiles compare with: ``(yesterday, the 7 days before)``, each ``[from, to)``.

    Yesterday is the whole of the day before today, and the 7 days before are the seven local days
    that end the day before the week starts (§61).
    """
    return (today_start - DAY_S, today_start), (week_start - 7 * DAY_S, week_start)


def budget_left(per_hour: object, remaining: object, window_start: object, now: float) -> int:
    """What is left this hour, from the stored window, without writing a refill.

    The store refills only when the next wake asks. A room that spent the hour and then went
    quiet would otherwise keep showing 0. Once ``now`` has reached ``window_start + 3600``,
    that wake would set remaining to ``per_hour``, so the page shows the rate. Inside the
    hour, what is stored is what is left. A window that is not a finite time shows the stored
    remaining: an unrecognized value does not invent a full hour.
    """
    rate, left = _n(per_hour), _n(remaining)
    if isinstance(window_start, bool) or not isinstance(window_start, (int, float)):
        return left
    start = float(window_start)
    if start != start or start in (float("inf"), float("-inf")):
        return left
    if float(now) >= start + HOUR_S:
        return rate
    return left


def _n(value: object) -> int:
    """A stored count: a non-negative int. Anything else, including a bool, is 0."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value > 0 else 0


def _cell(row: Any, key: str) -> Any:
    return row[key]


class _Tally:
    """One agent in one room, the two casings of one name already folded together."""

    __slots__ = (
        "name",
        "name_week",
        "harness",
        "host",
        "room_id",
        "wakes_today",
        "wakes_week",
        "posts_today",
        "posts_week",
    )

    def __init__(self, name: str, harness: str, host: str, room_id: int) -> None:
        self.name = name
        self.name_week = -1
        self.harness = harness
        self.host = host
        self.room_id = room_id
        self.wakes_today = 0
        self.wakes_week = 0
        self.posts_today = 0
        self.posts_week = 0

    def called(self, name: str, week: int) -> None:
        """Keep the casing from the row that posted or was woken most often in the window."""
        if week > self.name_week:
            self.name = name
            self.name_week = week

    def lively(self) -> bool:
        return bool(self.wakes_today or self.wakes_week or self.posts_today or self.posts_week)


def shape(
    rooms: Sequence[Room | None],
    wakes: Sequence[Any],
    posts: Sequence[Any],
    exhausted: Mapping[int, int],
    *,
    now: float,
    offset_min: object,
    today_start: float,
    week_start: float,
) -> dict[str, Any]:
    """The page: every room the caller passed, then the agents who were woken or who posted.

    A count whose room was not passed is dropped, so this function cannot widen what the
    caller decided was visible. Rooms and agents are busiest first (wakes in the window,
    then posts, then the name). An agent with nothing in the window is left out; a room
    is listed anyway, so its budget still shows. Two casings of one name, in one room and
    on one host, are one row.
    """
    known = [r for r in rooms if isinstance(r, Room)]
    by_id = {r.id: r for r in known}
    totals = {r.id: {"wakes_today": 0, "wakes_week": 0, "posts_today": 0, "posts_week": 0} for r in known}
    agents: dict[tuple[int, str, str, str], _Tally] = {}

    def tally(row: Any) -> _Tally | None:
        room_id = _cell(row, "room_id")
        if isinstance(room_id, bool) or not isinstance(room_id, int) or room_id not in by_id:
            return None
        name = _cell(row, "name")
        if not isinstance(name, str) or not name:
            return None
        harness = _cell(row, "harness") or ""
        host = _cell(row, "host") or ""
        if not isinstance(harness, str):
            harness = ""
        if not isinstance(host, str):
            host = ""
        key = (room_id, name.casefold(), harness, host)
        found = agents.get(key)
        if found is None:
            found = _Tally(name, harness, host, room_id)
            agents[key] = found
        return found

    def add(row: Any, *, wakes_row: bool) -> None:
        found = tally(row)
        if found is None:
            return None
        today, week = _n(_cell(row, "today")), _n(_cell(row, "week"))
        found.called(str(_cell(row, "name")), week)
        box = totals[found.room_id]
        if wakes_row:
            found.wakes_today += today
            found.wakes_week += week
            box["wakes_today"] += today
            box["wakes_week"] += week
        else:
            found.posts_today += today
            found.posts_week += week
            box["posts_today"] += today
            box["posts_week"] += week
        return None

    for row in wakes:
        add(row, wakes_row=True)
    for row in posts:
        add(row, wakes_row=False)

    def room_dict(room: Room) -> dict[str, Any]:
        box = totals[room.id]
        ran = exhausted.get(room.id, 0) if isinstance(exhausted, Mapping) else 0
        return {
            "name": room.name,
            "slug": room_slug(room.name),
            "private": bool(room.private),
            "budget_per_hour": _n(room.budget_per_hour),
            "budget_remaining": budget_left(
                room.budget_per_hour, room.budget_remaining, room.budget_window_start, now
            ),
            "wakes_today": box["wakes_today"],
            "wakes_week": box["wakes_week"],
            "posts_today": box["posts_today"],
            "posts_week": box["posts_week"],
            "exhausted_week": _n(ran),
        }

    ordered = sorted(
        known,
        key=lambda r: (-totals[r.id]["wakes_week"], -totals[r.id]["posts_week"], r.name.casefold()),
    )
    shown = [a for a in agents.values() if a.lively()]
    shown.sort(
        key=lambda a: (
            -a.wakes_week,
            -a.posts_week,
            a.name.casefold(),
            by_id[a.room_id].name.casefold(),
            a.host,
        )
    )
    return {
        "now": float(now),
        "offset_min": clamp_offset(offset_min),
        "totals": {
            key: sum(totals[r.id][key] for r in known)
            for key in ("wakes_today", "wakes_week", "posts_today", "posts_week")
        },
        "today_start": float(today_start),
        "week_start": float(week_start),
        "rooms": [room_dict(r) for r in ordered],
        "agents": [
            {
                "name": a.name,
                "harness": a.harness,
                "host": a.host,
                "room": by_id[a.room_id].name,
                "slug": room_slug(by_id[a.room_id].name),
                "wakes_today": a.wakes_today,
                "wakes_week": a.wakes_week,
                "posts_today": a.posts_today,
                "posts_week": a.posts_week,
            }
            for a in shown
        ],
    }
