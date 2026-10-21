"""The broker side of the review boards (DESIGN.md §37): applies agents' moves through the
``review`` tool, keeps the rows, and tells the room in one short line per move.

A room holds several boards, one per pull request URL, open or closed (§37.14); a closed board is
kept with every item and move it had, and ``open`` brings it back. The rules are in
``switchboard.reviews`` (pure); the SQL in ``store.py``. A move that hands the next step to another
agent (``reviews.handoff``: a raise, a concession to another owner, a fix) comes back with the line
that wakes them, which ``AgentService.review`` posts as the mover's own message; any other move is a
notice, which wakes no one. A board is read on demand (``review(action="show")``) or, for a person,
in the web UI.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from switchboard import reviews
from switchboard.models import Room
from switchboard.reviews import ReviewError

log = logging.getLogger(__name__)

KEPT = 'The board is kept: review(action="show", url=...) reads it, open(url) brings it back.'

ACTIONS = (
    "open",
    "list",
    "show",
    "raise",
    "ask",
    "concur",
    "concede",
    "contest",
    "insist",
    "fix",
    "confirm",
    "drop",
    "defer",
    "report",
    "close",
    "reset",
)


class Boards:
    def __init__(self, state: Any):
        self.state = state
        # The board each agent last acted on, or was told about, in each room (§37.14): what a call
        # without a ``url`` means when the room has several open boards. It is kept in memory, and a
        # restart forgets it; a room with several open boards then asks for the url, never guesses.
        self._last: dict[tuple[int, str], str] = {}
        # An agent that asked for a board it doesn't hold to be closed has said so in the room once
        self._asked: set[tuple[int, str]] = set()

    @property
    def store(self) -> Any:
        return self.state.store

    # ----------------------------------------------------------------- reads
    def boards_named(self, room_name: str) -> list[dict[str, Any]] | None:
        """The open boards of the room called ``room_name``, or None: for the hub, which republishes
        them when who is in the room changes (§37.12)."""
        room = self.store.get_room(room_name)
        return self.boards(room) if room is not None else None

    def boards(self, room: Room) -> list[dict[str, Any]]:
        """The room's open boards as data, the one opened first first: what the page draws."""
        rows = self.store.open_reviews(room.id)
        if not rows:
            return []
        ctx = self._context(room)
        return [self._board(room, rv, ctx) for rv in rows]

    def board(self, room: Room, rv: dict[str, Any]) -> dict[str, Any]:
        """One board of the room as data, open or closed: the review, its items and whether it's
        settled."""
        return self._board(room, rv, self._context(room))

    def _context(self, room: Room) -> dict[str, Any]:
        """What every board of ``room`` is read against: who is in it, who is there, and the clock."""
        members = self.store.members(room.id)
        agent_people = {member.name: self.state.service.host_person(member.host) for member in members}
        return {
            "agent_people": agent_people,
            "agents": list(agent_people),
            # in the room but not there (#424): a contested finding doesn't wait on its raiser then
            "offline": sorted(m.name for m in members if m.status == "offline"),
            "now": self.state.clock.now(),
            "ever": self.store.room_agent_names_ever(room.id),
        }

    def _board(self, room: Room, rv: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        items = self.store.review_items(rv["id"])
        agents, offline, now = ctx["agents"], ctx["offline"], ctx["now"]
        active = set(agents)
        f = reviews.forge(rv["url"])
        events = [
            {"ts": e.ts, "data": e.data} for e in self.store.review_events(room.id, rv["id"], rv["opened_at"])
        ]
        return {
            "id": rv["id"],
            "url": rv["url"],
            "forge": f,
            "head": rv["head"],
            "opened_by": rv["opened_by"],
            "opened_at": rv["opened_at"],
            # kept when closed, and brought back by ``open`` (§37.14)
            "closed_at": rv["closed_at"],
            "closed_by": rv["closed_by"],
            "reopened_by": rv["reopened_by"],
            "reopened_at": rv["reopened_at"],
            "settled": reviews.settled(items),
            "counts": reviews.counts(items, active, now, offline),
            "items": [item_dict(i, f, events, active, now, agents, offline) for i in items],
            "plan": reviews.handoff_plan(items, events, ctx["ever"]),
            "agents": agents,
            "offline": offline,
            "agent_people": ctx["agent_people"],
            "posted_at": rv["posted_at"],
            "posted_by": rv["posted_by"],
            # what an agent last read off the pull request (#291): its claim, shown as its own
            "pr_state": rv["pr_state"],
            "pr_threads": rv["pr_threads"],
            "pr_ci": rv["pr_ci"],
            "reported_by": rv["reported_by"],
            "reported_at": rv["reported_at"],
            "report": reviews.report_line(rv, now),
        }

    def _publish(self, room: Room) -> list[dict[str, Any]]:
        """Every open board of the room to the pages showing it, and back to the caller."""
        boards = self.boards(room)
        self.state.hub.review(room.name, boards)
        return boards

    # --------------------------------------------------------- which board
    def _several(self, room: Room) -> bool:
        """Does the room have more than one open board? Then a line that names a board says which."""
        return len(self.store.open_reviews(room.id)) > 1

    def _ref(self, rv: dict[str, Any]) -> str:
        """A board as a notice names it: ``#7`` or ``!7`` for a pull or merge request, else its URL."""
        return reviews.ref(reviews.forge(rv["url"])) or rv["url"]

    def _board_for(self, room: Room, actor: str, params: dict[str, Any]) -> dict[str, Any]:
        """The board an agent's call is about (§37.14): the one its ``url`` names, open or closed;
        else the open board it last acted on or was told about; else the room's only open board.
        With several open boards and no way to tell, it asks for the url and lists them."""
        if params.get("url") not in (None, ""):
            u = reviews.url(params.get("url"))
            rv = self.store.review_for_url(room.id, u)
            if rv is None:
                raise ReviewError(f'{room.name} has no board for {u}: open one with action="open"')
            return rv
        open_ = self.store.open_reviews(room.id)
        last = self._last.get((room.id, actor))
        for rv in open_:
            if rv["url"] == last:
                return rv
        if len(open_) == 1:
            return open_[0]
        if not open_:
            raise ReviewError(f'{room.name} has no review board: open one with action="open" and the url')
        raise ReviewError(
            f"{room.name} has {len(open_)} open boards: say which with url=. "
            + " | ".join(rv["url"] for rv in open_)
        )

    def _remember(self, room: Room, agent: str, rv: dict[str, Any]) -> None:
        """``rv`` is the board ``agent``'s next call without a ``url`` is about (§37.14)."""
        if rv["closed_at"] is None:
            self._last[(room.id, agent)] = rv["url"]

    def _person_board(self, room: Room, params: dict[str, Any]) -> dict[str, Any]:
        """The open board a person's move is about: the one named by ``url``, else the room's only
        open board. The page always sends the url; without one and with several, it is refused."""
        if params.get("url") not in (None, ""):
            u = reviews.url(params.get("url"))
            rv = self.store.review_for_url(room.id, u)
            if rv is None or rv["closed_at"] is not None:
                raise ReviewError(f"{room.name} has no open board for {u}")
            return rv
        open_ = self.store.open_reviews(room.id)
        if not open_:
            raise ReviewError(f"{room.name} has no review board")
        if len(open_) > 1:
            raise ReviewError(f"{room.name} has {len(open_)} open boards: say which with url")
        return open_[0]

    def _event(self, room: Room, rv: dict[str, Any], **data: Any) -> None:
        """A ``review`` event of ``rv``: it names its board, so a card's history and a handoff's
        decisions are read from this board's events alone (§37.14)."""
        self.store.add_event("review", room_id=room.id, data={**data, "board": rv["id"]})

    # ------------------------------------------------------------ agent moves
    def agent(self, room: Room, actor: str, params: dict[str, Any]) -> dict[str, Any]:
        """One ``review`` tool call from the member ``actor`` of ``room``. Returns the board as
        text (what the agent reads), with ``handoff`` (who must act next, and the line that tells
        them) when the move is someone else's to answer; or raises ReviewError with what to fix.
        A call is about one board (§37.14): its ``url``, else the one the agent last used."""
        action = params.get("action")
        if action not in ACTIONS:
            raise ReviewError(f"action must be one of: {', '.join(ACTIONS)}")
        if action == "open":
            return self._open(room, actor, params)
        if action == "list":
            return self._list(room, actor)
        if action == "reset":
            return self._reset(room, actor, params)
        rv = self._board_for(room, actor, params)
        if action == "show":  # a closed board too: it is kept
            self._remember(room, actor, rv)
            return self._shown(room, rv)
        if action == "close":  # settled boards can close even after handoff
            return self._close(room, rv, actor, params)
        if rv["closed_at"] is not None:
            raise ReviewError(f'the board for {rv["url"]} is closed: open it again with action="open" first')
        self._remember(room, actor, rv)
        if action == "report":  # also after the handoff: the merge that closes the board comes then
            return self._report(room, rv, actor, params)
        if rv["posted_at"] is not None:
            raise ReviewError("this board was already handed off: no more moves")
        if action in ("raise", "ask"):
            item = self._add(rv, actor, action, params)
            verb = "raised" if item.kind == "finding" else "asked"
            return self._told(room, rv, item, actor, f"{actor} {verb} {item.label}: {item.title}")
        kind, n = reviews.parse_label(params.get("item"))
        item = self.store.review_item(rv["id"], kind, n)
        if item is None:
            raise ReviewError(f"{room.name}'s board has no {('F' if kind == 'finding' else 'Q')}{n}")
        here = self.store.active_names(room.id)
        if action == "concur":
            return self._concur(room, rv, item, actor, here, params)
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
        elif action == "insist":
            # the raiser's case, to set beside the contest's: only then does a person decide (#424)
            fields["insisted"] = reviews.text(
                params.get("reason"), "reason", reviews.MAX_REASON, required=True
            )
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
            else "insisted"
            if action == "insist"
            else to
        )
        self._event(room, rv, item=item.label, to=shown, by=actor)
        moved = self.store.review_item(rv["id"], kind, n)
        assert moved is not None
        if reassigned:  # a handover, or a takeover from an owner who left (#371)
            gone = "" if item.owner in here else ", who left the room"
            return self._told(
                room,
                rv,
                moved,
                actor,
                f"{actor} moved {item.label} from {item.owner}{gone} to {fields['owner']}",
            )
        tail = {
            "deferred": f" to {fields.get('deferred_to')}",
            "conceded": f", owner {fields.get('owner')}",
            "fixed": f" in {fields.get('commit_sha', '')[:12]}",
            "contested": f": {item.raised_by} drops it or insists"
            if reviews.raiser_answers(moved, set(here))
            else f": {item.raised_by} isn't here, a person decides",
            "insisted": ": a person decides",
            "confirmed": f" by {actor}",
        }.get(shown, "")
        return self._told(room, rv, moved, actor, f"{actor} {action_past(action)} {item.label}{tail}")

    def _concur(
        self,
        room: Room,
        rv: dict[str, Any],
        item: reviews.Item,
        actor: str,
        here: list[str],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``actor`` agrees with the option an agent recommended, or picks another (#423, §37.12).
        The pick is stored; whether an option is "recommended" is computed from every agent's
        pick (``reviews.consensus``). It says so in the room as a notice and wakes no one: the
        agents who haven't picked were asked when the item was raised or asked."""
        pick = reviews.concur(item, actor, params.get("recommend"), here)
        self.store.set_review_pick(item.id, actor, pick)
        self._event(room, rv, item=item.label, to="picked", by=actor, option=pick)
        moved = self.store.review_item(rv["id"], item.kind, item.n)
        assert moved is not None
        c = reviews.consensus(moved, here)
        if c.state == "agreed":
            said = f"{actor} concurred on {item.label}: recommended by {reviews.names(c.by)}"
        elif c.state == "split":
            said = f"{actor} picked another option for {item.label}: the agents are split"
        else:
            said = f"{actor} picked an option for {item.label}; waiting on {reviews.names(c.waiting)}"
        return self._told(room, rv, moved, actor, said, wake=False)

    def _report(self, room: Room, rv: dict[str, Any], actor: str, params: dict[str, Any]) -> dict[str, Any]:
        """What ``actor`` read off the pull request with its own gh or glab (#291, §37.11): stored
        as its claim, with its name and the time. A merge or a close ends the board, and a new CI
        result is said in the room, each in one line naming who reported it, so agents cite the
        board instead of passing CI on from memory. switchboard fetches nothing itself."""
        fields = reviews.report(params)
        now = self.state.clock.now()
        self.store.set_review(rv["id"], **fields, reported_by=actor, reported_at=now)
        self._event(room, rv, report=dict(fields), by=actor)
        url = rv["url"]
        if fields.get("pr_ci") and fields["pr_ci"] != rv["pr_ci"]:
            self._notice(room, f"{actor} reports CI {fields['pr_ci']} on {url}")
        if fields.get("pr_state") in ("merged", "closed"):
            self.store.set_review(rv["id"], closed_at=now, closed_by=actor)
            self._notice(room, f"{actor} reports {url} {fields['pr_state']}: the review board is closed")
            self._publish(room)
            return {
                "ok": True,
                "closed": True,
                "board": f"{url} is {fields['pr_state']}: the board is closed.",
            }
        return self._shown(room, rv)

    def _close(self, room: Room, rv: dict[str, Any], actor: str, params: dict[str, Any]) -> dict[str, Any]:
        """Close a settled board at any time; open work needs the mover's reason in the room. The
        board is kept, with every item and move, and ``open`` brings it back (§37.14). Only the
        board's holder closes it: anyone else's close asks a person, in the room, and changes nothing."""
        if rv["closed_at"] is not None:
            raise ReviewError(f"the board for {rv['url']} is closed already")
        items = self.store.review_items(rv["id"])
        open_items = reviews.open_items(items)
        holder = reviews.holder(rv)
        mine = actor == holder
        reason = reviews.text(
            params.get("reason"), "reason", reviews.MAX_REASON, required=bool(open_items) or not mine
        )
        ref = self._ref(rv)
        if not mine:
            if (rv["id"], actor) not in self._asked:
                self._asked.add((rv["id"], actor))
                self._notice(
                    room,
                    f"{actor} asks a person to close the review board for {ref}"
                    f" (opened by {holder}): {reason}",
                )
            raise ReviewError(
                f"{ref} was opened by {holder}: only {holder} or a person can close it."
                " A person has been asked in the room."
            )
        labels = [item.label for item in open_items]
        line = f"Review board for {ref} closed by {actor}"
        line += f": {reason}" if reason else "."
        if labels:
            line += f". Open items: {', '.join(labels)}."
        self.store.set_review(rv["id"], closed_at=self.state.clock.now(), closed_by=actor)
        self._event(room, rv, close=rv["url"], by=actor, reason=reason, open=labels)
        self.state.service.post_notice(room, line, level="info")
        self._publish(room)
        return {"ok": True, "closed": True, "board": None, "note": KEPT}

    def _reset(self, room: Room, actor: str, params: dict[str, Any]) -> dict[str, Any]:
        """A fresh board for a URL that has one, on purpose (§37.14): the earlier board stays in the
        database, unseen, so no card or move is deleted. Only the board's holder, and always with a
        reason and the url: nothing here is a default."""
        u = reviews.url(params.get("url"))
        reason = reviews.text(params.get("reason"), "reason", reviews.MAX_REASON, required=True)
        head = reviews.head(params.get("head"))
        rv = self.store.review_for_url(room.id, u)
        if rv is None:
            raise ReviewError(f'{room.name} has no board for {u}: open one with action="open"')
        holder = reviews.holder(rv)
        if actor != holder:
            raise ReviewError(f"{self._ref(rv)} was opened by {holder}: only {holder} can reset it")
        self._check_room(room, closing=rv["closed_at"] is None)
        if rv["closed_at"] is None:
            self.store.set_review(rv["id"], closed_at=self.state.clock.now(), closed_by=actor)
        self._event(room, rv, reset=u, by=actor, reason=reason)
        fresh = self.store.open_review(room.id, u, head, actor)
        self._event(room, fresh, open=u, head=head, by=actor)
        self._notice(
            room,
            f"{actor} reset the review board for {self._ref(rv)}: {reason}. A fresh board is open;"
            " the earlier one is no longer shown",
        )
        self._remember(room, actor, fresh)
        return self._shown(room, fresh)

    def _list(self, room: Room, actor: str) -> dict[str, Any]:
        """Every board of the room, open and closed, with its counts and who opened and closed it."""
        ctx = self._context(room)
        rows = self.store.room_reviews(room.id)
        boards = []
        for rv in rows:
            items = self.store.review_items(rv["id"])
            boards.append(
                {
                    **rv,
                    "counts": reviews.counts(items, set(ctx["agents"]), ctx["now"], ctx["offline"]),
                }
            )
        last = self._last.get((room.id, actor))
        open_urls = [b["url"] for b in boards if b["closed_at"] is None]
        default = last if last in open_urls else open_urls[0] if len(open_urls) == 1 else None
        return {"ok": True, "boards": reviews.list_text(boards, default)}

    def _told(
        self,
        room: Room,
        rv: dict[str, Any],
        item: reviews.Item,
        actor: str,
        notice: str,
        wake: bool = True,
    ) -> dict[str, Any]:
        """The board after ``actor``'s move on ``item``, and how the room hears of it: the line
        for whoever acts next (posted by the caller as the mover's message, so it wakes them),
        or the notice when that's nobody (§37.4). A move that asks nothing of anyone, such as a
        pick among options (``wake`` False), is a notice whatever state the item is in. With several
        boards open, the line and the notice say which one, and whoever is told of a move on a board
        has it as the board their next call is about (§37.14)."""
        several = self._several(room)
        handoff = (
            reviews.handoff(item, actor, self.store.active_names(room.id), rv["url"] if several else "")
            if wake
            else None
        )
        if handoff is None:
            ref = self._ref(rv)
            self._notice(room, f"{ref}: {notice}" if several else notice)
        out = self._shown(room, rv, touched=item.label)
        if handoff is not None:
            out["handoff"] = {"mentions": list(handoff[0]), "text": handoff[1]}
            for agent in handoff[0]:
                self._last[(room.id, agent)] = rv["url"]
        return out

    def _open(self, room: Room, actor: str, params: dict[str, Any]) -> dict[str, Any]:
        url = reviews.url(params.get("url"))
        head = reviews.head(params.get("head"))
        rv, how = self._open_board(room, actor, url, head)
        self._remember(room, actor, rv)
        out = self._shown(room, rv)
        if how == "reopened":
            out["note"] = "The board was closed: it is open again, with its items and history."
        return out

    def _check_room(self, room: Room, *, closing: bool = False) -> None:
        """A room holds at most ``MAX_OPEN_BOARDS`` open boards (§37.14). ``closing``: the move also
        closes one, so the count doesn't grow."""
        open_ = len(self.store.open_reviews(room.id))
        if open_ - (1 if closing else 0) >= reviews.MAX_OPEN_BOARDS:
            raise ReviewError(
                f"{room.name} has {open_} open boards, the most it holds: close one before opening another"
            )

    def _open_board(self, room: Room, by: str, url: str, head: str) -> tuple[dict[str, Any], str]:
        """The room's board for ``url``, opened by ``by`` (an agent or a person) with ``head``, and
        what happened to it: ``opened`` (a new board), ``reopened`` (a closed one is back, with its
        items and history), ``moved`` (an open one now at a newer head) or ``kept`` (open already).
        Other boards of the room are left as they are (§37.14)."""
        rv = self.store.review_for_url(room.id, url)
        new_head = bool(head) and (rv is None or head != rv["head"])
        if rv is not None and rv["posted_at"] is not None and new_head:
            raise ReviewError(
                "this board was already handed off: no more moves. A fresh board for this pull request"
                ' starts with action="reset"'
            )
        how = "kept"
        if rv is None:
            self._check_room(room)
            rv = self.store.open_review(room.id, url, head, by)
            self._notice(room, f"{by} opened a review board for {url}" + (f" at {head[:12]}" if head else ""))
            how = "opened"
        else:
            if rv["closed_at"] is not None:
                self._check_room(room)
                self.store.reopen_review(rv["id"], by)
                self._notice(
                    room, f"{by} opened the review board for {url} again: its items and history are back"
                )
                how = "reopened"
            if new_head:
                # a new head: the same board, its checks now about a newer commit (§37.4)
                items = self.store.review_items(rv["id"])
                self.store.move_review_head(rv["id"], head, reviews.unsettled_finding_ids(items))
                self._notice(room, f"{by} moved the review board to {head[:12]}")
                how = how if how == "reopened" else "moved"
            rv = self.store.review_for_url(room.id, url) or rv
        self._event(room, rv, open=url, head=head, by=by, how=how)
        return rv, how

    def person_open(self, room: Room, name: str, url: str) -> str:
        """A person's ``/review`` (§37.8): the board opens, or comes back, or stays, for ``url``,
        and every page showing the room gets it at once. Returns what happened (``_open_board``)."""
        rv, how = self._open_board(room, name, reviews.url(url), "")
        self._publish(room)
        return how

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
            # a finding may offer fix options, in a question's shape: 2 to 4, and the one its raiser
            # recommends (#423). Options without a recommendation, or one without options, are refused.
            fix: dict[str, Any] = {}
            if "options" in params:
                opts = reviews.options(params["options"])
                fix = {
                    "options": opts,
                    "recommend": reviews.recommend(params.get("recommend"), opts, required=True),
                }
            elif params.get("recommend") is not None:
                raise ReviewError("recommend picks one of options: give the options too")
            return self.store.add_review_item(
                rv["id"],
                "finding",
                "raised",
                title=title,
                detail=detail,
                file=file,
                lines=lines,
                raised_by=actor,
                **fix,
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
            recommend=reviews.recommend(params.get("recommend"), opts, required=True),
        )

    # ----------------------------------------------------------- a person's moves
    def person(self, room: Room, name: str, person_id: int | None, params: dict[str, Any]) -> dict[str, Any]:
        """A person's move from the web UI (§37.5): answer a question, rule on a contested
        finding (concede it to an owner, or drop it), drop anything, or close the board.
        Each decision is posted as that person's own message in the room, so it reaches the
        agents the way anything a person says does; closing is a notice. The move is about one
        board: the ``url`` the page sends, or the room's only open one (§37.14). A person may
        close any board, whoever opened it."""
        rv = self._person_board(room, params)
        action = params.get("action")
        if action == "close":
            reason = reviews.text(params.get("reason"), "reason", reviews.MAX_REASON)
            self.store.set_review(rv["id"], closed_at=self.state.clock.now(), closed_by=name)
            self._event(room, rv, close=rv["url"], by=name, reason=reason, person=True)
            self._notice(
                room, f"{name} closed the review board for {rv['url']}" + (f": {reason}" if reason else "")
            )
            return {"ok": True, "boards": self._publish(room)}
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
        self._event(
            room,
            rv,
            item=item.label,
            to="confirmed"
            if action == "confirm"
            else "reassigned"
            if reassigned
            else "deferred"
            if action == "defer"
            else to,
            by=name,
            person=True,
        )
        # Only text switchboard or this person wrote: the label, the option's number, an owner
        # checked against the room, their own reason. Never the agents' title or options, which
        # would reach every agent as this person's words, @mentions and all (#177). With several
        # boards open it also names which one by its number, built from digits and nothing an agent
        # wrote, and sends the agents to ``list`` rather than to a board that may not be theirs.
        several = self._several(room)
        ref = reviews.ref(reviews.forge(rv["url"])) if several else ""
        self.state.service.human_say(
            room.name,
            f"Review board{' ' + ref if ref else ''}: {item.label} {said}. "
            + ('review(action="list") shows the boards.' if several else 'review(action="show") shows it.'),
            via="web",
            person=(name, person_id),
        )
        return {"ok": True, "boards": self._publish(room)}

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
        events = [{"data": e.data} for e in self.store.review_events(room.id, rv["id"], rv["opened_at"])]
        plan = reviews.handoff_plan(items, events, self.store.room_agent_names_ever(room.id))
        ref = reviews.ref(reviews.forge(rv["url"])) if self._several(room) else ""
        text = reviews.render_handoff(plan, chosen, self.state.cfg.delivery.max_msg_chars, ref)
        self.store.set_review(rv["id"], posted_at=self.state.clock.now(), posted_by=chosen)
        self._event(room, rv, handoff=rv["url"], by=name, agent=chosen)
        # the agent chosen reads this board when it calls ``show``, not another that is open
        self._last[(room.id, chosen)] = rv["url"]
        self.state.service.human_say(room.name, text, via="web", person=(name, person_id))
        return {"ok": True, "boards": self._publish(room)}

    # ---------------------------------------------------------------- output
    def _shown(self, room: Room, rv: dict[str, Any], touched: str | None = None) -> dict[str, Any]:
        """``rv`` as the agent reads it. An open board goes to the pages showing the room too; a
        closed one is only read."""
        if rv["closed_at"] is None:
            boards = self._publish(room)
            b = next(x for x in boards if x["id"] == rv["id"])
        else:
            b = self.board(room, rv)
        items = self.store.review_items(b["id"])
        out: dict[str, Any] = {
            "ok": True,
            "settled": b["settled"],
            "board": reviews.render(b, items, self.state.clock.now(), b["agents"], b["offline"]),
        }
        if touched:
            out["item"] = touched
        return out

    def _notice(self, room: Room, text: str) -> None:
        self.state.service.post_notice(room, text[:300], level="info")


def action_past(action: str) -> str:
    return {
        "concede": "conceded",
        "contest": "contested",
        "insist": "insisted",
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
    present: Sequence[str] = (),
    offline: Sequence[str] = (),
) -> dict[str, Any]:
    """An item as the page reads it, with its links on the forge and its history (#250), what
    the room's agents (``present``) have picked among its options (#423), and whether a contested
    finding is still its raiser's to answer, which ``offline`` agents can't (#424)."""
    c = reviews.consensus(i, present)
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
        "insisted": i.insisted,
        "insist_line": reviews.insist_line(i),
        "options": list(i.options),
        "recommend": i.recommend,
        "picks": [{"agent": a, "option": o} for a, o in reviews.effective_picks(i).items()],
        "consensus": {
            "state": c.state,
            "option": c.option,
            "by": list(c.by),
            "waiting": list(c.waiting),
            "order": list(c.order),
            "notes": list(c.notes),
        },
        "answer": i.answer,
        "answered_by": i.answered_by,
        "needs_person": reviews.needs_person(i, active, now, offline),
        "lines_url": reviews.lines_url(f, i.file, i.lines),
        "commit_url": reviews.commit_url(f, i.commit),
        "history": reviews.history(i, events or []),
    }
