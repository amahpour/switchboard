"""The broker side of a room's whiteboard (DESIGN.md §56).

One board per room. An agent's ``board`` tool and a signed-in page's ``board`` frame both land
here. The merge is pure (``switchboard.board``); the rows are ``store.py``. A change that is
kept posts one notice that names who, not what was drawn, and the hub fans the new elements out.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import secrets
from typing import Any

from switchboard import board, db
from switchboard.broker.agent_icons import PNG_SIGNATURE, IconError, png_size
from switchboard.broker.service import ServiceError
from switchboard.envelope import clean, sanitize
from switchboard.models import Room
from switchboard.store import Conflict


class RoomBoard:
    def __init__(self, state: Any) -> None:
        self.state = state
        self._converting: set[int] = set()
        self._pending: dict[str, tuple[int, int, asyncio.Future[Any], str]] = {}

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

    async def convert(self, room: Room, source: str, participant: int) -> list[Any]:
        """Conversion runs only in an open board, with one request per agent and a global cap."""
        if participant in self._converting or len(self._pending) >= 16:
            raise ServiceError("busy", "a board conversion is already pending")
        request = secrets.token_hex(16)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request] = (room.id, 0, future, "mermaid")
        self._converting.add(participant)
        try:
            self.state.hub.room_board(
                room.name, {"task": {"id": request, "kind": "mermaid", "source": source}}
            )
            try:
                result = await asyncio.wait_for(future, 8.0)
            except TimeoutError as e:
                raise ServiceError("no_board_page", "open this room's whiteboard, then ask again") from e
            if isinstance(result, dict):
                raise ServiceError("bad_request", "cannot convert this flow within the board limits")
            return result
        finally:
            self._pending.pop(request, None)
            self._converting.discard(participant)

    def mermaid_result(self, room: Room, request: Any, result: Any) -> bool:
        if not isinstance(request, str):
            return False
        pending = self._pending.get(request)
        if pending is None or pending[0] != room.id or pending[3] != "mermaid" or pending[2].done():
            return False
        if result == {"error": "cannot_convert"}:
            pending[2].set_result(result)
            return True
        try:
            board.mermaid_elements([], result, now=self.state.clock.now())
        except board.Reject:
            return False
        pending[2].set_result(result)
        return True

    def imported(self, room: Room, actor: str, participant: int, raw: list[Any]) -> dict[str, Any]:
        _, current = self.state.store.board_load(room.id)
        try:
            elements = board.mermaid_elements(current, raw, now=self.state.clock.now())
        except board.Reject as e:
            raise ServiceError("bad_request", e.reason) from e
        return self._commit(
            room, actor, participant, action="draw_mermaid", params=None, raw=elements, agent_raw=True
        )

    def snapshot(self, room: Room) -> dict[str, Any]:
        """The whole board, for a page that just subscribed."""
        seq, elements = self.state.store.board_load(room.id)
        return {"seq": seq, "elements": elements, "full": True}

    def agent(
        self, room: Room, actor: str, params: dict[str, Any], participant_id: int | None
    ) -> dict[str, Any]:
        """One ``board`` call from a member. ``participant_id`` shares the say rate limit when set
        and the call changed the board."""
        action = params.get("action")
        if not isinstance(action, str) or action not in board.ACTIONS:
            raise ServiceError("bad_request", "unknown action")
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
        raw: list[Any] | None,
        agent_raw: bool = False,
    ) -> dict[str, Any]:
        who = clean(actor).strip()[:100] or "someone"
        now = self.state.clock.now()
        published: dict[str, Any] | None = None
        added: tuple[str, ...] = ()
        with db.tx(self.state.store.con):
            seq, elements = self.state.store.board_load(room.id)
            if raw is None:
                assert action is not None and params is not None
                outcome = board.apply_action(elements, action, params, now=now)
                if outcome.error:
                    raise ServiceError("bad_request", outcome.error)
                incoming: list[Any] = list(outcome.incoming)
                added = outcome.added
                agent = True
            else:
                incoming = raw
                agent = agent_raw
                if agent_raw:
                    added = tuple(e["id"] for e in incoming)
            result = board.merge(elements, incoming, agent=agent)
            if agent_raw and (len(result.taken) != len(incoming) or result.rejected):
                raise ServiceError("bad_request", "the diagram does not fit on this board")
            if not result.taken:
                why = result.rejected[0] if result.rejected else "the board kept a newer copy of that shape"
                if raw is not None:
                    return {"seq": seq, "changed": False, "ids": []}
                raise ServiceError("bad_request", why)
            try:
                self.state.store.board_write(
                    room.id, seq + 1, list(result.elements), who, [e["id"] for e in result.taken]
                )
            except Conflict as e:
                if raw is not None:
                    return {"seq": seq, "changed": False, "ids": []}
                raise ServiceError("bad_request", "board is full") from e
            if participant_id is not None:
                self.state.store.update_participant(participant_id, last_say_at=now)
            published = {
                "seq": seq + 1,
                "elements": list(result.taken),
                "actor": who,
                "ids": [e["id"] for e in result.taken],
                "full": False,
            }
        # The notice is its own write: a failure here leaves the board saved and the log a line short,
        # and it never carries the shape text (that text would otherwise be a system line).
        self.state.service.post_notice(room, f"{who} changed the board", level="info")
        assert published is not None
        self.state.hub.room_board(room.name, published)
        ids = list(added) or published["ids"]
        return {"seq": published["seq"], "changed": True, "ids": ids, "text": _done(action, ids)}


def _done(action: str | None, ids: list[str]) -> str:
    if not ids:
        return "ok"
    if action == "delete":
        return f"deleted {ids[0]}"
    if action in ("move", "edit"):
        return f"updated {ids[0]}"
    if action is None:
        return "ok"
    return f"added {ids[0]}"
