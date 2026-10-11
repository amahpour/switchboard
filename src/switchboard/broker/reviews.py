"""The broker side of a review board (DESIGN.md §37): applies agents' moves through the
``review`` tool, keeps the rows, and tells the room in one short line per move.

The rules are in ``switchboard.reviews`` (pure); the SQL in ``store.py``. A move never posts a
chat message, so it never wakes anyone: the line is a notice, and the board is read on demand
(``review(action="show")``) or, for a person, in the web UI.
"""

from __future__ import annotations

import logging
from typing import Any

from switchboard import reviews
from switchboard.models import Room
from switchboard.reviews import ReviewError

log = logging.getLogger(__name__)

ACTIONS = ("open", "show", "raise", "ask", "concede", "contest", "fix", "drop")


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
        return {
            "id": rv["id"],
            "url": rv["url"],
            "forge": reviews.forge(rv["url"]),
            "head": rv["head"],
            "opened_by": rv["opened_by"],
            "opened_at": rv["opened_at"],
            "settled": reviews.settled(items),
            "counts": reviews.counts(items),
            "items": [item_dict(i) for i in items],
            "plan": {owner: [i.label for i in its] for owner, its in reviews.post_plan(items).items()},
            "posted_at": rv["posted_at"],
            "posted_by": rv["posted_by"],
        }

    # ------------------------------------------------------------ agent moves
    def agent(self, room: Room, actor: str, params: dict[str, Any]) -> dict[str, Any]:
        """One ``review`` tool call from the member ``actor`` of ``room``. Returns the board as
        text (what the agent reads), or raises ReviewError with what to fix."""
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
            self._notice(room, f"{actor} {verb} {item.label}: {item.title}")
            return self._shown(room, touched=item.label)
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
        elif action in ("contest", "drop"):
            fields["reason"] = reviews.text(params.get("reason"), "reason", reviews.MAX_REASON, required=True)
        self.store.move_review_item(item.id, to, **fields)
        self.store.add_event("review", room_id=room.id, data={"item": item.label, "to": to, "by": actor})
        tail = {
            "conceded": f", owner {fields.get('owner')}",
            "fixed": f" in {fields.get('commit_sha', '')[:12]}",
            "contested": ": a person decides",
        }.get(to, "")
        self._notice(room, f"{actor} {action_past(action)} {item.label}{tail}")
        return self._shown(room, touched=item.label)

    def _open(self, room: Room, actor: str, params: dict[str, Any]) -> dict[str, Any]:
        url = reviews.url(params.get("url"))
        head = reviews.head(params.get("head"))
        rv = self.store.current_review(room.id)
        if rv is not None and rv["url"] != url:
            raise ReviewError(
                f"{room.name} already has a board for {rv['url']}: a person closes it before another opens"
            )
        if rv is None:
            self.store.open_review(room.id, url, head, actor)
            self._notice(
                room, f"{actor} opened a review board for {url}" + (f" at {head[:12]}" if head else "")
            )
        elif head and head != rv["head"]:
            # a new head: the same board, its checks now about a newer commit (§37.4)
            self.store.set_review(rv["id"], head=head)
            self._notice(room, f"{actor} moved the review board to {head[:12]}")
        self.store.add_event("review", room_id=room.id, data={"open": url, "head": head, "by": actor})
        return self._shown(room)

    def _add(self, rv: dict[str, Any], actor: str, action: str, params: dict[str, Any]) -> reviews.Item:
        title = reviews.text(params.get("title"), "title", reviews.MAX_TITLE, required=True)
        detail = reviews.text(params.get("detail"), "detail", reviews.MAX_DETAIL)
        if action == "raise":
            return self.store.add_review_item(
                rv["id"],
                "finding",
                "raised",
                title=title,
                detail=detail,
                file=reviews.text(params.get("file"), "file", reviews.MAX_FILE),
                lines=reviews.lines(params.get("lines")),
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
        if action not in ("answer", "concede", "drop"):
            raise ReviewError("action must be answer, concede, drop, post or close")
        kind, n = reviews.parse_label(params.get("item"))
        item = self.store.review_item(rv["id"], kind, n)
        if item is None:
            raise ReviewError(f"{room.name}'s board has no {('F' if kind == 'finding' else 'Q')}{n}")
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
        else:
            fields = {
                "reason": reviews.text(params.get("reason"), "reason", reviews.MAX_REASON, required=True)
            }
            said = f"dropped: {fields['reason']}"
        self.store.move_review_item(item.id, to, **fields)
        self.store.add_event("review", room_id=room.id, data={"item": item.label, "to": to, "by": name})
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
                "the board isn't settled: every finding fixed or dropped, every question answered"
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
    return {"concede": "conceded", "contest": "contested", "fix": "fixed", "drop": "dropped"}[action]


def item_dict(i: reviews.Item) -> dict[str, Any]:
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
        "reason": i.reason,
        "options": list(i.options),
        "recommend": i.recommend,
        "answer": i.answer,
        "answered_by": i.answered_by,
        "needs_person": i.needs_person,
    }
