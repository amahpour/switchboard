"""Who a review board's move wakes, and when its line is answered (#884, DESIGN.md §37.17).

A raise is the author's side's to answer, not every agent's in the room; one agent's raises within a
few seconds go as one message; and a line stops counting as unanswered for an agent once the board has
moved on without it. The pure rules first, then the real ``Boards``, store and engine on a ``FakeClock``,
with no broker and no I/O: a bystander reviewer is never woken and never gets ``again=yes``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import FakeClock
from engine_agents import listener, result, session, stop, woke
from engine_world import World

from switchboard import reviews
from switchboard.broker.reviews import Boards
from switchboard.config import Config
from switchboard.delivery import rules
from switchboard.models import Membership, Participant
from switchboard.reviews import Item

PR = "https://github.com/example/shop/pull/866"
ROOM = ("dispatch", "codex-review", "grok-reviewer")


def finding(
    state: str = "raised", raised_by: str = "codex-review", owner: str = "", n: int = 1, **kw: Any
) -> Item:
    base: dict[str, Any] = {"options": (), "recommend": None, "commit": "", "reason": ""}
    return Item(
        id=n, n=n, kind="finding", state=state, title=f"finding {n}", detail="", file="a.py", lines="3",
        raised_by=raised_by, owner=owner, answer="", answered_by="", **{**base, **kw},
    )  # fmt: skip


def question(**kw: Any) -> Item:
    return replace(finding(), kind="question", state="open", options=("a", "b"), recommend=0, **kw)


# --------------------------------------------------------------- the author's side
def test_the_board_records_its_author_side_from_who_opened_it_answered_and_owns() -> None:
    """The holder that opened the board, an owner, a conceder and a contester are the author's side; a
    reviewer is not. Fails if a raise is everyone's to answer (the bug: the other reviewer was woken)."""
    room = ("dispatch", "codex-review", "grok-reviewer", "claude-author", "cursor-fixer")
    assert reviews.author_side("dispatch", [], [], room) == ("dispatch",)
    items = [finding(), finding("conceded", owner="cursor-fixer", n=2)]
    events = [
        {"data": {"item": "F3", "to": "contested", "by": "claude-author", "board": 1}},
        {"data": {"item": "F4", "to": "conceded", "by": "alice", "person": True, "board": 1}},  # a person
    ]
    assert reviews.author_side("dispatch", items, events, room) == (
        "dispatch",
        "claude-author",
        "cursor-fixer",
    )


def test_an_opener_that_only_raised_is_a_reviewer_and_a_person_or_switchboard_is_nobody() -> None:
    """A reviewer that opened the board and raised on it is not the side that answers; a board a person
    or switchboard opened knows no author until someone answers a finding, so it reads as empty."""
    room = ("dispatch", "codex-review", "grok-reviewer")
    assert reviews.author_side("codex-review", [finding()], [], room) == ()
    assert reviews.author_side("alice", [finding()], [], room) == ()
    assert reviews.author_side("switchboard", [], [{"open": PR, "by": "switchboard"}], room) == ()
    # the link's poster is recorded when switchboard opens the board for it (§37.16)
    opened = [{"data": {"open": PR, "by": "switchboard", "author": "dispatch", "board": 1}}]
    assert reviews.author_side("switchboard", [finding()], opened, room) == ("dispatch",)
    # an agent that moved it to the commit it pushed is its side too; one that found it open as it was is not
    assert reviews.author_side("alice", [], [{"open": PR, "by": "dispatch", "how": "moved"}], room) == (
        "dispatch",
    )
    assert reviews.author_side("alice", [], [{"open": PR, "by": "grok-reviewer", "how": "kept"}], room) == ()


def test_an_agent_that_raised_here_is_a_reviewer_unless_it_owns_a_finding() -> None:
    """In a review the agents do together, a reviewer may answer another's finding. It is still a
    reviewer once it has raised one, so the next raise does not go to it instead of the author, which
    the board then doesn't know: everyone hears it. A finding conceded to an agent makes it the side
    whatever else it did."""
    room = ("codex-review", "grok-reviewer", "claude-author")
    items = [finding(), finding(raised_by="grok-reviewer", n=2)]
    answered = [{"item": "F1", "to": "contested", "by": "grok-reviewer"}]
    assert reviews.author_side("alice", items, answered, room) == ()
    owned = [*items, finding("conceded", raised_by="codex-review", owner="grok-reviewer", n=3)]
    assert reviews.author_side("alice", owned, answered, room) == ("grok-reviewer",)


