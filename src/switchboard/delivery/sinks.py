"""SinkRegistry: open wait() calls and Cursor stop parks.

A sink is an agent that is blocked, listening: an open sink makes the
member effectively idle (DESIGN.md §8.2). The registry is plain in-memory
state owned by the engine; the runner attaches asyncio futures to it.

- ``wait``: one member (room) listening through the MCP ``wait()`` tool.
- ``park``: a Cursor stop hook long-polling for a follow-up (DESIGN.md §9.4).
  It belongs to the whole session (one conversation), so it serves every
  room of that participant; one live park per participant.
"""

from __future__ import annotations

import itertools
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Sink:
    id: int
    participant_id: int
    membership_id: int
    room_id: int
    kind: str  # 'wait' | 'park' (a Cursor stop hook, for every room of the participant)
    path: str  # batch path used when the engine fills it
    wait_id: str
    opened_at: float
    deadline: float
    conn_id: int | None = None
    result: dict[str, Any] | None = None
    batch_id: int | None = None
    closed: bool = False
    close_reason: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def open(self) -> bool:
        return not self.closed


class SinkRegistry:
    RECENT_MAX = 512

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self._open: dict[int, Sink] = {}
        # recently closed sinks, so a late unwait can still find the batch it got
        self._recent: OrderedDict[int, Sink] = OrderedDict()

    def open(self, **kw: Any) -> Sink:
        s = Sink(id=next(self._ids), **kw)
        self._open[s.id] = s
        return s

    def get(self, sink_id: int) -> Sink | None:
        return self._open.get(sink_id) or self._recent.get(sink_id)

    def open_sinks(self) -> list[Sink]:
        return list(self._open.values())

    def for_participant(self, participant_id: int) -> list[Sink]:
        return [s for s in self._open.values() if s.participant_id == participant_id]

    def open_for(self, participant_id: int, membership_id: int | None = None) -> Sink | None:
        """The member's open wait() sink, else its session's park (a park serves every room)."""
        park = None
        for s in self._open.values():
            if s.participant_id != participant_id:
                continue
            if membership_id is None or s.membership_id == membership_id:
                return s
            if s.kind == "park" and park is None:
                park = s
        return park

    def parks_for(self, participant_id: int) -> list[Sink]:
        return [s for s in self._open.values() if s.participant_id == participant_id and s.kind == "park"]

    def for_room(self, room_id: int) -> list[Sink]:
        """Open wait() sinks in a room (parks belong to a session, see ``parks``)."""
        return [s for s in self._open.values() if s.room_id == room_id and s.kind != "park"]

    def for_membership(self, membership_id: int) -> list[Sink]:
        """Open wait() sinks of one membership (parks belong to a session)."""
        return [s for s in self._open.values() if s.membership_id == membership_id and s.kind != "park"]

    def parks(self) -> list[Sink]:
        return [s for s in self._open.values() if s.kind == "park"]

    def for_conn(self, conn_id: int) -> list[Sink]:
        return [s for s in self._open.values() if s.conn_id == conn_id]

    def find_wait(self, participant_id: int, wait_id: str) -> Sink | None:
        for s in list(self._open.values()) + list(self._recent.values()):
            if s.participant_id == participant_id and s.wait_id == wait_id:
                return s
        return None

    def close(self, sink_id: int, result: dict[str, Any] | None, reason: str) -> Sink | None:
        """Close an open sink with ``result`` (None: nobody is told anything)."""
        s = self._open.pop(sink_id, None)
        if s is None:
            return None
        s.closed = True
        s.close_reason = reason
        s.result = result
        self._recent[s.id] = s
        while len(self._recent) > self.RECENT_MAX:
            self._recent.popitem(last=False)
        return s
