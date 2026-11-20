"""The broker side of the review boards (DESIGN.md §37): applies agents' moves through the
``review`` tool, keeps the rows, and tells the room in one short line per move.

A room holds several boards, one per pull request URL, open or closed (§37.14); a closed board is
kept with every item and move it had, and ``open`` brings it back. The rules are in
``switchboard.reviews`` (pure); the SQL in ``store.py``. A move that hands the next step to another
agent (``reviews.told``: a raise, a concession to another owner, a fix) is posted here as the mover's
own message at once, delivered only to whoever acts next (a raise: the author's side, #884); any
other move is a notice, which wakes no one. A line stops counting as unanswered for an agent once the
board has moved on without it (§37.17). A board is read on demand (``review(action="show")``) or, for
a person, in the web UI.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from switchboard import db, review_stats, reviews
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
    "edit",
    "concur",
    "abstain",
    "concede",
    "contest",
    "insist",
    "fix",
    "confirm",
    "take",
    "reopen",
    "drop",
    "defer",
    "report",
    "close",
    "reset",
)


MAX_UNREVIEWED = 500  # links noted as "nothing asked", across all rooms, until a restart (§37.16)

# who posts a board's line: the mover's own message fields, as ``AgentService.review`` has them
Poster = dict[str, Any]


def vendor_of(member: Any) -> str:
    """Whose model an agent is, as far as the broker knows: its harness (Claude Code, Codex, ...). A review
    asked for by a link goes to agents of another vendor than the author's (§37.16)."""
    return str(member.harness)