def test_an_offline_or_departed_author_side_is_not_counted() -> None:
    """A stale member (offline) or one that left would never answer: the raise falls back to every
    other agent rather than wait on it. Fails if the side is taken as recorded whatever its state."""
    room = ("codex-review", "grok-reviewer", "claude-author")
    assert reviews.author_side("dispatch", [], [], room) == ()  # it left the room
    assert reviews.author_side("claude-author", [], [], room, offline=("claude-author",)) == ()
    assert reviews.acts_next(finding(), room, ()) == ("grok-reviewer", "claude-author")


# ------------------------------------------------------------------ who acts next
def test_a_raise_is_the_author_sides_alone_and_everyones_when_the_board_does_not_know() -> None:
    """With a side, only it is named; with none, every other agent, as before #884."""
    who, line = reviews.handoff(finding(), "codex-review", ROOM, side=("dispatch",)) or ((), "")
    assert who == ("dispatch",)
    assert line == (
        "Review board: F1 raised: finding 1. @dispatch: concede or contest it."
        ' review(action="show") shows the board.'
    )
    assert reviews.handoff(finding(), "codex-review", ROOM)[0] == ("dispatch", "grok-reviewer")  # type: ignore[index]


def test_fix_options_still_ask_every_agent_to_pick() -> None:
    """A finding with fix options waits on every agent's pick (§37.12), so its raise still asks them all;
    a question too. Fails if narrowing the raise left an agent the consensus waits on unasked."""
    opts = finding(options=("x", "y"), recommend=0)
    assert reviews.handoff(opts, "codex-review", ROOM, side=("dispatch",))[0] == (  # type: ignore[index]
        "dispatch",
        "grok-reviewer",
    )
    assert reviews.handoff(question(), "codex-review", ROOM, side=("dispatch",))[0] == (  # type: ignore[index]
        "dispatch",
        "grok-reviewer",
    )
    # once grok-reviewer has picked, it has no move left on it; the side still concedes or contests
    picked = replace(opts, picks=(("grok-reviewer", 1),))
    assert reviews.acts_next(picked, ROOM, ("dispatch",)) == ("dispatch",)
    assert reviews.acts_next(replace(question(), abstentions=("dispatch",)), ROOM) == ("grok-reviewer",)


def test_each_state_has_one_next_move_and_a_settled_card_none() -> None:
    side = ("dispatch",)
    assert reviews.acts_next(finding("contested"), ROOM, side) == ("codex-review",)
    assert reviews.acts_next(finding("contested", insisted="it stands"), ROOM, side) == ()  # a person's
    assert reviews.acts_next(finding("conceded", owner="dispatch"), ROOM, side) == ("dispatch",)
    assert reviews.acts_next(finding("fixed", owner="dispatch"), ROOM, side) == ("codex-review",)
    assert (
        reviews.acts_next(finding("fixed", owner="dispatch", confirmed_by="codex-review"), ROOM, side) == ()
    )
    assert reviews.acts_next(finding("dropped"), ROOM, side) == ()
    assert reviews.acts_next(replace(question(), state="answered"), ROOM) == ()


def test_a_line_is_answered_once_its_cards_moved_on_or_the_agent_has_no_move_on_them() -> None:
    """Of the agents a line reached, only those still to make the move it asked owe it: a card that
    moved on since (conceded, even to the agent itself, which now owns the fix) asks nothing more of
    that line, and an agent with no move on a card that is where it was owes nothing either."""
    items = {"F1": finding("conceded", owner="dispatch"), "F2": finding(n=2)}
    reached = ["dispatch", "grok-reviewer"]
    assert reviews.answered([("F1", "raised")], items, reached, ROOM, ("dispatch",)) == reached
    assert reviews.answered([("F1", "conceded")], items, reached, ROOM, ()) == [
        "grok-reviewer"
    ]  # a fix to make
    assert reviews.answered([("F1", "raised"), ("F2", "raised")], items, reached, ROOM, ()) == []  # F2: both
    assert reviews.answered([("F2", "raised")], items, reached, ROOM, ("dispatch",)) == ["grok-reviewer"]
    assert reviews.answered([("F2", "")], items, reached, ROOM, ("dispatch",)) == ["grok-reviewer"]
    assert reviews.answered([("F9", "raised")], items, reached, ROOM, ()) == reached  # a card the board lost


