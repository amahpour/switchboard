"""The broker side of a room's whiteboard (DESIGN.md §56).

One board per room. An agent's ``board`` tool and a signed-in page's ``board`` frame both land
here. The merge is pure (``switchboard.board``); the rows are ``store.py``. A change that is
kept posts one notice that names who, not what was drawn, and the hub fans the new elements out.
"""

from __future__ import annotations

from typing import Any

from switchboard import board, db
from switchboard.broker.service import ServiceError
from switchboard.envelope import clean, sanitize
from switchboard.models import Room
from switchboard.store import Conflict


class RoomBoard:
    def __init__(self, state: Any) -> None:
        self.state = state

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
                agent = False
            result = board.merge(elements, incoming, agent=agent)
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
