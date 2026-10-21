"""Runner: the async side of the delivery engine (DESIGN.md §8.2).

It carries out the engine's actions (resolve sink futures, publish notices and
buddy-list snapshots) and ticks the engine every second. Everything that needs asyncio lives here; the
engine stays synchronous.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from switchboard.models import Action, Notice, ResolveSink, Snapshot

if TYPE_CHECKING:  # pragma: no cover
    from switchboard.broker.app import BrokerState

log = logging.getLogger("switchboard.runner")

TICK_S = 1.0


class Runner:
    def __init__(self, state: "BrokerState", tick_s: float = TICK_S):
        self.state = state
        self.tick_s = tick_s
        self.futures: dict[int, asyncio.Future[dict[str, Any]]] = {}

    @property
    def engine(self) -> Any:
        return self.state.engine

    # ------------------------------------------------------------ actions
    def execute(self, actions: Iterable[Action]) -> None:
        snaps: list[int] = []
        for a in actions:
            try:
                if isinstance(a, ResolveSink):
                    self._resolve(a)
                elif isinstance(a, Notice):
                    self._notice(a)
                elif isinstance(a, Snapshot):
                    if a.room_id not in snaps:
                        snaps.append(a.room_id)
            except Exception:  # pragma: no cover - never let one action stop the rest
                log.exception("action %s failed", type(a).__name__)
        for room_id in snaps:
            self._snapshot(room_id)

    def future_for(self, sink_id: int) -> asyncio.Future[dict[str, Any]]:
        """The future a wait() RPC awaits. If the engine already resolved the
        sink (it was filled on open), the future is already done."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        s = self.engine.sinks.get(sink_id)
        if s is not None and s.closed:
            fut.set_result(s.result or {"status": "cancelled"})
        else:
            self.futures[sink_id] = fut
        return fut

    def drop_future(self, sink_id: int) -> None:
        self.futures.pop(sink_id, None)

    def _resolve(self, a: ResolveSink) -> None:
        fut = self.futures.pop(a.sink_id, None)
        if fut is not None and not fut.done():
            fut.set_result(a.result)

    def _notice(self, a: Notice) -> None:
        st = self.state
        room = st.store.room_by_id(a.room_id) if a.room_id is not None else None
        if a.room_id is not None and (room is None or room.closed):
            # its room was deleted or closed (§28): nobody reads it, and it is no broker-wide news
            log.info("notice for room %s dropped (closed or deleted): %s", a.room_id, a.text)
            return
        if room is not None and a.persist:
            # The room message is the one line clients show; it carries the level
            # for styling. A second, transient frame would print the notice twice.
            st.service.post_notice(room, a.text, level=a.level)
            return
        st.hub.notice(room.name if room else None, a.level, a.text)

    def _snapshot(self, room_id: int) -> None:
        st = self.state
        room = st.store.room_by_id(room_id)
        if room is None:
            return
        st.hub.members_changed(room.name)
        st.hub.settings_changed(room.name)

    # --------------------------------------------------------------- tick
    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.tick_s)
            try:
                self.execute(self.engine.tick())
            except Exception:
                log.exception("engine tick failed")

    async def stop(self) -> None:
        for fut in self.futures.values():
            if not fut.done():
                fut.set_result({"status": "cancelled"})
        self.futures.clear()
