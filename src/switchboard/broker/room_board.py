"""The broker side of a room's whiteboard (DESIGN.md §56).

One board per room. An agent's ``board`` tool and a signed-in page's ``board`` frame both land
here. The merge is pure (``switchboard.board``); the rows are ``store.py``. A change that is
kept posts one notice that names who, not what was drawn, and the hub fans the new elements out.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import secrets
from collections.abc import Callable
from typing import Any

from switchboard import board, board_checks, board_flow, db
from switchboard.broker.agent_icons import PNG_SIGNATURE, IconError, png_size
from switchboard.broker.service import ServiceError
from switchboard.delivery import rules
from switchboard.envelope import clean, sanitize
from switchboard.models import Room
from switchboard.store import Conflict

log = logging.getLogger(__name__)

MAX_CONVERSIONS = 4  # drawings being converted at once, for all rooms


class RoomBoard:
    def __init__(self, state: Any) -> None:
        self.state = state
        self._converting: set[int] = set()
        self._budget_full_at: dict[int, float] = {}
        self._pending: dict[str, tuple[int, int, asyncio.Future[Any], str]] = {}
        # the open burst of changes per room, and the timer that closes it once it goes quiet
        self._bursts: dict[int, tuple[Room, tuple[str, int, float]]] = {}
        self._timers: dict[int, asyncio.TimerHandle] = {}

    def _note_change(self, room: Room, who: str, now: float) -> None:
        """Count a saved change in its room's burst; post the notice of the one it closed (§56.3)."""
        held = self._bursts.get(room.id)
        closed, current = rules.board_burst_step(held[1] if held else None, who, now)
        if closed is not None:
            self.state.service.post_notice(room, rules.board_burst_text(closed), level="info")
        self._bursts[room.id] = (room, current)
        self._arm(room.id, rules.BOARD_NOTICE_GAP_S)

    def _arm(self, room_id: int, delay: float) -> None:
        timer = self._timers.pop(room_id, None)
        if timer is not None:
            timer.cancel()
        self._timers[room_id] = asyncio.get_running_loop().call_later(delay, self._wake, room_id)

    def _wake(self, room_id: int) -> None:
        self._timers.pop(room_id, None)
        try:
            self.flush_due()
        except Exception:  # pragma: no cover - a failed notice must not kill the loop callback
            log.exception("board notice failed")
        held = self._bursts.get(room_id)
        if held is not None:  # written to while the timer ran: wait out what is left of its gap
            left = rules.BOARD_NOTICE_GAP_S - (self.state.clock.now() - held[1][2])
            self._arm(room_id, max(left, 0.05))

    def flush_due(self) -> None:
        """Post the notice of every burst that has been quiet for the gap."""
        now = self.state.clock.now()
        for room_id, (room, burst) in list(self._bursts.items()):
            if rules.board_burst_due(burst, now):
                del self._bursts[room_id]
                self.state.service.post_notice(room, rules.board_burst_text(burst), level="info")

    def close(self) -> None:
        """At shutdown: stop the timers and post what is still open, so nothing is lost or left pending."""
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        bursts, self._bursts = list(self._bursts.values()), {}
        for room, burst in bursts:
            with contextlib.suppress(Exception):
                self.state.service.post_notice(room, rules.board_burst_text(burst), level="info")

    def check_budget(self, participant: int) -> float | None:
        """Seconds until this agent may change the board (§56.3), without spending its budget."""
        now = self.state.clock.now()
        # Only outstanding debt matters. Drop old sessions once the map grows, without a timer.
        if len(self._budget_full_at) >= 1000:
            self._budget_full_at = {p: at for p, at in self._budget_full_at.items() if at > now}
        _, retry = rules.board_budget(self._budget_full_at.get(participant), now)
        return retry

    async def image(self, room: Room) -> dict[str, Any]:
        """Ask an already-open board to rasterize. No browser is launched by the broker."""
        if len(self._pending) >= 16:
            raise ServiceError("busy", "too many board picture requests")
        seq, _elements = self.state.store.board_load(room.id)
        request = secrets.token_hex(16)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request] = (room.id, seq, future, "image")
        try:
            self.state.hub.room_board(room.name, {"task": {"id": request, "kind": "image", "seq": seq}})
            try:
                data = await asyncio.wait_for(future, 4.0)
            except TimeoutError as e:
                raise ServiceError(
                    "no_board_page", "open this room's whiteboard in a browser, then ask again"
                ) from e
            current, _elements = self.state.store.board_load(room.id)
            if current != seq:
                raise ServiceError("board_changed", "the board changed while drawing its picture; ask again")
            return {"image": data, "seq": seq}
        finally:
            self._pending.pop(request, None)

    def image_result(self, room: Room, request: Any, result: Any) -> bool:
        """Only a bounded PNG for this room's outstanding request may answer an image read."""
        if (
            not isinstance(request, str)
            or len(request) != 32
            or not isinstance(result, str)
            or len(result) > 800000
        ):
            return False
        pending = self._pending.get(request)
        if pending is None or pending[0] != room.id or pending[2].done() or pending[3] != "image":
            return False
        try:
            raw = base64.b64decode(result, validate=True)
            if not raw.startswith(PNG_SIGNATURE) or len(raw) > 600000:
                return False
            width, height = png_size(raw)
            if max(width, height) > 1200:
                return False
        except (ValueError, binascii.Error, IconError):
            return False
        pending[2].set_result(result)
        return True

    async def convert(self, source: Any, participant: int) -> board_flow.Converted:
        """The parse, layout and routing of a diagram, off the event loop: a dense diagram is real work,
        and nothing else in the broker may wait for it. One conversion per agent and a few at once."""
        if participant in self._converting or len(self._converting) >= MAX_CONVERSIONS:
            raise ServiceError("busy", "a board drawing is already being made; ask again in a moment")
        self._converting.add(participant)
        try:
            return await asyncio.to_thread(board_flow.convert, source)
        except board.Reject as e:
            raise ServiceError("bad_request", e.reason) from e
        finally:
            self._converting.discard(participant)

    def drawn(self, room: Room, actor: str, participant: int, flow: board_flow.Converted) -> dict[str, Any]:
        """A converted flowchart as one change. The caller has just rechecked membership and the rate
        limit; the board is loaded, and the diagram placed clear of it, inside the transaction, so a
        change that landed while the conversion ran is respected (§56.9)."""
        now = self.state.clock.now()

        def place(current: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return board.mermaid_elements(current, flow.elements, now=now)

        return self._commit(
            room,
            actor,
            participant,
            action="draw_mermaid",
            params=None,
            raw=place,
            agent_raw=True,
            summary=flow.summary,
        )

    def snapshot(self, room: Room) -> dict[str, Any]:
        """The whole board, for a page that just subscribed."""
        seq, elements = self.state.store.board_load(room.id)
        return {"seq": seq, "elements": elements, "full": True}

    def agent(
        self, room: Room, actor: str, params: dict[str, Any], participant_id: int | None
    ) -> dict[str, Any]:
        """One ``board`` call from a member. A saved change spends its participant's board budget."""
        action = params.get("action")
        if not isinstance(action, str) or action not in board.ACTIONS:
            raise ServiceError("bad_request", f"unknown action: use one of {', '.join(board.ACTIONS)}")
        if action == "read":
            seq, elements = self.state.store.board_load(room.id)
            return {"seq": seq, "text": sanitize(board.summary(elements, seq), limit=None)}
        return self._commit(room, actor, participant_id, action=action, params=params, raw=None)

    def from_page(self, room: Room, actor: str, elements: list[Any]) -> dict[str, Any]:
        """Elements a signed-in person sent. The caller has already checked they can see the room."""
        return self._commit(room, actor, None, action=None, params=None, raw=elements)

    def _commit(
        self,
        room: Room,
        actor: str,
        participant_id: int | None,
        *,
        action: str | None,
        params: dict[str, Any] | None,
        raw: list[Any] | Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None,
        agent_raw: bool = False,
        summary: str = "",
    ) -> dict[str, Any]:
        from_page = raw is not None and not callable(raw) and not agent_raw
        who = clean(actor).strip()[:100] or "someone"
        now = self.state.clock.now()
        published: dict[str, Any] | None = None
        added: tuple[str, ...] = ()
        problems: tuple[str, ...] = ()
        with db.tx(self.state.store.con):
            seq, elements = self.state.store.board_load(room.id)
            if raw is None:
                assert action is not None and params is not None
                outcome = board.apply_action(elements, action, params, now=now)
                if outcome.error:
                    raise ServiceError("bad_request", outcome.error)
                incoming: list[Any] = list(outcome.incoming)
                added = outcome.added
                problems = outcome.warnings
                agent = True
            else:
                if callable(raw):
                    try:
                        raw = raw(elements)
                    except board.Reject as e:
                        raise ServiceError("bad_request", e.reason) from e
                incoming = raw
                agent = agent_raw
                if agent_raw:
                    added = tuple(e["id"] for e in incoming)
            big = board.MAX_DIAGRAM if agent_raw or action == "arrange" else None
            result = board.merge(elements, incoming, agent=agent, cap=big)
            if agent_raw and (len(result.taken) != len(incoming) or result.rejected):
                raise ServiceError("bad_request", "the diagram does not fit on this board")
            dropped = len(result.rejected)
            kept = {e["id"] for e in result.taken}
            refused = [
                e["id"]
                for e in incoming
                if isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"] not in kept
            ][: board.MAX_CALL * 4]
            if not result.taken:
                why = result.rejected[0] if result.rejected else "the board kept a newer copy of that shape"
                if raw is not None:
                    return {
                        "seq": seq,
                        "changed": False,
                        "ids": [],
                        **({"dropped": dropped, "refused": refused} if from_page else {}),
                    }
                raise ServiceError("bad_request", why)
            try:
                self.state.store.board_write(
                    room.id, seq + 1, list(result.elements), who, [e["id"] for e in result.taken]
                )
            except Conflict as e:
                if raw is not None:
                    return {"seq": seq, "changed": False, "ids": []}
                raise ServiceError("bad_request", "board is full") from e
            if agent_raw:  # a whole diagram: its result names what is wrong with it, like a shape action's
                problems = tuple(
                    board_checks.warnings(list(result.elements), only={e["id"] for e in result.taken})
                )
            published = {
                "seq": seq + 1,
                "elements": list(result.taken),
                "actor": who,
                "ids": [e["id"] for e in result.taken],
                "full": False,
            }
        if participant_id is not None:
            full_at, _ = rules.board_budget(self._budget_full_at.get(participant_id), now)
            self._budget_full_at[participant_id] = full_at
        # The notice is its own write, posted when the actor's burst ends (§56.3): a failure here leaves
        # the board saved and the log a line short, and it never carries the shape text.
        self._note_change(room, who, now)
        assert published is not None
        self.state.hub.room_board(room.name, published)
        ids = list(added) or published["ids"]
        text = summary or _done(action, ids)
        if problems:
            text += "\nlayout warnings, fix before announcing:\n" + "\n".join(f"- {w}" for w in problems)
        done = {"seq": published["seq"], "changed": True, "ids": ids, "text": text}
        if from_page:  # what the page asked for that was not kept: the socket answers it with the board
            done["dropped"] = dropped
            done["refused"] = refused
        return done


def _done(action: str | None, ids: list[str]) -> str:
    if not ids:
        return "ok"
    if action == "delete":
        return f"deleted {ids[0]}"
    if action == "arrange":
        return f"arranged {ids[0]}"
    if action in ("move", "edit", "resize"):
        return f"updated {ids[0]}"
    if action is None:
        return "ok"
    return f"added {ids[0]}"
