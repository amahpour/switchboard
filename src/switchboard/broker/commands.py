"""Human slash commands (DESIGN.md §10).

Reachable only through ``human.command`` over the UDS (peer-checked) or an
authenticated web session. An agent's ``say("/pause")`` is stored literally
and never parsed. Commands that reduce activity need ``human_cli``; commands
that raise it need ``human`` (the web session, or test trust). ``/review``
(§26) posts one chat message as the human, so it needs what ``switchboard say``
needs: ``human_cli``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from switchboard.broker import review
from switchboard.delivery.rules import parse_mentions
from switchboard.models import SCREEN_NAME_RE, Room

if TYPE_CHECKING:  # pragma: no cover
    from switchboard.broker.service import RoomService

ROLE_RANK = {"anon": 0, "human_cli": 1, "human": 2}
MAX_BUDGET = 1_000_000
MAX_HOPS = 1000  # /hops <n>: 0..MAX_HOPS; 0 turns the loop guard off

HELP_TEXT = """\
commands (type them in the web UI; the CLI runs them with `switchboard cmd '#room' /...`):
  /pause              freeze every agent wake in this room
  /resume             unfreeze the room and reset the loop guard (web only)
  /budget             show the wake budget
  /budget <n>         set the wakes left this hour (raising it is web only)
  /hops               show the loop guard: agent messages in a row / the limit
  /hops <n>           set the loop guard limit, 0-1000; 0 turns it off
                      (raising it or turning it off is web only)
  /hold <name>        stop delivering to one agent (messages stay queued)
  /release <name>     resume delivery to a held agent (web only)
  /kick <name>        remove an agent and revoke its membership
  /review <reviewer> <author> [note]
                      ask one agent to review another's recent work: its
                      changes first, then its session transcript (needs
                      agentsview; the note is added to the request)
  /who                list members
  /status             room and delivery status
  /help               this list
  //text              post text that starts with a single /"""

_NO_ARGS = frozenset({"pause", "resume", "who", "status", "help"})
_ONE_NAME = frozenset({"kick", "hold", "release"})
_ONE_NUMBER = {"budget": MAX_BUDGET, "hops": MAX_HOPS}  # /name [n], 0 <= n <= max
KNOWN = _NO_ARGS | _ONE_NAME | set(_ONE_NUMBER) | {"review"}

_STATIC_ROLES = {
    "pause": "human_cli",
    "resume": "human",
    "kick": "human_cli",
    "hold": "human_cli",
    "release": "human",
    "who": "human_cli",
    "status": "human_cli",
    "help": "human_cli",
    "review": "human_cli",  # posts a human chat message, like `switchboard say`
}


class CommandError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Command:
    name: str
    args: tuple[str, ...]
    raw: str


@dataclass(frozen=True)
class Actor:
    role: str  # 'human' or 'human_cli'
    via: str  # 'web' or 'cli'
    chain: str | None = None  # short process chain for CLI callers


@dataclass
class Result:
    ok: bool
    text: str
    notice: str | None = None  # persisted room notice (state changes, CLI audit)
    post: str | None = None  # a chat message to post as the human (/review)
    post_skip: tuple[int, ...] = ()  # memberships that get no delivery of ``post``
    warnings: tuple[str, ...] = ()  # warn-level room notices, posted after ``post``
    room_changed: bool = False
    members_changed: bool = False
    event: str | None = None
    event_data: dict = field(default_factory=dict)


