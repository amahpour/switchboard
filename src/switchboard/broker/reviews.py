"""The broker side of a review board (DESIGN.md §37): applies agents' moves through the
``review`` tool, keeps the rows, and tells the room in one short line per move.

The rules are in ``switchboard.reviews`` (pure); the SQL in ``store.py``. A move that hands the
next step to another agent (``reviews.handoff``: a raise, a concession to another owner, a fix)
comes back with the line that wakes them, which ``AgentService.review`` posts as the mover's own
message; any other move is a notice, which wakes no one. The board is read on demand
(``review(action="show")``) or, for a person, in the web UI.
"""

from __future__ import annotations

import logging
from typing import Any

from switchboard import reviews
from switchboard.models import Room
from switchboard.reviews import ReviewError

log = logging.getLogger(__name__)

ACTIONS = ("open", "show", "raise", "ask", "concede", "contest", "fix", "confirm", "drop")


class Boards:
    def __init__(self, state: Any):
        self.state = state

    @property
    def store(self) -> Any:
        return self.state.store

    # ----------------------------------------------------------------- reads
    def board(self, room: Room) -> dict[str, Any] | None:
        """The room's board as data: the review, its items and whether it's settled."""
        rv = self.store.current_review(room.id)
        if rv is None:
            return None
        items = self.store.review_items(rv["id"])
        active = set(self.store.active_names(room.id))
        now = self.state.clock.now()
        f = reviews.forge(rv["url"])
        events = [{"ts": e.ts, "data": e.data} for e in self.store.review_events(room.id, rv["opened_at"])]
        return {
            "id": rv["id"],
            "url": rv["url"],
            "forge": f,
            "head": rv["head"],
            "opened_by": rv["opened_by"],
            "opened_at": rv["opened_at"],
            "settled": reviews.settled(items),
            "counts": reviews.counts(items, active, now),
            "items": [item_dict(i, f, events, active, now) for i in items],
            "plan": {owner: [i.label for i in its] for owner, its in reviews.post_plan(items).items()},
            "posted_at": rv["posted_at"],
            "posted_by": rv["posted_by"],
        }

    # ------------------------------------------------------------ agent moves
    def agent(self, room: Room, actor: str, params: dict[str, Any]) -> dict[str, Any]:
        """One ``review`` tool call from the member ``actor`` of ``room``. Returns the board as
        text (what the agent reads), with ``handoff`` (who must act next, and the line that tells
        them) when the move is someone else's to answer; or raises ReviewError with what to fix."""
        action = params.get("action")
        if action not in ACTIONS:
            raise ReviewError(f"action must be one of: {', '.join(ACTIONS)}")
        if action == "open":
            return self._open(room, actor, params)
        rv = self.store.current_review(room.id)
        if rv is None:
            raise ReviewError(f'{room.name} has no review board: open one with action="open" and the url')
        if action == "show":
            return self._shown(room)
        if action in ("raise", "ask"):
            item = self._add(rv, actor, action, params)
            verb = "raised" if item.kind == "finding" else "asked"
            return self._told(room, item, actor, f"{actor} {verb} {item.label}: {item.title}")
        kind, n = reviews.parse_label(params.get("item"))
        item = self.store.review_item(rv["id"], kind, n)
        if item is None:
            raise ReviewError(f"{room.name}'s board has no {('F' if kind == 'finding' else 'Q')}{n}")
        to = reviews.agent_move(item, action, actor)
        fields: dict[str, Any] = {}
        if action == "concede":
            owner = reviews.text(params.get("owner"), "owner", 100) or actor
            if owner not in self.store.active_names(room.id):
                raise ReviewError(f"owner must be an agent in {room.name}: {owner!r} isn't")
            fields["owner"] = owner
        elif action == "fix":
            commit = reviews.head(params.get("commit"))
            if not commit:
                raise ReviewError("fix needs the commit that fixes it")
            fields["commit_sha"] = commit
            fields["proof"] = reviews.proof(params.get("proof"))
            fields["reason"] = ""  # a conceded contest's reason no longer describes the fix
        elif action == "confirm":
            fields["confirmed_by"] = actor
        elif action in ("contest", "drop"):
            fields["reason"] = reviews.text(params.get("reason"), "reason", reviews.MAX_REASON, required=True)
        self.store.move_review_item(item.id, to, **fields)
        self.store.add_event(
            "review",
            room_id=room.id,
            data={"item": item.label, "to": "confirmed" if action == "confirm" else to, "by": actor},
        )
        tail = {
            "conceded": f", owner {fields.get('owner')}",
            "fixed": f" in {fields.get('commit_sha', '')[:12]}",
            "contested": ": a person decides",
            "confirmed": f" by {actor}",
        }.get("confirmed" if action == "confirm" else to, "")
        moved = self.store.review_item(rv["id"], kind, n)
        assert moved is not None
        return self._told(room, moved, actor, f"{actor} {action_past(action)} {item.label}{tail}")

    def _told(self, room: Room, item: reviews.Item, actor: str, notice: str) -> dict[str, Any]:
        """The board after ``actor``'s move on ``item``, and how the room hears of it: the line
        for whoever acts next (posted by the caller as the mover's message, so it wakes them),
        or the notice when that's nobody (§37.4)."""
        handoff = reviews.handoff(item, actor, self.store.active_names(room.id))
        if handoff is None:
            self._notice(room, notice)
        out = self._shown(room, touched=item.label)
        if handoff is not None:
            out["handoff"] = {"mentions": list(handoff[0]), "text": handoff[1]}
        return out

    def _open(self, room: Room, actor: str, params: dict[str, Any]) -> dict[str, Any]:
        url = reviews.url(params.get("url"))
        head = reviews.head(params.get("head"))
        self._open_board(room, actor, url, head, "a person closes it before another opens")
        return self._shown(room)

    def _open_board(self, room: Room, by: str, url: str, head: str, then: str) -> None:
        """The room's board for ``url``, opened by ``by`` (an agent or a person), or the open one
        when it's for ``url`` already. Another URL is refused until that board is closed."""
        rv = self.store.current_review(room.id)
        if rv is not None and rv["url"] != url:
            raise ReviewError(f"{room.name} already has a board for {rv['url']}: {then}")
        if rv is None:
            self.store.open_review(room.id, url, head, by)
            self._notice(room, f"{by} opened a review board for {url}" + (f" at {head[:12]}" if head else ""))
        elif head and head != rv["head"]:
            # a new head: the same board, its checks now about a newer commit (§37.4)
            self.store.set_review(rv["id"], head=head)
            self._notice(room, f"{by} moved the review board to {head[:12]}")
        self.store.add_event("review", room_id=room.id, data={"open": url, "head": head, "by": by})

    def person_open(self, room: Room, name: str, url: str) -> None:
        """A person's ``/review`` (§37.8): the board opens, or stays, for ``url``, and every page
        showing the room gets it at once."""
        self._open_board(room, name, reviews.url(url), "", "close it before another opens")
        self.state.hub.review(room.name, self.board(room))

    def _add(self, rv: dict[str, Any], actor: str, action: str, params: dict[str, Any]) -> reviews.Item:
        title = reviews.text(params.get("title"), "title", reviews.MAX_TITLE, required=True)
        detail = reviews.text(params.get("detail"), "detail", reviews.MAX_DETAIL)
        if action == "raise":
            if "where" in params and ("file" in params or "lines" in params):
                raise ReviewError("use where or file and lines, not both")
            file, lines = (
                reviews.where(params["where"])
                if "where" in params
                else (
                    reviews.text(params.get("file"), "file", reviews.MAX_FILE),
                    reviews.lines(params.get("lines")),
                )
            )
            return self.store.add_review_item(
                rv["id"],
                "finding",
                "raised",
                title=title,
                detail=detail,
                file=file,
                lines=lines,
                raised_by=actor,
            )
        opts = reviews.options(params.get("options"))
        return self.store.add_review_item(
            rv["id"],
            "question",
            "open",
            title=title,
            detail=detail,
            raised_by=actor,
            options=opts,
            recommend=reviews.recommend(params.get("recommend"), opts),
        )

    # ----------------------------------------------------------- a person's moves
    def person(self, room: Room, name: str, person_id: int | None, params: dict[str, Any]) -> dict[str, Any]:
        """A person's move from the web UI (§37.5): answer a question, rule on a contested
        finding (concede it to an owner, or drop it), drop anything, or close the board.
        Each decision is posted as that person's own message in the room, so it reaches the
        agents the way anything a person says does; closing is a notice."""
        rv = self.store.current_review(room.id)
        if rv is None:
            raise ReviewError(f"{room.name} has no review board")
        action = params.get("action")
        if action == "close":
            self.store.set_review(rv["id"], closed_at=self.state.clock.now())
            self.store.add_event("review", room_id=room.id, data={"close": rv["url"], "by": name})
            self._notice(room, f"{name} closed the review board for {rv['url']}")
            self.state.hub.review(room.name, None)
            return {"ok": True, "board": None}
        if action == "post":
            return self._post(room, rv, name, person_id)
        if action not in ("answer", "concede", "confirm", "drop"):
            raise ReviewError("action must be answer, concede, confirm, drop, post or close")
        kind, n = reviews.parse_label(params.get("item"))
        item = self.store.review_item(rv["id"], kind, n)
        if item is None:
            raise ReviewError(f"{room.name}'s board has no {('F' if kind == 'finding' else 'Q')}{n}")
        if action == "confirm":
            if item.kind != "finding" or item.state != "fixed" or item.confirmed_by:
                raise ReviewError(f"{item.label} has no fix awaiting confirmation")
            if not reviews.needs_person(item, set(self.store.active_names(room.id)), self.state.clock.now()):
                raise ReviewError(f"{item.raised_by}, who raised {item.label}, confirms while in the room")
            to = "fixed"
        else:
            to = reviews.person_move(item, action)
        fields: dict[str, Any] = {}
        if action == "answer":
            choice = params.get("option")
            if type(choice) is not int or not 0 <= choice < len(item.options):
                raise ReviewError(
                    f"option must be one of {item.label}'s choices, 0 to {len(item.options) - 1}"
                )
            fields = {"answer": item.options[choice], "answered_by": name}
            said = f"answered with option {choice}"
        elif action == "concede":
            owner = reviews.text(params.get("owner"), "owner", 100, required=True)
            if owner not in self.store.active_names(room.id):
                raise ReviewError(f"owner must be an agent in {room.name}: {owner!r} isn't")
            fields = {"owner": owner}
            said = f"conceded, owner {owner}"
        elif action == "confirm":
            fields = {"confirmed_by": name}
            said = "confirmed"
        else:
            fields = {
                "reason": reviews.text(params.get("reason"), "reason", reviews.MAX_REASON, required=True)
            }
            said = f"dropped: {fields['reason']}"
        self.store.move_review_item(item.id, to, **fields)
        self.store.add_event(
            "review",
            room_id=room.id,
            data={"item": item.label, "to": "confirmed" if action == "confirm" else to, "by": name},
        )
        # Only text switchboard or this person wrote: the label, the option's number, an owner
        # checked against the room, their own reason. Never the agents' title or options, which
        # would reach every agent as this person's words, @mentions and all (#177).
        self.state.service.human_say(
            room.name,
            f'Review board: {item.label} {said}. review(action="show") shows it.',
            via="web",
            person=(name, person_id),
        )
        b = self.board(room)
        self.state.hub.review(room.name, b)
        return {"ok": True, "board": b}

    def _post(self, room: Room, rv: dict[str, Any], name: str, person_id: int | None) -> dict[str, Any]:
        """A person's Post (§37.6): once, on a settled board, one message from that person that
        @mentions each owner and lists what it posts. switchboard posts nothing itself."""
        if rv["posted_at"] is not None:
            raise ReviewError(f"{rv['posted_by']} already posted this board")
        items = self.store.review_items(rv["id"])
        if not reviews.settled(items):
            raise ReviewError(
                "the board isn't settled: every finding confirmed or dropped, every question answered"
            )
        plan = reviews.post_plan(items)
        if not plan:
            raise ReviewError("nothing to post: every item was dropped")
        text = reviews.render_post(plan, self.state.cfg.delivery.max_msg_chars)
        self.store.set_review(rv["id"], posted_at=self.state.clock.now(), posted_by=name)
        self.store.add_event("review", room_id=room.id, data={"post": rv["url"], "by": name})
        self.state.service.human_say(room.name, text, via="web", person=(name, person_id))
        b = self.board(room)
        self.state.hub.review(room.name, b)
        return {"ok": True, "board": b}

    # ---------------------------------------------------------------- output
    def _shown(self, room: Room, touched: str | None = None) -> dict[str, Any]:
        b = self.board(room)
        assert b is not None
        items = self.store.review_items(b["id"])
        out: dict[str, Any] = {
            "ok": True,
            "settled": b["settled"],
            "board": reviews.render(b, items),
        }
        if touched:
            out["item"] = touched
        self.state.hub.review(room.name, b)
        return out

    def _notice(self, room: Room, text: str) -> None:
        self.state.service.post_notice(room, text[:300], level="info")


def action_past(action: str) -> str:
    return {
        "concede": "conceded",
        "contest": "contested",
        "fix": "fixed",
        "confirm": "confirmed",
        "drop": "dropped",
    }[action]


def item_dict(
    i: reviews.Item,
    f: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
    active: set[str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """An item as the page reads it, with its links on the forge and its history (#250)."""
    return {
        "label": i.label,
        "kind": i.kind,
        "state": i.state,
        "title": i.title,
        "detail": i.detail,
        "file": i.file,
        "lines": i.lines,
        "raised_by": i.raised_by,
        "owner": i.owner,
        "commit": i.commit,
        "proof": i.proof,
        "confirmed_by": i.confirmed_by,
        "reason": i.reason,
        "options": list(i.options),
        "recommend": i.recommend,
        "answer": i.answer,
        "answered_by": i.answered_by,
        "needs_person": reviews.needs_person(i, active, now),
        "lines_url": reviews.lines_url(f, i.file, i.lines),
        "commit_url": reviews.commit_url(f, i.commit),
        "history": reviews.history(i, events or []),
    }