# ------------------------------------------------------------------ one message
def test_labels_in_a_row_read_as_a_range() -> None:
    assert reviews.label_list([f"F{n}" for n in range(1, 12)]) == "F1 to F11"
    assert reviews.label_list(["F1", "F2"]) == "F1 and F2"
    assert reviews.label_list(["F1", "F3", "F4", "F5", "F9"]) == "F1, F3 to F5 and F9"


def test_one_agents_plain_raises_go_as_one_line_with_their_titles() -> None:
    lines = [reviews.told(finding(n=n), "codex-review", ROOM, side=("dispatch",)) for n in (1, 2, 3)]
    assert all(t is not None for t in lines)
    text = reviews.burst_text(lines)  # type: ignore[arg-type]
    assert text == (
        "Review board: F1 to F3 raised. @dispatch: concede or contest each.\n"
        "- F1: finding 1\n- F2: finding 2\n- F3: finding 3\n"
        'review(action="show") shows the board.'
    )
    assert reviews.burst_text(lines[:1]) == lines[0].line  # type: ignore[list-item,union-attr]


def test_mixed_lines_keep_each_ones_own_ask() -> None:
    """A raise with fix options and a question ask more agents than a plain raise: each keeps its line."""
    a = reviews.told(finding(), "codex-review", ROOM, side=("dispatch",))
    b = reviews.told(finding(n=2, options=("x", "y"), recommend=0), "codex-review", ROOM, side=("dispatch",))
    q = reviews.told(question(), "codex-review", ROOM)
    assert a is not None and b is not None and q is not None
    text = reviews.burst_text([a, b, q])
    assert text.startswith("Review board: F1 and F2 raised and Q1 asked.\n- F1 raised: finding 1. @dispatch:")
    assert f"- {b.body}\n" in text and f"- {q.body}\n" in text and "@grok-reviewer" in text


def test_a_burst_goes_after_the_quiet_period_or_at_most_a_few_seconds_after_its_first_line() -> None:
    assert not reviews.burst_due(0.0, 2.0, 4.9, 3.0, 5.0)
    assert reviews.burst_due(0.0, 2.0, 5.0, 3.0, 5.0)  # the most a first line waits
    assert reviews.burst_due(0.0, 0.5, 3.5, 3.0, 5.0)  # quiet since its last
    assert reviews.burst_wait(0.0, 0.5, 1.0, 3.0, 5.0) == pytest.approx(2.5)
    assert reviews.burst_wait(0.0, 4.0, 4.5, 3.0, 5.0) == pytest.approx(0.5)
    assert reviews.burst_wait(0.0, 0.0, 9.0, 3.0, 5.0) == 0.0


# ------------------------------------------------------------- the engine, for real
@dataclass
class Timer:
    at: float
    callback: Any
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


@dataclass
class Timers:
    """``call_later`` on the test's clock: ``run()`` fires what the clock has reached."""

    clock: FakeClock
    armed: list[Timer] = field(default_factory=list)

    def __call__(self, delay: float, callback: Any) -> Timer:
        t = Timer(self.clock.now() + delay, callback)
        self.armed.append(t)
        return t

    def run(self) -> None:
        for t in sorted(self.armed, key=lambda t: t.at):
            if not t.cancelled and t.at <= self.clock.now():
                t.cancelled = True
                t.callback()


class Service:
    """The broker's posting, as far as a board uses it: store, classify, and hand it to the engine."""

    def __init__(self, w: World):
        self.w = w

    def _post(self, room: Any, **kw: Any) -> Any:
        msg = self.w.store.insert_message(room.id, **kw)
        self.w.actions += self.w.engine.on_message(msg.id)
        return msg

    def post_notice(self, room: Any, text: str, level: str | None = None) -> None:
        self.w.store.insert_message(
            room.id, sender_name="switchboard", sender_kind="system", via="system", kind="notice", text=text
        )

    def host_person(self, host: str | None) -> int:
        return 0