class Boards:
    def __init__(self, state: Any):
        self.state = state
        # The board each agent last acted on, or was told about, in each room (§37.14): what a call
        # without a ``url`` means when the room has several open boards. It is kept in memory, and a
        # restart forgets it; a room with several open boards then asks for the url, never guesses.
        self._last: dict[tuple[int, str], str] = {}
        # An agent that asked for a board it doesn't hold to be closed has said so in the room once
        self._asked: set[tuple[int, str]] = set()
        # A link with no agent of another vendor in the room to review it is said so once (§37.16)
        self._unreviewed: set[tuple[int, str]] = set()
        self._unreviewed_full = False

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

    def past_boards(self, room: Room) -> list[dict[str, Any]]:
        """List closed boards without building their cards and handoff plans (§37.14)."""
        rows = (rv for rv in self.store.room_reviews(room.id) if rv["closed_at"] is not None)
        return [
            {
                "id": rv["id"],
                "url": rv["url"],
                "forge": reviews.forge(rv["url"]),
                "opened_at": rv["opened_at"],
                "settled_at": self._settled_at(room, rv),
                "closed_at": rv["closed_at"],
                "outcome": rv["pr_state"] if rv["pr_state"] in ("merged", "closed") else "unknown",
            }
            for rv in sorted(rows, key=lambda rv: (rv["closed_at"], rv["id"]), reverse=True)
        ]

    def past_board(self, room: Room, url: str) -> dict[str, Any] | None:
        rv = self.store.review_for_url(room.id, reviews.url(url))
        if rv is None or rv["closed_at"] is None:
            return None
        return self.board(room, rv)

    def _settled_at(self, room: Room, rv: dict[str, Any]) -> float | None:
        events = [(e.ts, e.data) for e in self.store.review_events(room.id, rv["id"], rv["opened_at"])]
        return review_stats.settled_at(self.store.review_items(rv["id"]), events)

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
            "items": [item_dict(i, f, events, active, now, agents, offline, rv["head"]) for i in items],
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
    def agent(self, room: Room, actor: str, params: dict[str, Any], *, poster: Poster) -> dict[str, Any]:
        """One ``review`` tool call from the member ``actor`` of ``room``. Returns the board as
        text (what the agent reads); or raises ReviewError with what to fix. A move that is someone
        else's to answer is posted as ``actor``'s message (``poster``: its fields), to them alone. A
        call is about one board (§37.14): its ``url``, else the one the agent last used."""
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
            return self._shown(room, rv, selected=params.get("item"))
        if action == "close":  # settled boards can close even after handoff
            return self._close(room, rv, actor, params)
        if action == "report":  # a merge may be reported after handoff or close
            self._remember(room, actor, rv)
            return self._report(room, rv, actor, params)
        if rv["closed_at"] is not None:
            raise ReviewError(f'the board for {rv["url"]} is closed: open it again with action="open" first')
        self._remember(room, actor, rv)
        if rv["posted_at"] is not None:
            raise ReviewError("this board was already handed off: no more moves")
        if action in ("raise", "ask"):
            self._guard_slot(room, rv)
            item = self._add(rv, actor, action, params)
            verb = "raised" if item.kind == "finding" else "asked"
            return self._told(
                room, rv, item, actor, f"{actor} {verb} {item.label}: {item.title}", poster=poster
            )
        kind, n = reviews.parse_label(params.get("item"))
        item = self.store.review_item(rv["id"], kind, n)
        if item is None:
            raise ReviewError(f"{room.name}'s board has no {('F' if kind == 'finding' else 'Q')}{n}")
        here = self.store.active_names(room.id)
        if action == "concur":
            return self._concur(room, rv, item, actor, here, params)
        if action == "edit":
            return self._edit(room, rv, item, actor, params, by_person=False)
        if action == "abstain":
            return self._abstain(room, rv, item, actor, here)
        held = reviews.holder(rv)
        # The board row names the holder; only an agent in the room that the board's own open or
        # reopen records as an agent can be that holder. A name alone is not enough: a person who
        # opened it may since be renamed, and an agent may join under their old name (#904).
        holder_here = (
            action == "fix"
            and item.state == "raised"
            and reviews.holder_in_room(held, here)
            and reviews.held_by_agent(
                held, [e.data for e in self.store.review_events(room.id, rv["id"], rv["opened_at"])]
            )
        )
        to = reviews.agent_move(
            item,
            action,
            actor,
            owner_here=item.owner in here,
            raiser_here=item.raised_by in here,
            holder=held,
            holder_here=holder_here,
        )
        if action == "reopen":
            self._guard_slot(room, rv)
        if action == "take":
            self.store.take_review_item(item.id, actor)
            self._event(room, rv, item=item.label, to="taken", by=actor, previous_raiser=item.raised_by)
            moved = self.store.review_item(rv["id"], kind, n)
            assert moved is not None
            return self._told(
                room,
                rv,
                moved,
                actor,
                f"{actor} took {item.label} from {item.raised_by}, who left the room",
                wake=False,
            )
        fields: dict[str, Any] = {}
        reassigned = action == "concede" and item.state == "conceded"
        if action == "concede":
            owner = reviews.text(params.get("owner"), "owner", 100) or actor
            if owner not in here:
                raise ReviewError(f"owner must be an agent in {room.name}: {owner!r} isn't")
            if reassigned and owner == item.owner:
                if owner == actor:  # a reopened card comes back to its owner (#905): not a handoff
                    raise ReviewError(f"{item.label} is already yours to fix: the next move is fix")
                raise ReviewError(f"{item.label} is {owner}'s already: name the agent who takes it over")
            fields["owner"] = owner
        elif action == "fix":
            commit = reviews.head(params.get("commit"))
            if not commit:
                raise ReviewError("fix needs the commit that fixes it")
            fields["commit_sha"] = commit
            fields["proof"] = reviews.proof(params.get("proof"))
            fields["reason"] = ""  # a conceded contest's reason no longer describes the fix
            if item.state == "raised":
                # the holder conceded and fixed in one move: the owner is the holder (#904)
                fields["owner"] = actor
        elif action == "reopen":
            fields["reason"] = reviews.text(params.get("reason"), "reason", reviews.MAX_REASON, required=True)
            fields["confirmed_by"] = ""
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
        shown = (
            "reopened"
            if action == "reopen"
            else "confirmed"
            if action == "confirm"
            else "reassigned"
            if reassigned
            else "deferred"
            if action == "defer"
            else "insisted"
            if action == "insist"
            else to
        )
        evidence: dict[str, Any] = {}
        if action == "fix":
            evidence = {"commit": fields["commit_sha"], "proof": fields["proof"]}
            if item.state == "fixed":
                # Older fix events did not carry evidence: retain that attempt before replacing it.
                evidence.update(previous_commit=item.commit, previous_proof=item.proof)
        elif action == "reopen":
            evidence = {"reason": fields["reason"], "commit": item.commit, "proof": item.proof}
        # One transaction: a crash between the item and its events must not leave a fixed card
        # with half a history. A retried fix would then revise, and the concession would be gone
        # (#904). db.tx joins the store's own transactions. The room hears only after the commit.
        with db.tx(self.store.con):
            self.store.move_review_item(item.id, to, **fields)
            if action == "fix" and item.state == "raised":
                # the concession this fix stood in for, so the card's history does not skip it
                self._event(room, rv, item=item.label, to="conceded", by=actor)
            self._event(room, rv, item=item.label, to=shown, by=actor, **evidence)
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
                poster=poster,
            )
        tail = {
            "reopened": f", owner {item.owner}: {fields.get('reason')}",
            "deferred": f" to {fields.get('deferred_to')}",
            "conceded": f", owner {fields.get('owner')}",
            "fixed": f" in {fields.get('commit_sha', '')[:12]}",
            "contested": f": {item.raised_by} drops it or insists"
            if reviews.raiser_answers(moved, set(here))
            else f": {item.raised_by} isn't here, a person decides",
            "insisted": ": a person decides",
            "confirmed": f" by {actor}",
        }.get(shown, "")
        return self._told(
            room,
            rv,
            moved,
            actor,
            f"{actor} {action_past(action)} {item.label}{tail}",
            action=action,
            poster=poster,
        )

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

    def _edit(
        self,
        room: Room,
        rv: dict[str, Any],
        item: reviews.Item,
        actor: str,
        params: dict[str, Any],
        *,
        by_person: bool,
    ) -> dict[str, Any]:
        """Correct a card's title, detail, file or lines (#786). The state stays. A notice, like a
        pick: the new words are on the card, and the history names which fields changed. The notice
        names the fields, not the new text, so a card's title is not posted into the room as the
        editor's words."""
        changes = reviews.edit_changes(item, actor, params, by_person=by_person)
        self.store.edit_review_item(item.id, **changes)
        fields = [name for name in reviews.EDIT_FIELDS if name in changes]
        self._event(room, rv, item=item.label, to="edited", by=actor, fields=fields, person=by_person)
        moved = self.store.review_item(rv["id"], item.kind, item.n)
        assert moved is not None
        said = f"{actor} edited {item.label}: {', '.join(fields)}"
        if by_person:
            self._notice(room, said)
            return {"ok": True, "boards": self._publish(room)}
        return self._told(room, rv, moved, actor, said, wake=False)

    def _abstain(
        self,
        room: Room,
        rv: dict[str, Any],
        item: reviews.Item,
        actor: str,
        here: list[str],
    ) -> dict[str, Any]:
        """``actor`` records no view on ``item``'s options (#785). The pick row, if any, goes, and
        a ``review`` event (``to: "abstained"``) is what the board reads back: the later of that
        and a ``picked`` event wins, so a later ``concur`` replaces it. A notice, like a pick:
        the agents who haven't spoken were asked when the item was raised or asked."""
        reviews.abstain(item, actor, here)
        self.store.clear_review_pick(item.id, actor)
        self._event(room, rv, item=item.label, to="abstained", by=actor)
        moved = self.store.review_item(rv["id"], item.kind, item.n)
        assert moved is not None
        c = reviews.consensus(moved, here)
        if c.state == "agreed":
            said = f"{actor} abstained on {item.label}: recommended by {reviews.names(c.by)}"
        elif c.state == "split":
            said = f"{actor} abstained on {item.label}: the agents are split"
        elif c.waiting:
            said = f"{actor} abstained on {item.label}; waiting on {reviews.names(c.waiting)}"
        else:
            said = f"{actor} abstained on {item.label}"
        return self._told(room, rv, moved, actor, said, wake=False)

    def _report(self, room: Room, rv: dict[str, Any], actor: str, params: dict[str, Any]) -> dict[str, Any]:
        """What ``actor`` read off the pull request with its own gh or glab (#291, §37.11): stored
        as its claim, with its name and the time. A merge or a close ends the board, and a new CI
        result is said in the room, each in one line naming who reported it, so agents cite the
        board instead of passing CI on from memory. switchboard fetches nothing itself."""
        fields = reviews.report(params)
        url = rv["url"]
        was_closed = rv["closed_at"] is not None
        state = fields.get("pr_state")
        if was_closed and state not in ("merged", "closed"):
            raise ReviewError('a closed board only accepts a report with state="merged" or state="closed"')
        if state == "closed" and rv["pr_state"] == "merged":  # open (reopened) or closed alike
            raise ReviewError(f"{url} is recorded as merged already: a closed report would overwrite it")
        ended = state in ("merged", "closed")
        result = {"ok": True, "closed": True, "board": f"{url} is {state}: the board is closed."}
        if was_closed and all(rv[key] == value for key, value in fields.items()):
            return result
        now = self.state.clock.now()
        closure = {"closed_at": now, "closed_by": actor} if ended and not was_closed else {}
        self.store.set_review(rv["id"], **fields, **closure, reported_by=actor, reported_at=now)
        self._event(room, rv, report=fields, by=actor)
        if fields.get("pr_ci") and fields["pr_ci"] != rv["pr_ci"]:
            self._notice(room, f"{actor} reports CI {fields['pr_ci']} on {url}")
        if ended:
            status = "remains closed" if was_closed else "is closed"
            self._notice(room, f"{actor} reports {url} {state}: the review board {status}")
            self._settle(room, rv, closed=True)  # a closed board takes no moves: nobody owes its lines
            self._publish(room)
            return result
        return self._shown(room, rv)

    def _close(self, room: Room, rv: dict[str, Any], actor: str, params: dict[str, Any]) -> dict[str, Any]:
        """Close a settled board at any time; open work needs the mover's reason in the room. The
        board is kept, with every item and move, and ``open`` brings it back (§37.14). Only the
        board's holder closes it: anyone else's close asks a person, in the room, and changes nothing."""
        if rv["closed_at"] is not None:
            raise ReviewError(f"the board for {rv['url']} is closed already")
        outcome, merge_commit, given = reviews.close_outcome(params)
        if outcome == "closed" and rv["pr_state"] == "merged":
            raise ReviewError(
                f"the pull request for {rv['url']} is recorded as merged: a close with state=closed would"
                ' overwrite it. Close with state="merged", or with no state.'
            )
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
        if outcome != "unknown":  # the pull request's outcome, a claim in the closer's name (#886)
            line += f", the pull request {outcome}" + (f" as {merge_commit}" if merge_commit else "")
        line += f": {reason}" if reason else "."
        if labels:
            line += f". Open items: {', '.join(labels)}."
        now = self.state.clock.now()
        if outcome == "unknown":
            self.store.set_review(rv["id"], closed_at=now, closed_by=actor)
        else:  # recorded as report records it, so Past boards, the Usage page and the stats read it
            fields = {"pr_state": outcome}
            self.store.set_review(
                rv["id"], **fields, closed_at=now, closed_by=actor, reported_by=actor, reported_at=now
            )
            self._event(
                room, rv, report={**fields, **({"commit": merge_commit} if merge_commit else {})}, by=actor
            )
        self._event(room, rv, close=rv["url"], by=actor, reason=reason, open=labels, outcome=outcome)
        self.state.service.post_notice(room, line, level="info")
        self._settle(room, rv, closed=True)
        self._publish(room)
        note = KEPT if given else f"{KEPT} Next time, {reviews.CLOSE_OUTCOME_HELP}."
        return {"ok": True, "closed": True, "board": None, "note": note}

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
        freeing = rv["closed_at"] is None and reviews.fills_open_slot(self.store.review_items(rv["id"]))
        self._check_room(room, freeing=freeing, grows=rv["closed_at"] is not None)
        if rv["closed_at"] is None:
            self.store.set_review(rv["id"], closed_at=self.state.clock.now(), closed_by=actor)
        self._settle(room, rv, closed=True)
        self._event(room, rv, reset=u, by=actor, reason=reason)
        fresh = self.store.open_review(room.id, u, head, actor)
        self._event(room, fresh, open=u, head=head, by=actor, opener="agent")
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
        action: str = "",
        poster: Poster | None = None,
    ) -> dict[str, Any]:
        """The board after ``actor``'s move on ``item``, and how the room hears of it: the line
        for whoever acts next, posted as the mover's message (``poster``) to them alone, so it wakes
        them, or the notice when that's nobody (§37.4). A raised finding's next move is the author's
        side's (``reviews.author_side``, #884). A move that asks nothing of anyone, such as a pick
        among options (``wake`` False), is a notice whatever state the item is in. With several boards
        open, the line and the notice say which one, and whoever is told of a move on a board has it as
        the board their next call is about (§37.14). Every move then settles the board's earlier lines:
        one an agent no longer owes is answered (``_settle``)."""
        several = self._several(room)
        line = None
        if wake and poster is not None:
            agents, offline = self._here(room)
            side = self._side(room, rv, self.store.review_items(rv["id"]), agents, offline)
            url = rv["url"] if several else ""
            line = reviews.told(item, actor, agents, url, action=action, side=side)
        if line is None:
            ref = self._ref(rv)
            self._notice(room, f"{ref}: {notice}" if several else notice)
        out = self._shown(room, rv, touched=item.label)
        if line is not None and poster is not None:
            self._say(room, rv, poster, line)
        self._settle(room, rv)
        return out

    # ---------------------------------------------------- lines for whoever acts next (#884)
    def _here(self, room: Room) -> tuple[list[str], list[str]]:
        """The room's agents, in the order they joined, and the ones of them that are offline."""
        members = self.store.members(room.id)
        names = self.store.active_names(room.id)
        return names, [m.name for m in members if m.status == "offline"]

    def _side(
        self,
        room: Room,
        rv: dict[str, Any],
        items: Sequence[reviews.Item],
        agents: Sequence[str],
        offline: Sequence[str],
    ) -> tuple[str, ...]:
        """The board's author side as it records it (``reviews.author_side``)."""
        events = [e.data for e in self.store.review_events(room.id, rv["id"], rv["opened_at"])]
        return reviews.author_side(reviews.holder(rv), items, events, agents, offline)

    def _say(self, room: Room, rv: dict[str, Any], poster: Poster, line: reviews.Told) -> None:
        """Post ``line`` at once as its mover's message, delivered only to the agents it names (§37.4),
        then record it: which card it named, in which state, and to whom, so a later move can settle it
        (``_settle``). The engine already gives several lines that arrive together to an agent as one
        wake (§37.17). Whoever is told has this board as the one their next call is about (§37.14)."""
        who = list(line.who)
        msg = self.state.service._post(
            room,
            **poster,
            # the one place stored card text is joined and cut into a message for an agent: scrubbed here as
            # defense in depth, though the upgrade cleaned the stored rows (db.clean_review_rows, #890)
            text=reviews.scrub(line.line)[: self.state.cfg.delivery.max_msg_chars],
            mentions=who,
            addressed_only=True,
        )
        self.store.add_event(
            "review_line",
            room_id=room.id,
            data={
                "board": rv["id"],
                "msg": msg.id,
                "items": [line.label],
                "states": [line.state],
                "to": who,
            },
        )
        for agent in who:
            self._last[(room.id, agent)] = rv["url"]

    def _settle(self, room: Room, rv: dict[str, Any], *, closed: bool = False) -> None:
        """Answer the board's earlier lines that the agents they reached no longer owe (#884): once a
        line's cards have moved on, or the agent has no move left on them (``reviews.answered``), its
        delivery of that line is handled, as a say or a pass would handle it, so the line neither comes
        back to it as ``again=yes`` nor sets off the watchdog. ``closed``: the board takes no more
        moves, so every line is answered. An agent that still has the move a line asked keeps it."""
        lines = self.store.review_lines_open(room.id, rv["id"], rv["opened_at"])
        if not lines:
            return
        items = self.store.review_items(rv["id"])
        by_label = {i.label: i for i in items}
        agents, offline = self._here(room)
        side = self._side(room, rv, items, agents, offline)
        done: list[tuple[int, int]] = []
        for msg, cards, reached, to in lines:
            # only the agents the line was addressed to: one that has it because the line names its
            # claim (§8.1) answers it as it answers any message that does, with a say or a pass
            names = [name for _, name in reached if to is None or name in to]
            free = set(names if closed else reviews.answered(cards, by_label, names, agents, side))
            done.extend((membership_id, msg) for membership_id, name in reached if name in free)
        if done:
            self.store.answer_deliveries(done)

    def _open(self, room: Room, actor: str, params: dict[str, Any]) -> dict[str, Any]:
        url = reviews.url(params.get("url"))
        head = reviews.head(params.get("head"))
        rv, how = self._open_board(room, actor, url, head, opener="agent")
        self._remember(room, actor, rv)
        out = self._shown(room, rv)
        if how == "reopened":
            out["note"] = "The board was closed: it is open again, with its items and history."
        return out

    def _filling(self, room: Room) -> list[dict[str, Any]]:
        """Open boards that take a slot: empty ones, and any that still have work (§37.14)."""
        return [
            rv
            for rv in self.store.open_reviews(room.id)
            if reviews.fills_open_slot(self.store.review_items(rv["id"]))
        ]

    def _check_room(
        self, room: Room, *, freeing: bool = False, adding: bool = True, grows: bool = True
    ) -> None:
        """A room holds at most ``MAX_OPEN_BOARDS`` boards that fill a slot (§37.14, #886). A settled
        board with cards does not. ``freeing``: this move closes one that fills a slot. ``adding``: the
        board being opened fills one (a settled reopen does not). ``grows``: the move adds an open
        board, which the ceiling ``MAX_OPEN_BOARDS_TOTAL`` on all open boards counts (#886). A refusal
        names the boards to close, so a full room can always make room."""
        filling = self._filling(room)
        open_ = self.store.open_reviews(room.id)
        if len(open_) + (1 if grows else 0) > reviews.MAX_OPEN_BOARDS_TOTAL:
            settled = [rv["url"] for rv in open_ if rv not in filling]
            raise ReviewError(
                f"{room.name} has {len(open_)} open boards, the most it keeps open at all"
                f" ({reviews.MAX_OPEN_BOARDS_TOTAL}), settled ones included: close a finished one with"
                f" state merged, closed or unknown ({', '.join(settled) or 'none are settled'})"
            )
        if len(filling) - (1 if freeing else 0) + (1 if adding else 0) > reviews.MAX_OPEN_BOARDS:
            urls = ", ".join(rv["url"] for rv in filling)
            raise ReviewError(
                f"{room.name} has {len(filling)} open boards, the most it holds: close one before opening"
                f" another ({urls}). A finished one is closed with state merged, closed or unknown"
            )

    def _guard_slot(self, room: Room, rv: dict[str, Any]) -> None:
        """A move that puts work back on a settled board takes a slot. At the cap it is refused,
        the same way opening another board is."""
        if not reviews.fills_open_slot(self.store.review_items(rv["id"])):
            self._check_room(room, grows=False)

    def _open_board(
        self, room: Room, by: str, url: str, head: str, *, opener: str, author: str = ""
    ) -> tuple[dict[str, Any], str]:
        """The room's board for ``url``, opened by ``by`` (an agent or a person) with ``head``, and
        what happened to it: ``opened`` (a new board), ``reopened`` (a closed one is back, with its
        items and history), ``moved`` (an open one now at a newer head) or ``kept`` (open already).
        Other boards of the room are left as they are (§37.14). ``author``: the agent that posted the
        link a board is opened for (§37.16), recorded with the move: the board's author side (#884).
        ``opener``: what ``by`` is, ``agent``, ``person`` or ``link``, recorded with the move: a
        board's holder is an agent only by this record, never by its name alone (#904)."""
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
                adding = reviews.fills_open_slot(self.store.review_items(rv["id"]))
                self._check_room(room, adding=adding)
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
        extra = {"author": author} if author else {}
        self._event(room, rv, open=url, head=head, by=by, how=how, opener=opener, **extra)
        return rv, how

    def on_agent_say(self, room: Room, author: str, text: str) -> None:
        """An agent has posted ``text`` in ``room`` (§37.16, #816). With the room's setting on, each pull or
        merge request link in it that has no board here, open or closed, opens one and posts one request, as
        switchboard, that @mentions the room's agents of another vendor than the author's. It is the words
        of ``/review`` and a checked link, never the agent's text. No agent of another vendor: a notice says
        so and nothing wakes. The board is the memory: the same link again, or with ``/files`` on the end, is
        a board already, so it asks once. Nothing is launched, and a room message never sets this."""
        if not room.review_on_link:
            return
        member = self.store.find_member(room.id, author)
        if member is None:
            return
        boards = [rv["url"] for rv in self.store.room_reviews(room.id)]
        have = {reviews.canonical_url(u) for u in boards}
        for url in reviews.pr_links(text):
            if url in have:
                continue
            if not reviews.trusted_forge(url, boards):
                self._say_once(
                    room,
                    url,
                    f"{author} posted {url}, a merge request on a host {room.name} has no review board on:"
                    " nothing was asked. If you trust it, /review it",
                )
                continue
            others = [m.name for m in self.store.members(room.id) if vendor_of(m) != vendor_of(member)]
            if not others:
                self._say_once(
                    room,
                    url,
                    f"{author} posted {url}, and no agent of another vendor is in {room.name} to"
                    " review it: ask one to join, then /review it",
                )
                continue
            try:
                self._open_board(room, "switchboard", url, "", opener="link", author=author)
            except ReviewError as e:
                self._notice(room, f"not asking for a review of {url}: {e}")
                continue
            have.add(url)
            self._publish(room)
            from switchboard.broker.commands import review_request

            self.state.service._post(
                room,
                sender_name="switchboard",
                sender_kind="system",
                via="system",
                text=review_request(
                    others, url, f"{author} posted this link. Asked for by this room's setting."
                ),
                mentions=others,
            )

    def _say_once(self, room: Room, url: str, text: str) -> None:
        """A notice about a link nothing was asked for, once for each link until the server restarts. The
        memory is kept from agent input, so it is capped at ``MAX_UNREVIEWED``: past it no more notes are
        posted (a person's ``/review`` is unaffected) and the cap is logged once, instead of growing."""
        key = (room.id, url)
        if key in self._unreviewed:
            return
        if len(self._unreviewed) >= MAX_UNREVIEWED:
            if not self._unreviewed_full:
                self._unreviewed_full = True
                log.warning(
                    "review-on-link: %d links already noted; no more notes until a restart", MAX_UNREVIEWED
                )
            return
        self._unreviewed.add(key)
        self._notice(room, text)

    def person_open(self, room: Room, name: str, url: str) -> str:
        """A person's ``/review`` (§37.8): the board opens, or comes back, or stays, for ``url``,
        and every page showing the room gets it at once. Returns what happened (``_open_board``)."""
        rv, how = self._open_board(room, name, reviews.url(url), "", opener="person")
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
            self._settle(room, rv, closed=True)
            return {"ok": True, "boards": self._publish(room)}
        if action == "handoff":
            return self._handoff(room, rv, name, person_id, params)
        if rv["posted_at"] is not None:
            raise ReviewError("this board was already handed off: no more moves")
        if action not in ("answer", "concede", "confirm", "drop", "fix", "defer", "edit"):
            raise ReviewError(
                "action must be answer, concede, confirm, fix, drop, defer, edit, handoff or close"
            )
        kind, n = reviews.parse_label(params.get("item"))
        item = self.store.review_item(rv["id"], kind, n)
        if item is None:
            raise ReviewError(f"{room.name}'s board has no {('F' if kind == 'finding' else 'Q')}{n}")
        if action == "edit":
            return self._edit(room, rv, item, name, params, by_person=True)
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
        self._settle(room, rv)  # a ruling or an answer is the next move on that card: its lines are answered
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
    def _shown(
        self, room: Room, rv: dict[str, Any], touched: str | None = None, selected: Any = None
    ) -> dict[str, Any]:
        """``rv`` as the agent reads it. An open board goes to the pages showing the room too; a
        closed one is only read."""
        if rv["closed_at"] is None:
            boards = self._publish(room)
            b = next(x for x in boards if x["id"] == rv["id"])
        else:
            b = self.board(room, rv)
        items = self.store.review_items(b["id"])
        now = self.state.clock.now()
        rendered = reviews.render(b, items, now, b["agents"], b["offline"])
        if selected not in (None, ""):
            kind, n = reviews.parse_label(selected)
            item = next((i for i in items if i.kind == kind and i.n == n), None)
            if item is None:
                raise ReviewError(f"{room.name}'s board has no {('F' if kind == 'finding' else 'Q')}{n}")
            events = [
                {"ts": e.ts, "data": e.data}
                for e in self.store.review_events(room.id, rv["id"], rv["opened_at"])
            ]
            rendered = reviews.render_item(b, item, events, now, b["agents"], b["offline"])
            touched = item.label
        out: dict[str, Any] = {"ok": True, "settled": b["settled"], "board": rendered}
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
        "reopen": "reopened",
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
    head: str = "",
) -> dict[str, Any]:
    """An item as the page reads it, with its links on the forge and its history (#250), what
    the room's agents (``present``) have picked among its options (#423), and whether a contested
    finding is still its raiser's to answer, which ``offline`` agents can't (#424). ``head`` is the
    board's, for a GitLab finding's link to its lines at that commit (#442)."""
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
            "abstained": list(c.abstained),
        },
        "answer": i.answer,
        "answered_by": i.answered_by,
        "needs_person": reviews.needs_person(i, active, now, offline),
        "lines_url": reviews.lines_url(f, i.file, i.lines, head),
        "diff_url": reviews.diff_url(f, i.file),
        "commit_url": reviews.commit_url(f, i.commit),
        "history": reviews.history(i, events or []),
    }