def parse_command(text: str) -> Command:
    if not isinstance(text, str):
        raise CommandError("bad_request", "command must be text")
    raw = text.strip()
    if not raw.startswith("/") or raw.startswith("//"):
        raise CommandError("bad_request", "commands start with a single '/'; try /help")
    parts = raw[1:].split()
    if not parts:
        raise CommandError("bad_request", "empty command; try /help")
    name, args = parts[0].lower(), tuple(parts[1:])
    if name not in KNOWN:
        raise CommandError("bad_request", f"unknown command /{name}; try /help")
    if name == "review":
        return _parse_review(args, raw)
    if name in _NO_ARGS and args:
        raise CommandError("bad_request", f"/{name} takes no arguments")
    if name in _ONE_NAME:
        if len(args) != 1:
            raise CommandError("bad_request", f"usage: /{name} <name>")
        if not SCREEN_NAME_RE.match(args[0].lower().lstrip("@")):
            raise CommandError("bad_request", f"/{name}: not a valid screen name")
        args = (args[0].lower().lstrip("@"),)
    if name in _ONE_NUMBER:
        top = _ONE_NUMBER[name]
        if len(args) > 1:
            raise CommandError("bad_request", f"usage: /{name} [n]")
        if args:
            if not args[0].isascii() or not args[0].isdigit():
                raise CommandError("bad_request", f"/{name} needs a whole number between 0 and {top}")
            digits = args[0].lstrip("0") or "0"
            # length first: int() refuses strings over 4300 digits with a ValueError
            if len(digits) > len(str(top)) or int(digits) > top:
                raise CommandError("bad_request", f"/{name} must be between 0 and {top}")
            n = int(digits)
            args = (str(n),)
    return Command(name=name, args=args, raw=raw)


def _screen_name(cmd: str, arg: str) -> str:
    n = arg.lower().lstrip("@")
    if not SCREEN_NAME_RE.match(n):
        raise CommandError("bad_request", f"/{cmd}: not a valid screen name")
    return n


def _parse_review(args: tuple[str, ...], raw: str) -> Command:
    """``/review <reviewer> <author> [note...]``: two member names, then free text."""
    if len(args) < 2:
        raise CommandError("bad_request", "usage: /review <reviewer> <author> [note]")
    reviewer, author = _screen_name("review", args[0]), _screen_name("review", args[1])
    if reviewer == author:
        raise CommandError("bad_request", "/review: the reviewer and the author must be two different members")
    note = review.clean_note(" ".join(args[2:]))
    return Command(name="review", args=(reviewer, author, note) if note else (reviewer, author), raw=raw)


def hops_raise(new: int, old: int) -> bool:
    """Does a hop limit of ``new`` (replacing ``old``) allow more agent activity?
    0 means no limit, so turning the guard off is a raise and turning it on is not."""
    if new == old:
        return False
    if new == 0:
        return True
    if old == 0:
        return False
    return new > old


def required_role(cmd: Command, room: Room) -> str:
    if cmd.name == "budget":
        if not cmd.args:
            return "human_cli"
        return "human_cli" if int(cmd.args[0]) <= room.budget_remaining else "human"
    if cmd.name == "hops":
        if not cmd.args:
            return "human_cli"
        return "human" if hops_raise(int(cmd.args[0]), room.hop_limit) else "human_cli"
    return _STATIC_ROLES[cmd.name]


def check_role(cmd: Command, room: Room, actor: Actor) -> None:
    need = required_role(cmd, room)
    if ROLE_RANK.get(actor.role, 0) < ROLE_RANK[need]:
        # need == "human" for these two means a value was given and it raises activity
        if cmd.name == "budget" and need == "human":
            what = "raising the budget"
        elif cmd.name == "hops" and need == "human":
            what = "turning the loop guard off" if cmd.args[0] == "0" else "raising the hop limit"
        else:
            what = f"/{cmd.name}"
        raise CommandError(
            "forbidden",
            f"{what} needs your web session: type it in the switchboard web UI",
        )