@dataclass
class Review:
    w: World
    boards: Boards
    timers: Timers
    agents: dict[str, tuple[Participant, Membership]]

    def move(self, who: str, **params: Any) -> dict[str, Any]:
        """``who``'s ``review`` call, as ``AgentService.review`` makes it: the call first confirms a
        follow-up its Stop hook printed (``before_call``, what every tool call does), then the move."""
        p, m = self.agents[who]
        self.w.actions += self.w.engine.before_call(self.w.p(p))
        poster = {
            "sender_name": m.screen_name,
            "sender_kind": "agent",
            "sender_harness": p.harness,
            "sender_membership_id": m.id,
            "via": "mcp",
            "sender_host": p.host or None,
        }
        return self.boards.agent(self.w.room, who, {"url": PR, **params}, poster=poster)

    def read(self, who: str) -> None:
        """``who`` calls read(), as a stubbed line tells it to: what it shows reaches its context."""
        p, m = self.agents[who]
        self.w.actions += self.w.engine.before_call(self.w.p(p))
        _text, bid, _n, _more, acts = self.w.engine.pull(self.w.p(p), self.w.m(m), "read", 50)
        self.w.actions += acts
        if bid is not None:
            self.w.actions += self.w.engine.on_confirm(bid, "next_call")

    def lines(self) -> list[Any]:
        """The board's lines in the room: the movers' chat messages."""
        return [x for x in self.w.store.history(self.w.room.id) if x.kind == "chat"]

    def delivered(self, who: str, msg: Any) -> dict[str, Any] | None:
        m = self.agents[who][1]
        return next((d for d in self.w.store.deliveries(m.id) if d["message_id"] == msg.id), None)

    def later(self, s: float) -> None:
        """``s`` seconds pass: the board's timers, then the engine's tick (holds, the watchdog)."""
        self.w.clock.advance(s)
        self.timers.run()
        self.w.actions += self.w.engine.tick()


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config(human_name="alice"))  # the real quiet period: 3 s


def review_room(w: World, clock: FakeClock) -> tuple[Review, Any, Any]:
    """#switchboard-reviews from #884: the dispatching session (claude, its Stop hook parked on the
    broker), the PR's reviewer (codex, busy reviewing) and another reviewer not on this PR (parked too).
    Returns the review and the two parks."""
    timers = Timers(clock)
    dispatch_p, dispatch_m, dispatch_park = listener(w, "dispatch", "claude", status="idle")
    codex = session(w, "codex-review", "codex", status="busy")
    grok_p, grok_m, grok_park = listener(w, "grok-reviewer", "claude", status="idle")
    state = SimpleNamespace(
        store=w.store, clock=clock, cfg=w.cfg, service=Service(w), hub=SimpleNamespace(review=lambda *a: None)
    )
    r = Review(
        w,
        Boards(state, call_later=timers),
        timers,
        {"dispatch": (dispatch_p, dispatch_m), "codex-review": codex, "grok-reviewer": (grok_p, grok_m)},
    )
    r.move("dispatch", action="open")  # the dispatching session opened the board: it holds it
    return r, dispatch_park, grok_park


def test_many_raises_wake_only_the_author_side_once_and_a_bystander_never(w: World, clock: FakeClock) -> None:
    """The scene of #884: codex-review raises eleven findings at once. The dispatching session, which
    opened the board, is woken once, by one message; grok-reviewer, the other reviewer, gets no delivery
    of it at all, so no wake now, none when the unaddressed hold would end, and no again=yes. Then the
    dispatcher answers every card on the board and ends its turn: nothing comes back to it either.
    Fails on each part of #884: a raise that names every agent, a line per raise, and a board move that
    the broker doesn't take as the answer."""
    r, dispatch_park, grok_park = review_room(w, clock)
    for n in range(1, 12):
        r.move("codex-review", action="raise", title=f"finding {n}", where=f"a.py:{n}")
        clock.advance(0.2)
    assert r.lines() == [] and not woke(w, dispatch_park)  # held for the rest of the burst
    r.later(3.0)
    [line] = r.lines()
    assert line.text.startswith("Review board: F1 to F11 raised. @dispatch: concede or contest each.\n")
    assert all(f"- F{n}: finding {n}" in line.text for n in range(1, 12))  # none dropped
    assert woke(w, dispatch_park) and not woke(w, grok_park)
    assert r.delivered("grok-reviewer", line) is None  # not addressed: not delivered
    r.later(400.0)  # past the unaddressed hold (270 s) and the watchdog (120 s)
    assert not woke(w, grok_park)

    res = result(w, dispatch_park)
    assert res is not None and f"id={line.id} " in res["text"]
    w.actions += w.engine.on_hook_ack(res["batch_id"], res["ack"])  # its Stop hook printed the follow-up
    r.read("dispatch")  # the follow-up's turn reads the stubbed line: it is in context now
    assert r.delivered("dispatch", line)["state"] == "in_context"  # type: ignore[index]
    p = r.agents["dispatch"][0]
    for n in range(1, 12):
        r.move("dispatch", action="concede", item=f"F{n}")  # its own to fix: a notice each, no wake
        assert r.delivered("dispatch", line)["state"] == ("handled" if n == 11 else "in_context")  # type: ignore[index]
    park = stop(w, p)  # its turn ends without say() or pass(): the board was its answer
    assert w.store.recent_events(kinds=("requeue",), limit=5) == []
    r.later(400.0)
    assert not woke(w, park) and not woke(w, grok_park)
    assert len(r.lines()) == 1  # the eleven concessions were notices: nothing more for anyone to answer


