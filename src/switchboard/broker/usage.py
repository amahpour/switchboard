"""The usage page's data (DESIGN.md §61, #818).

A read of stored rows for the one person asking: the open rooms they can see (§44), the
wake batches and agent messages in those rooms, and how often each room's budget ran out.
It writes nothing. The windows and the ranking are pure (:mod:`switchboard.usage`).

The room list is taken on the broker's event loop, where the one visibility rule already
runs. The three counts run on a connection of their own, so the caller can put them on a
worker thread and leave the broker's connection to the loop. A long history then does not
stall every other request.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from switchboard import db
from switchboard.clock import Clock
from switchboard.models import Room
from switchboard.store import Store
from switchboard.usage import parse_offset, shape, window_starts


def counts(
    db_path: str | os.PathLike[str],
    clock: Clock,
    rooms: Sequence[Room],
    offset_raw: str | None,
) -> dict[str, Any]:
    """The page for ``rooms`` (already the ones the viewer can see). ``offset_raw`` is the
    browser's minutes east of UTC; a value this release does not know is UTC.

    Opens a read-only connection and closes it. Safe to call off the event loop: it does not
    touch the broker's own connection.
    """
    offset = parse_offset(offset_raw)
    now = float(clock.now())
    today, week = window_starts(now, offset)
    con = db.connect(db_path, readonly=True)
    try:
        store = Store(con, clock)
        ids = [r.id for r in rooms]
        return shape(
            rooms,
            store.usage_wakes(ids, since=week, today=today, until=now),
            store.usage_posts(ids, since=week, today=today, until=now),
            store.usage_exhausted(ids, since=week, until=now),
            now=now,
            offset_min=offset,
            today_start=today,
            week_start=week,
        )
    finally:
        con.close()
