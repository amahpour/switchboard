"""Runner: the async side of the delivery engine (DESIGN.md §8.2).

It carries out the engine's actions (push through the adapter's transport,
resolve sink futures, publish notices and buddy-list snapshots) and ticks
the engine every second. Everything that needs asyncio lives here; the
engine stays synchronous.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from switchboard.adapters.base import SendError
from switchboard.models import Action, Notice, Push, ResolveSink, Snapshot

if TYPE_CHECKING:  # pragma: no cover
    from switchboard.broker.app import BrokerState

log = logging.getLogger("switchboard.runner")

TICK_S = 1.0


class Runner:
    def __init__(self, state: "BrokerState", tick_s: float = TICK_S):
        self.state = state
        self.tick_s = tick_s
        self.futures: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

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
                elif isinstance(a, Push):
                    self._spawn(self._push(a))
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

    async def _push(self, a: Push) -> None:
        st = self.state
        p = st.store.get_participant(a.participant_id)
        b = st.store.get_batch(a.batch_id)
        if p is None or b is None or b.state != "offered" or b.posted_at is not None:
            return  # cancelled (/pause) or settled before the transport got it
        adapter = st.engine.adapter(p)
        # Handed to the transport = posted: a frame on its way can't be recalled,
        # so /pause no longer cancels it (DESIGN §8.5), and the idle expiry
        # counts from here. mcp.posted refines the time when it reports one.
        st.store.mark_posted(a.batch_id)
        try:
            t_post = await adapter.send(p, b, a.text, room=a.room, sender=a.sender)
        except Exception as e:
            # SendError messages are fixed strings or an exception type name (no text, no paths)
            why = e.reason if isinstance(e, SendError) else type(e).__name__
            cur = st.store.get_batch(a.batch_id)
            if cur is not None and cur.state != "offered":
                # already confirmed (the frame landed) or settled: nothing to take back
                log.info(
                    "push of batch %d: no post report (%s); batch already %s", a.batch_id, why, cur.state
                )
                return
            if isinstance(e, SendError) and not e.counted:
                # a re-route (e.g. a Codex steer after the turn ended): back to
                # pending without counting as a push failure; re-evaluated at once
                log.info("push of batch %d re-routed: %s", a.batch_id, why)
                self.execute(st.engine.on_expire(a.batch_id, "reroute", count_failure=False))
                return
            log.warning("push of batch %d failed: %s", a.batch_id, why)
            self.execute(st.engine.on_expire(a.batch_id, "send_error"))
            return
        if t_post is not None:
            # the MCP server's clock reading, bounded: never before the batch, never
            # in the future (a far-future posted_at would stop the idle expiry)
            t_post = min(max(t_post, b.created_at), st.clock.now() + 1.0)
            st.store.refine_posted(a.batch_id, t_post)
        if a.rules_version:
            st.store.mark_rules_seen(b.membership_id, a.rules_version)

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

    def _spawn(self, coro: Any) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # --------------------------------------------------------------- tick
    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.tick_s)
            try:
                self.execute(self.engine.tick())
            except Exception:
                log.exception("engine tick failed")

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        for fut in self.futures.values():
            if not fut.done():
                fut.set_result({"status": "cancelled"})
        self.futures.clear()