def test_the_author_side_keeps_its_line_while_a_card_still_waits_on_it(w: World, clock: FakeClock) -> None:
    """Not waking the agent who must act is the other half: a dispatcher that answered five of eleven
    and ended its turn gets the line again (again=yes), as before. Fails if a board move answered the
    whole line."""
    r, dispatch_park, _grok = review_room(w, clock)
    for n in range(1, 12):
        r.move("codex-review", action="raise", title=f"finding {n}")
    r.later(3.0)
    [line] = r.lines()
    res = result(w, dispatch_park)
    w.actions += w.engine.on_hook_ack(res["batch_id"], res["ack"])  # type: ignore[index]
    r.read("dispatch")
    p = r.agents["dispatch"][0]
    for n in range(1, 6):
        r.move("dispatch", action="concede", item=f"F{n}")
    stop(w, p)
    d = r.delivered("dispatch", line)
    assert d is not None and d["redelivered"] == 1  # requeued once: it comes back as again=yes
    [ev] = w.store.recent_events(kinds=("requeue",), limit=5)
    assert ev.data["reason"] == "redeliver"


def test_a_stubbed_line_answered_on_the_board_gets_no_reminder(w: World, clock: FakeClock) -> None:
    """A line that reached the dispatcher as a stub ("call read()") and that it answered on the board
    without reading: the watchdog would remind it of an unanswered @mention after two minutes. It is
    answered, so nothing comes. Fails if only a say() or pass() could answer a board's line."""
    r, dispatch_park, _grok = review_room(w, clock)
    r.move("codex-review", action="raise", title="off by one")
    r.later(3.0)
    [line] = r.lines()
    res = result(w, dispatch_park)
    w.actions += w.engine.on_hook_ack(res["batch_id"], res["ack"])  # type: ignore[index]
    r.move("dispatch", action="contest", item="F1", reason="the loop starts at 1")
    assert r.delivered("dispatch", line)["state"] == "handled"  # type: ignore[index]
    park = stop(w, r.agents["dispatch"][0])
    r.later(130.0)  # past watchdog_s
    assert not woke(w, park)
    assert w.store.recent_events(kinds=("requeue", "watchdog_remind"), limit=5) == []


def test_when_the_board_knows_no_author_everyone_hears_and_the_first_answer_frees_the_rest(
    w: World, clock: FakeClock
) -> None:
    """A board switchboard opened for a link with no author recorded: a raise asks every other agent,
    as before. Once the dispatcher concedes it, the board knows its side, and grok-reviewer's copy of
    the line is answered: its turn's end brings nothing back. Fails if a bystander kept owing a line
    someone else answered (the again=yes reminders of #884)."""
    timers = Timers(clock)
    dispatch_p, dispatch_m, _ = listener(w, "dispatch", "claude", status="idle")
    codex = session(w, "codex-review", "codex", status="busy")
    grok_p, grok_m = session(w, "grok-reviewer", "claude", status="busy")
    state = SimpleNamespace(
        store=w.store, clock=clock, cfg=w.cfg, service=Service(w), hub=SimpleNamespace(review=lambda *a: None)
    )
    w.store.open_review(w.room.id, PR, "", "switchboard")
    r = Review(
        w,
        Boards(state, call_later=timers),
        timers,
        {"dispatch": (dispatch_p, dispatch_m), "codex-review": codex, "grok-reviewer": (grok_p, grok_m)},
    )
    r.move("codex-review", action="raise", title="off by one")
    r.later(3.0)
    [line] = r.lines()
    assert set(line.mentions) == {"dispatch", "grok-reviewer"}
    out = w.hook(grok_p, "PostToolUse", ok=True)  # grok is busy: it hears of it after a tool call
    assert out is not None and f"id={line.id} " in out.text
    w.actions += w.engine.on_hook_ack(out.batch_id, out.ack)
    r.read("grok-reviewer")
    assert r.delivered("grok-reviewer", line)["state"] == "in_context"  # type: ignore[index]
    r.move("dispatch", action="concede", item="F1")
    assert r.delivered("grok-reviewer", line)["state"] == "handled"  # type: ignore[index]
    w.hook(grok_p, "Stop")
    assert w.store.recent_events(kinds=("requeue",), limit=5) == []