def _hhmmss(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def hops_state(room: Room) -> str:
    """``hops 3/30``, or ``hops 3, loop guard off`` for a limit of 0."""
    if room.hop_limit == 0:
        return f"hops {room.hop_count}, loop guard off"
    return f"hops {room.hop_count}/{room.hop_limit}"


def _hops_show(room: Room, human: str) -> str:
    if room.hop_limit == 0:
        text = (f"{room.name}: {hops_state(room)} (agents may message each other without limit;"
                " /hops <n> turns it back on)")
    else:
        text = (f"{room.name}: {hops_state(room)} (the room pauses after {room.hop_limit} agent"
                f" messages in a row with none from {human})")
    return text + _hops_pause_hint(room)


def _hops_pause_hint(room: Room) -> str:
    """What the new limit means for a room that is paused, or about to be."""
    if room.paused and room.paused_reason == "loop guard":
        if room.hop_limit == 0 or room.hop_count < room.hop_limit:
            return "; still paused by the loop guard: /resume to continue"
        return "; still paused by the loop guard: /resume to continue (it resets the count)"
    if room.paused:
        return f"; the room is paused ({room.paused_reason or 'paused'}) until /resume"
    if room.hop_limit and room.hop_count >= room.hop_limit:
        return "; the next agent message pauses the room"
    return ""


def _hops_set(room: Room, n: int, svc: "RoomService") -> Result:
    """Set the room's hop limit. It never un-pauses: a room the loop guard paused needs /resume."""
    old = room.hop_limit
    if n == old:
        return Result(True, f"{room.name}: the hop limit is already {n}; {hops_state(room)}"
                            + _hops_pause_hint(room))
    room = svc.store.set_hop_limit(room.id, n)
    guard_paused = room.paused and room.paused_reason == "loop guard"
    if n == 0:
        head = f"loop guard off (hop limit 0, was {old}); agents may message each other without limit"
        notice = f"turned the loop guard off (hop limit was {old})"
        tail = ("; the room is still paused by the loop guard: /resume to continue" if guard_paused
                else _hops_pause_hint(room))
    else:
        if old == 0:
            head, notice = f"loop guard on, hop limit {n}", f"turned the loop guard on with a hop limit of {n}"
        else:
            head, notice = f"hop limit set to {n} (was {old})", f"set the hop limit to {n} (was {old})"
        if guard_paused and room.hop_count < n:
            # most likely why it was raised: say how to get going again
            tail = f"; now {room.hop_count}/{n}; /resume to continue"
        else:
            tail = f"; {hops_state(room)}" + _hops_pause_hint(room)
    return Result(
        True,
        f"{room.name}: {head}{tail}",
        notice=notice,
        room_changed=True,
        event="hop_limit_set",
        event_data={"old": old, "new": n},
    )


def _review(cmd: Command, room: Room, svc: "RoomService") -> Result:
    """Check the pair and agentsview, then hand back the request to post (§26). Nothing is
    posted or recorded here: ``RoomService.command`` posts it as an ordinary human message."""
    store = svc.store
    members = []
    for n in cmd.args[:2]:
        m = store.find_member(room.id, n)
        if m is None:
            raise CommandError("not_found", f"no such member in {room.name}: {n}")
        members.append(m)
    rev, author = members
    p = store.get_participant(author.participant_id)
    tid, why = (review.participant_transcript(p, author.name, svc.cfg) if p
                else (None, f"{author.name} has no transcript"))
    if tid is None:
        raise CommandError("bad_request", f"/review: {why}")
    run = review.agentsview_command(svc.cfg)
    if run is None:
        raise CommandError("bad_request", review.missing_text(svc.cfg))
    note = cmd.args[2] if len(cmd.args) > 2 else ""
    limit = svc.cfg.delivery.max_msg_chars
    base = review.request_text(rev.name, author.name, tid, cmd=run)
    if len(base) > limit:
        raise CommandError("bad_request", f"/review: its request needs {len(base)} characters, but"
                                          f" [delivery] max_msg_chars is {limit}")
    text = review.request_text(rev.name, author.name, tid, cmd=run, note=note)
    if len(text) > limit:
        fits = max(0, limit - len(base) - 1)
        raise CommandError("bad_request", f"/review: the note is too long ({len(note)} characters;"
                                          f" at most {fits} fit in one message)")
    lines = [f"asked {rev.name} to review {author.name}'s recent work (agentsview id {tid})"]
    if author.name.lower() in parse_mentions(note, [author.name]):
        # the note @mentions the author, but the author gets no delivery of the request
        lines.append(f"{author.name} won't get this request (it is about its work): post to it separately")
    if rev.held:
        lines.append(f"{rev.name} is held: it gets the request after /release {rev.name}")
    if room.paused:
        lines.append(f"{room.name} is paused: the request goes out after /resume")
    warn = review.approvals_warning(rev.name, rev.approval_mode)
    if warn:
        lines.append(warn)
    return Result(
        True,
        "\n".join(lines),
        post=text,
        # the request is about the author, not for it: waking it would spend a turn and add
        # to the transcript the reviewer is about to read (§26)
        post_skip=(author.membership_id,),
        warnings=(warn,) if warn else (),
        event="review",
        event_data={"reviewer": rev.membership_id, "author": author.membership_id, "harness": author.harness},
    )


def apply(cmd: Command, room: Room, actor: Actor, svc: "RoomService") -> Result:
    """Run a parsed command. Role checks happen here too (defence in depth)."""
    store = svc.store
    # Refill first: whether ``/budget n`` raises or lowers is judged against the
    # budget that is really left now, not a window that has already rolled over.
    room = store.refill_budget(room.id)
    check_role(cmd, room, actor)
    name = cmd.name
    if name == "help":
        return Result(True, HELP_TEXT)
    if name == "who":
        return Result(True, svc.who_text(room))
    if name == "status":
        return Result(True, svc.status_text(room))
    if name == "pause":
        if room.paused:
            return Result(True, f"{room.name} is already paused ({room.paused_reason or 'by you'})")
        store.set_paused(room.id, True, "paused by " + svc.cfg.human_name)
        return Result(
            True,
            f"{room.name} paused: no agent wakes until /resume",
            notice="paused the room; no agent wakes until /resume",
            room_changed=True,
            event="pause",
        )
    if name == "resume":
        was = room.paused
        store.set_paused(room.id, False)
        return Result(
            True,
            f"{room.name} resumed; loop guard reset" if was else f"{room.name} was not paused; loop guard reset",
            notice="resumed the room; loop guard reset",
            room_changed=True,
            event="resume",
        )
    if name == "budget":
        if not cmd.args:
            return Result(
                True,
                f"{room.name}: {room.budget_remaining}/{room.budget_per_hour} wakes left this hour"
                f" (refills at {_hhmmss(room.budget_reset_at)})",
            )
        n = int(cmd.args[0])
        old = room.budget_remaining
        store.set_budget(room.id, n)
        return Result(
            True,
            f"{room.name}: budget set to {n} (was {old}); refills to {room.budget_per_hour}"
            f" at {_hhmmss(room.budget_reset_at)}",
            notice=f"set the wake budget to {n} (was {old})",
            room_changed=True,
            event="budget_set",
            event_data={"old": old, "new": n},
        )
    if name == "hops":
        if not cmd.args:
            return Result(True, _hops_show(room, svc.cfg.human_name))
        # Never un-pauses: a room the loop guard paused still needs /resume.
        return _hops_set(room, int(cmd.args[0]), svc)
    if name == "review":
        return _review(cmd, room, svc)
    # one-name commands
    target = cmd.args[0]
    member = store.find_member(room.id, target)
    if member is None:
        raise CommandError("not_found", f"no such member in {room.name}: {target}")
    if name == "hold":
        if member.held:
            return Result(True, f"{member.name} is already held")
        store.set_held(member.membership_id, True)
        return Result(
            True,
            f"holding delivery to {member.name}; messages stay queued until /release",
            notice=f"held delivery to {member.name}",
            members_changed=True,
            event="hold",
            event_data={"membership_id": member.membership_id},
        )
    if name == "release":
        if not member.held:
            return Result(True, f"{member.name} is not held")
        store.set_held(member.membership_id, False)
        return Result(
            True,
            f"released {member.name}",
            notice=f"released {member.name}",
            members_changed=True,
            event="release",
            event_data={"membership_id": member.membership_id},
        )
    if name == "kick":
        svc.kick(room, member)
        return Result(
            True,
            f"kicked {member.name}; its membership is revoked",
            members_changed=True,
            event="kick",
            event_data={"membership_id": member.membership_id},
        )
    raise CommandError("bad_request", f"unknown command /{name}")  # pragma: no cover
