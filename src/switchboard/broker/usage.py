"""The usage page's data (DESIGN.md §61, #818).

A read of stored rows for the one person asking: the open rooms they can see (§44), the
wake batches and agent messages in those rooms, and how often each room's budget ran out.
It writes nothing and adds no table. The windows and the ranking are pure (:mod:`switchboard.usage`).
"""

from __future__ import annotations

from typing import Any

from switchboard.usage import parse_offset, shape, window_starts


def build(state: Any, viewer: int, offset_raw: str | None) -> dict[str, Any]:
    """The page for ``viewer`` (a person's id, 0 the owner, -1 nobody). ``offset_raw`` is the
    browser's minutes east of UTC; a value this release does not know is UTC."""
    offset = parse_offset(offset_raw)
    now = float(state.store.clock.now())
    today, week = window_starts(now, offset)
    rooms = state.service.visible_rooms(viewer)
    ids = [r.id for r in rooms]
    return shape(
        rooms,
        state.store.usage_wakes(ids, since=week, today=today, until=now),
        state.store.usage_posts(ids, since=week, today=today, until=now),
        state.store.usage_exhausted(ids, since=week, until=now),
        now=now,
        offset_min=offset,
        today_start=today,
        week_start=week,
    )
