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

ACTIONS = ("open", "show", "raise", "ask", "concede", "contest", "fix", "confirm", "drop", "defer")


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
        agent_people = {
            member.name: self.state.service.host_person(member.host) for member in self.store.members(room.id)
        }
        agents = list(agent_people)
        active = set(agents)
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
            "plan": reviews.handoff_plan(items, events, self.store.room_agent_names_ever(room.id)),
            "agents": agents,
            "agent_people": agent_people,
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
        if rv["posted_at"] is not None:
            raise ReviewError("this board was already handed off: no more moves")
        if action in ("raise", "ask"):
            item = self._add(rv, actor, action, params)
            verb = "raised" if item.kind == "finding" else "asked"
            return self._told(room, item, actor, f"{actor} {verb} {item.label}: {item.title}")
        kind, n = reviews.parse_label(params.get("item"))
        item = self.store.review_item(rv["id"], kind, n)
        if item is None:
            raise ReviewError(f"{room.name}'s board has no {('F' if kind == 'finding' else 'Q')}{n}")
        here = self.store.active_names(room.id)
        to = reviews.agent_move(item, action, actor, owner_here=item.owner in here)
        fields: dict[str, Any] = {}
        reassigned = action == "concede" and item.state == "conceded"
        if action == "concede":
            owner = reviews.text(params.get("owner"), "owner", 100) or actor
            if owner not in here:
                raise ReviewError(f"owner must be an agent in {room.name}: {owner!r} isn't")
            if reassigned and owner == item.owner:
                raise ReviewError(f"{item.label} is {owner}'s already: name the agent who takes it over")
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
        elif action == "defer":
            # the follow-up ticket comes as the reason: the tool's list has no room for a field (#371)
            fields["deferred_to"] = reviews.text(
                params.get("reason"), "reason (the follow-up ticket)", reviews.MAX_TICKET, required=True
            )
            fields["reason"] = ""
        self.store.move_review_item(item.id, to, **fields)
        shown = (
            "confirmed"
            if action == "confirm"
            else "reassigned"
            if reassigned
            else "deferred"
            if action == "defer"
            else to
        )
        self.store.add_event("review", room_id=room.id, data={"item": item.label, "to": shown, "by": actor})
        moved = self.store.review_item(rv["id"], kind, n)
        assert moved is not None
        if reassigned:  # a handover, or a takeover from an owner who left (#371)
            gone = "" if item.owner in here else ", who left the room"
            return self._told(
                room, moved, actor, f"{actor} moved {item.label} from {item.owner}{gone} to {fields['owner']}"
            )
        tail = {
            "deferred": f" to {fields.get('deferred_to')}",
            "conceded": f", owner {fields.get('owner')}",
            "fixed": f" in {fields.get('commit_sha', '')[:12]}",
            "contested": ": a person decides",
            "confirmed": f" by {actor}",
        }.get(shown, "")
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
        if rv is not None and rv["posted_at"] is not None:
            raise ReviewError("this board was already handed off: close it before opening another")
        if rv is not None and rv["url"] != url:
            raise ReviewError(f"{room.name} already has a board for {rv['url']}: {then}")
        if rv is None:
            self.store.open_review(room.id, url, head, by)
            self._notice(room, f"{by} opened a review board for {url}" + (f" at {head[:12]}" if head else ""))
        elif head and head != rv["head"]:
            # a new head: the same board, its checks now about a newer commit (§37.4)
            items = self.store.review_items(rv["id"])
            self.store.move_review_head(rv["id"], head, reviews.unsettled_finding_ids(items))
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
        if action == "handoff":
            return self._handoff(room, rv, name, person_id, params)
        if rv["posted_at"] is not None:
            raise ReviewError("this board was already handed off: no more moves")
        if action not in ("answer", "concede", "confirm", "drop", "fix", "defer"):
            raise ReviewError("action must be answer, concede, confirm, fix, drop, defer, handoff or close")
        kind, n = reviews.parse_label(params.get("item"))
        item = self.store.review_item(rv["id"], kind, n)
        if item is None:
            raise ReviewError(f"{room.name}'s board has no {('F' if kind == 'finding' else 'Q')}{n}")
        reassigned = action == "concede" and item.state == "conceded"
        if action == "confirm":
            # a person may confirm any fix, the raiser's check or not: a person's ruling (#371)
            if item.kind != "finding" or item.state != "fixed" or item.confirmed_by:
                raise ReviewError(f"{item.label} has no fix awaiting confirmation")
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
            if reassigned and owner == item.owner:
                raise ReviewError(f"{item.label} is {owner}'s already")
            fields = {"owner": owner}
            said = f"reassigned to {owner}" if reassigned else f"conceded, owner {owner}"
        elif action == "confirm":
            fields = {"confirmed_by": name}
            said = "confirmed"
        elif action == "defer":
            # the ticket comes as the reason, as from an agent: the route takes only the fields it lists
            ticket = reviews.text(
                params.get("reason"), "reason (the follow-up ticket)", reviews.MAX_TICKET, required=True
            )
            fields = {"deferred_to": ticket, "reason": ""}
            said = f"deferred to {ticket}"  # their own words, like a drop's reason
        elif action == "fix":
            # a person's ruling that it's fixed: settled at once, on their say (the page sends no
            # commit: the route takes only the fields it lists)
            fields = {"confirmed_by": name, "reason": ""}
            said = "marked fixed"
        else:
            fields = {
                "reason": reviews.text(params.get("reason"), "reason", reviews.MAX_REASON, required=True)
            }
            said = f"dropped: {fields['reason']}"
        self.store.move_review_item(item.id, to, **fields)
        self.store.add_event(
            "review",
            room_id=room.id,
            data={
                "item": item.label,
                "to": "confirmed"
                if action == "confirm"
                else "reassigned"
                if reassigned
                else "deferred"
                if action == "defer"
                else to,
                "by": name,
                "person": True,
            },
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

    def _handoff(
        self, room: Room, rv: dict[str, Any], name: str, person_id: int | None, params: dict[str, Any]
    ) -> dict[str, Any]:
        """One person's handoff to one agent, after every finding and question is settled."""
        if rv["posted_at"] is not None:
            raise ReviewError(f"this board was already handed off to {rv['posted_by']}")
        items = self.store.review_items(rv["id"])
        if not reviews.settled(items):
            raise ReviewError(
                "the board isn't settled: every finding confirmed or dropped, every question answered"
            )
        if not items:
            raise ReviewError("nothing to hand off: the board has no items")
        person_key = self.state.service.person_key(person_id)
        agents = [
            member.name
            for member in self.store.members(room.id)
            if self.state.service.host_person(member.host) == person_key
        ]
        chosen = reviews.text(params.get("agent"), "agent", 100) or (
            rv["opened_by"] if rv["opened_by"] in agents else (agents[0] if agents else "")
        )
        if chosen not in agents:
            raise ReviewError(f"posted by must be your agent in {room.name}: {chosen!r} isn't")
        events = [{"data": e.data} for e in self.store.review_events(room.id, rv["opened_at"])]
        plan = reviews.handoff_plan(items, events, self.store.room_agent_names_ever(room.id))
        text = reviews.render_handoff(plan, chosen, self.state.cfg.delivery.max_msg_chars)
        self.store.set_review(rv["id"], posted_at=self.state.clock.now(), posted_by=chosen)
        self.store.add_event(
            "review", room_id=room.id, data={"handoff": rv["url"], "by": name, "agent": chosen}
        )
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
        "defer": "deferred",
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
        "outdated_head": i.outdated_head,
        "deferred_to": i.deferred_to,
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