def test_a_concur_answers_the_question_for_that_agent_only(w: World, clock: FakeClock) -> None:
    """A question is every agent's to pick on; one that concurs has answered it (a board move, not a
    say), the one that hasn't still owes it."""
    r, _d, _g = review_room(w, clock)
    r.move(
        "codex-review", action="ask", title="Before or after tax?", options=["before", "after"], recommend=0
    )
    r.later(3.0)
    [line] = r.lines()
    assert set(line.mentions) == {"dispatch", "grok-reviewer"}
    r.move("grok-reviewer", action="concur", item="Q1", recommend=0)
    assert r.delivered("grok-reviewer", line)["state"] == "handled"  # type: ignore[index]
    assert r.delivered("dispatch", line)["state"] != "handled"  # type: ignore[index]


def test_the_first_line_waits_a_few_seconds_at_most_and_nothing_is_lost(w: World, clock: FakeClock) -> None:
    """A reviewer that raises one finding a second for eight seconds: the first message goes 5 s after
    the first raise, not after the last, and every raise is in one of the messages. Fails if a burst
    held its first line for as long as raises kept coming, or dropped one."""
    r, dispatch_park, _g = review_room(w, clock)
    first_at = clock.now()
    for n in range(1, 9):
        r.move("codex-review", action="raise", title=f"finding {n}")
        r.later(1.0)
        if clock.now() - first_at < 5.0:
            assert r.lines() == []
    assert woke(w, dispatch_park)
    msgs = r.lines()
    assert msgs[0].ts - first_at == pytest.approx(5.0)
    r.later(3.0)
    text = "\n".join(x.text for x in r.lines())
    assert all(f"- F{n}: finding {n}" in text for n in range(1, 9))
    assert len(r.lines()) == 2


def test_a_say_and_any_other_move_come_after_the_lines_already_made(w: World, clock: FakeClock) -> None:
    """The room reads in order: a held burst is posted before the raiser's next say, and before anyone's
    other move on the board, rather than a few seconds after it."""
    r, _d, _g = review_room(w, clock)
    r.move("codex-review", action="raise", title="off by one")
    r.boards.flush_actor(w.room.id, "codex-review")  # what AgentService.say does first
    assert [x.text.split(":")[0] for x in r.lines()] == ["Review board"]
    r.move("codex-review", action="raise", title="rounding")
    r.move("dispatch", action="concede", item="F1")  # another move flushes the room's bursts first
    assert [x.text for x in r.lines()][1].startswith("Review board: F2 raised: rounding. @dispatch:")


def test_shutdown_posts_what_is_held(w: World, clock: FakeClock) -> None:
    """A restart within the burst's few seconds posts it rather than lose it."""
    r, dispatch_park, _g = review_room(w, clock)
    r.move("codex-review", action="raise", title="off by one")
    r.boards.close()
    [line] = r.lines()
    assert "F1 raised: off by one" in line.text and woke(w, dispatch_park)
    assert all(t.cancelled for t in r.timers.armed)


def test_without_a_quiet_period_each_line_goes_at_once(tmp_path: Path, clock: FakeClock) -> None:
    """``quiet_s = 0`` (the integration tests' fast config) turns the wait off: nothing is held."""
    w = World(tmp_path, clock, Config(human_name="alice").with_delivery(quiet_s=0.0, max_hold_s=0.0))
    r, _d, _g = review_room(w, clock)
    r.move("codex-review", action="raise", title="off by one")
    assert len(r.lines()) == 1 and r.timers.armed == []


def test_closing_the_board_answers_every_line_on_it(w: World, clock: FakeClock) -> None:
    """A closed board takes no moves, so nobody owes its lines: the dispatcher that closes it with a
    finding open is not reminded of that finding."""
    r, dispatch_park, _g = review_room(w, clock)
    r.move("codex-review", action="raise", title="off by one")
    r.later(3.0)
    [line] = r.lines()
    r.move("dispatch", action="close", reason="superseded by #867")
    assert r.delivered("dispatch", line)["state"] == "handled"  # type: ignore[index]
    assert rules.redeliver_ids(w.store.in_context_items(r.agents["dispatch"][1].id)) == []
