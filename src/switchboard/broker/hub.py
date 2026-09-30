"""Fan-out to WebSocket clients and UDS tail subscribers (DESIGN.md §5.5).

Publishing is synchronous (``put_nowait`` into per-subscriber queues), so a
message committed to SQLite and published in the same event-loop step can't
race a subscriber that is registering and reading its backlog.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger("switchboard.hub")

QUEUE_MAX = 2000
WS_QUEUE_MAX = 40000
MEMBERS_DEBOUNCE_S = 0.2


class Subscriber:
    """Base class. ``rooms`` is the set of room names this subscriber follows."""

    kinds: frozenset[str] = frozenset({"msg", "members", "room", "notice", "rooms"})

    def __init__(self) -> None:
        self.rooms: set[str] = set()
        self.q: asyncio.Queue[Any] = asyncio.Queue(maxsize=QUEUE_MAX)
        self.closed = False
        self.sid_hash: str | None = None

    def wants(self, kind: str, room: str | None) -> bool:
        if self.closed or kind not in self.kinds:
            return False
        return room is None or room in self.rooms

    def format(self, kind: str, room: str | None, data: dict[str, Any]) -> Any:
        raise NotImplementedError

    def offer(self, item: Any) -> bool:
        if self.closed:
            return False
        try:
            self.q.put_nowait(item)
            return True
        except asyncio.QueueFull:
            log.warning("subscriber queue full; dropping subscriber")
            self.close()
            return False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.q.put_nowait(None)
            except asyncio.QueueFull:
                # make room for the sentinel
                try:
                    self.q.get_nowait()
                    self.q.put_nowait(None)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass


class WsSubscriber(Subscriber):
    """A browser WebSocket. Frames are the §5.5 server->client shapes, plus ``remotes``:
    every remote link's state, for the header chips and the remotes panel (§27.11), and
    ``machines``: every machine that dials in (§31.7)."""

    kinds: frozenset[str] = Subscriber.kinds | {"remotes", "machines"}

    def __init__(self) -> None:
        super().__init__()
        # a hello may replay up to 500 lines for each of 64 rooms
        self.q = asyncio.Queue(maxsize=WS_QUEUE_MAX)

    def format(self, kind: str, room: str | None, data: dict[str, Any]) -> Any:
        if kind in ("rooms", "remotes", "machines"):
            return {"t": kind, **data}
        return {"t": kind, "room": room, **data}

    async def run_sender(self, send_text: Callable[[str], Any], close: Callable[[int], Any]) -> None:
        try:
            while True:
                item = await self.q.get()
                if item is None:
                    await close(1008)
                    return
                await send_text(json.dumps(item, ensure_ascii=False))
        except asyncio.CancelledError:
            raise
        except Exception:  # client went away
            self.closed = True


class Hub:
    def __init__(self) -> None:
        self.subs: set[Subscriber] = set()
        self._members_pending: dict[str, asyncio.TimerHandle] = {}
        self._members_fn: Callable[[str], list[dict[str, Any]] | None] | None = None
        self._settings_pending: dict[str, asyncio.TimerHandle] = {}
        self._settings_fn: Callable[[str], dict[str, Any] | None] | None = None

    # registration ----------------------------------------------------------
    def add(self, sub: Subscriber) -> None:
        self.subs.add(sub)

    def remove(self, sub: Subscriber) -> None:
        self.subs.discard(sub)
        sub.close()

    def ws_count(self) -> int:
        return sum(1 for s in self.subs if isinstance(s, WsSubscriber) and not s.closed)

    # publishing ------------------------------------------------------------
    def publish(self, kind: str, room: str | None, data: dict[str, Any]) -> int:
        n = 0
        for sub in list(self.subs):
            if sub.closed:
                self.subs.discard(sub)
                continue
            if sub.wants(kind, room) and sub.offer(sub.format(kind, room, data)):
                n += 1
        return n

    def message(self, room: str, msg: dict[str, Any]) -> int:
        return self.publish("msg", room, {"msg": msg})

    def notice(self, room: str | None, level: str, text: str) -> int:
        return self.publish("notice", room, {"level": level, "text": text})

    def room_settings(self, room: str, settings: dict[str, Any]) -> int:
        return self.publish("room", room, {"settings": settings})

    def rooms_changed(self, names: list[str]) -> int:
        return self.publish("rooms", None, {"rooms": names})

    def remotes_changed(self, remotes: list[dict[str, Any]], config_error: str | None = None) -> int:
        """Every remote's state (``RemoteManager.summary()``), to the web UI only: the
        UDS tail subscribers never ask for this kind."""
        return self.publish("remotes", None, {"remotes": remotes, "config_error": config_error})

    def set_members_source(self, fn: Callable[[str], list[dict[str, Any]] | None]) -> None:
        self._members_fn = fn

    def members_changed(self, room: str) -> None:
        """Debounced (200 ms) full buddy-list snapshot for ``room``."""
        if room in self._members_pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop (unit tests): publish right away
            self._flush_members(room)
            return
        self._members_pending[room] = loop.call_later(
            MEMBERS_DEBOUNCE_S, self._flush_members, room
        )

    def _flush_members(self, room: str) -> None:
        self._members_pending.pop(room, None)
        if self._members_fn is None:
            return
        members = self._members_fn(room)
        if members is not None:
            self.publish("members", room, {"members": members})

    def set_settings_source(self, fn: Callable[[str], dict[str, Any] | None]) -> None:
        self._settings_fn = fn

    def settings_changed(self, room: str) -> None:
        """Debounced (200 ms) room settings frame (paused, budget, hops) for ``room``."""
        if room in self._settings_pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._flush_settings(room)
            return
        self._settings_pending[room] = loop.call_later(
            MEMBERS_DEBOUNCE_S, self._flush_settings, room
        )

    def _flush_settings(self, room: str) -> None:
        self._settings_pending.pop(room, None)
        if self._settings_fn is None:
            return
        settings = self._settings_fn(room)
        if settings is not None:
            self.room_settings(room, settings)

    def drop_room(self, room: str) -> None:
        """``room`` was closed or deleted (DESIGN.md §28): nobody follows that name any more,
        and its debounced members/settings frames are cancelled. A room created again under
        the name never streams into an old web page or ``tail``: web pages subscribe again
        after the ``rooms`` frame, an old ``tail`` goes quiet after the close notice."""
        for sub in self.subs:
            sub.rooms.discard(room)
        for pending in (self._members_pending, self._settings_pending):
            h = pending.pop(room, None)
            if h is not None:
                h.cancel()

    # sessions --------------------------------------------------------------
    def close_sessions(self, sid_hash: str | None = None) -> int:
        """Close WebSockets of one session (or of all sessions when None)."""
        n = 0
        for sub in list(self.subs):
            if isinstance(sub, WsSubscriber) and (sid_hash is None or sub.sid_hash == sid_hash):
                self.remove(sub)
                n += 1
        return n

    def close_all(self) -> None:
        for h in list(self._members_pending.values()) + list(self._settings_pending.values()):
            h.cancel()
        self._members_pending.clear()
        self._settings_pending.clear()
        for sub in list(self.subs):
            self.remove(sub)
