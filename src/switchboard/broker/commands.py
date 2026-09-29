"""Human slash commands (DESIGN.md §10).

Reachable only through ``human.command`` over the UDS (peer-checked) or an
authenticated web session. An agent's ``say("/pause")`` is stored literally
and never parsed. Commands that reduce activity need ``human_cli``; commands
that raise it need ``human`` (the web session, or test trust). ``/catchup``
(§26, and ``/review``, its alias until 0.4) posts one chat message as the human,
so it needs what ``switchboard say`` needs: ``human_cli``. ``/close`` (§28.3) ends every
membership, so it reduces activity: ``human_cli``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from switchboard.broker import catchup
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
  /close              close this room: every agent leaves, the history is kept;
                      reopen it from Closed rooms in the web UI
  /catchup <agent> [on <member> | on "<topic>"] [note]
                      one agent gets up to speed from the others' session
                      history, with its own tool (e.g. AgentsView):
    /catchup codex-1 on claude-1                on claude-1's work
    /catchup codex-1 on "sprint cleanup"        on a topic, across the room
    /catchup codex-1                            on the room since it joined
    /catchup codex-1 on claude-1 pick it apart  plus a critical second opinion
  /review <agent> <member> [note]
                      old name of /catchup <agent> on <member> (until 0.4)
  /who                list members
  /status             room and delivery status
  /help               this list
  //text              post text that starts with a single /"""

_NO_ARGS = frozenset({"pause", "resume", "who", "status", "help", "close"})
_ONE_NAME = frozenset({"kick", "hold", "release"})
_ONE_NUMBER = {"budget": MAX_BUDGET, "hops": MAX_HOPS}  # /name [n], 0 <= n <= max
KNOWN = _NO_ARGS | _ONE_NAME | set(_ONE_NUMBER) | {"catchup", "review"}

_STATIC_ROLES = {
    "pause": "human_cli",
    "resume": "human",
    "kick": "human_cli",
    "close": "human_cli",  # every agent leaves: it reduces activity, like /kick (§28.3)
    "hold": "human_cli",
    "release": "human",
    "who": "human_cli",
    "status": "human_cli",
    "help": "human_cli",
    "catchup": "human_cli",  # posts a human chat message, like `switchboard say`
    "review": "human_cli",  # the /catchup alias
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
    post: str | None = None  # a chat message to post as the human (/catchup)
    post_skip: tuple[int, ...] = ()  # memberships that get no delivery of ``post``
    warnings: tuple[str, ...] = ()  # warn-level room notices, posted after ``post``
    room_changed: bool = False
    members_changed: bool = False
    event: str | None = None
    event_data: dict = field(default_factory=dict)
    # the command published everything itself (/close): ``RoomService.command`` returns the
    # reply at once, since its post-processing would publish under the renamed room
    done: bool = False


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
    if name == "catchup":
        # parsed from the raw text: a quoted topic keeps its spaces
        return _parse_catchup(raw[1:].split(None, 1)[1] if args else "", raw)
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


CATCHUP_USAGE = '/catchup <agent> [on <member> | on "<topic>"] [note]'
_OPEN_QUOTES = '"\u201c'  # a straight or a curly opening double quote
_CLOSE_QUOTES = '"\u201d'
_SINGLE_QUOTES = "'\u2018\u2019"  # not a topic's quotes: said so, instead of "not a valid screen name"


def _after_on(rest: str) -> str | None:
    """The text after a leading ``on`` word (case-insensitive), or None when ``rest``
    doesn't start with one. ``on"topic"`` counts too."""
    if rest[:2].lower() != "on" or (len(rest) > 2 and not rest[2].isspace() and rest[2] not in _OPEN_QUOTES):
        return None
    return rest[2:].lstrip()


def _parse_catchup(text: str, raw: str) -> Command:
    """``/catchup <agent> [on <member> | on "<topic>"] [note...]``.

    ``args``: ``(agent, mode, target, note)``, mode one of ``member`` (target: its name),
    ``topic`` (target: the topic, cleaned, 1-200 characters) or ``room`` (target '').
    The note is free text, cleaned like chat text. An unquoted topic of several words
    reads as a member name and its note, and fails as a member name."""
    words = text.split(None, 1)
    if not words:
        raise CommandError("bad_request", f"usage: {CATCHUP_USAGE}")
    agent = _screen_name("catchup", words[0])
    rest = words[1].strip() if len(words) > 1 else ""
    after = _after_on(rest)
    if after is None:
        return Command(name="catchup", args=(agent, "room", "", catchup.clean_text(rest)), raw=raw)
    if not after:
        raise CommandError("bad_request", f"/catchup: on whom, or on what? usage: {CATCHUP_USAGE}")
    if after[0] in _OPEN_QUOTES:
        end = next((i for i, ch in enumerate(after[1:], 1) if ch in _CLOSE_QUOTES), None)
        if end is None:
            raise CommandError("bad_request", "/catchup: the topic needs a closing double quote")
        topic = catchup.clean_text(after[1:end])
        if not topic:
            raise CommandError("bad_request", "/catchup: the topic is empty")
        if len(topic) > catchup.TOPIC_MAX:
            raise CommandError("bad_request", f"/catchup: the topic is too long ({len(topic)} characters;"
                                              f" at most {catchup.TOPIC_MAX})")
        return Command(name="catchup", args=(agent, "topic", topic, catchup.clean_text(after[end + 1:])),
                       raw=raw)
    if after[0] in _SINGLE_QUOTES:
        inner = after[1:]
        end = next((i for i, ch in enumerate(inner) if ch in _SINGLE_QUOTES), len(inner))
        raise CommandError("bad_request", f'/catchup: a topic goes in double quotes: /catchup {agent} on'
                                          f' "{catchup.clean_text(inner[:end])}"')
    parts = after.split(None, 1)
    member = _screen_name("catchup", parts[0])
    if member == agent:
        raise CommandError("bad_request", "/catchup: an agent can't catch up on itself; name another member")
    note = catchup.clean_text(parts[1]) if len(parts) > 1 else ""
    return Command(name="catchup", args=(agent, "member", member, note), raw=raw)


def _parse_review(args: tuple[str, ...], raw: str) -> Command:
    """``/review <reviewer> <author> [note...]``, the alias of ``/catchup <reviewer> on <author>
    review it critically[: note]`` (until 0.4). Same ``args`` as ``/catchup``."""
    if len(args) < 2:
        raise CommandError("bad_request", "usage: /review <reviewer> <author> [note]")
    reviewer, author = _screen_name("review", args[0]), _screen_name("review", args[1])
    if reviewer == author:
        raise CommandError("bad_request", "/review: the reviewer and the author must be two different members")
    note = catchup.review_note(catchup.clean_text(" ".join(args[2:])))
    return Command(name="review", args=(reviewer, "member", author, note), raw=raw)


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


def _catchup(cmd: Command, room: Room, svc: "RoomService") -> Result:
    """Check the names, name each subject's session, and hand back the request to post
    (§26). Nothing is posted or recorded here: ``RoomService.command`` posts it as an
    ordinary human message. Also ``/review`` (the alias), whose reply says so first."""
    agent_name, mode, target, note = cmd.args
    members = svc.store.members(room.id)
    by_name = {m.name.lower(): m for m in members}
    agent = by_name.get(agent_name)
    if agent is None:
        raise CommandError("not_found", f"no such member in {room.name}: {agent_name}")
    if mode == "member":
        subject = by_name.get(target)
        if subject is None:
            # "on sprint cleanup": most likely an unquoted topic, unless it is a single word (a
            # typo) or the name of a member that left
            hint = ""
            if cmd.name == "catchup" and note and not svc.store.name_used_by_other(room.id, target, -1, 0.0):
                hint = f' (a topic goes in double quotes: /catchup {agent.name} on "{target} {note}")'
            raise CommandError("not_found", f"no such member in {room.name}: {target}{hint}")
        subjects = [subject]
    else:
        subjects = [m for m in members if m.membership_id != agent.membership_id]
        if not subjects:
            raise CommandError("bad_request", f"/{cmd.name}: {agent.name} is the only agent in {room.name}:"
                                              " there is nobody to catch up on")
    since = catchup.window_start(mode, svc.clock.now(), agent.joined_at)
    handles = [catchup.handle(m, svc.store.get_participant(m.participant_id), svc.cfg, agent.host)
               for m in subjects]
    topic = target if mode == "topic" else ""
    limit = svc.cfg.delivery.max_msg_chars
    # the alias's fixed "review it critically" is part of what the request needs; the rest
    # of the note is what the human typed
    fixed, own = "", note
    if cmd.name == "review":
        fixed = catchup.REVIEW_NOTE
        own = note[len(fixed) + 2:]  # after "review it critically: "; '' when there is none
    base = catchup.request_text(agent.name, mode, handles, since=since, max_chars=limit, topic=topic, note=fixed)
    text = catchup.request_text(agent.name, mode, handles, since=since, max_chars=limit, topic=topic, note=note)
    if len(base) > limit:
        raise CommandError("bad_request", f"/{cmd.name}: its request needs {len(base)} characters, but"
                                          f" [delivery] max_msg_chars is {limit}")
    if len(text) > limit:
        fits = limit - (len(text) - len(own))
        raise CommandError("bad_request", f"/{cmd.name}: the note is too long ({len(own)} characters;"
                                          f" at most {fits} fit in one message)")
    lines = [catchup.REVIEW_DEPRECATED] if cmd.name == "review" else []
    what = {"member": f"{subjects[0].name}'s work", "topic": f'"{topic}" across {len(handles)} session(s)',
            "room": "what the room did"}[mode]
    lines.append(f"asked {agent.name} to catch up on {what} since {catchup.when(since)}; it got:")
    lines += [f"  {h.line()}" + (f" ({h.why})" if h.why else "") for h in handles]
    for n in parse_mentions(f"{topic} {note}", [m.name for m in subjects]):
        # the topic or note @mentions a subject, but subjects get no delivery of the request
        lines.append(f"{n} won't get this request (it is about its work): post to it separately")
    if agent.held:
        lines.append(f"{agent.name} is held: it gets the request after /release {agent.name}")
    if room.paused:
        lines.append(f"{room.name} is paused: the request goes out after /resume")
    warn = catchup.approvals_warning(agent.name, agent.approval_mode)
    if warn:
        lines.append(warn)
    data: dict[str, Any] = {"agent": agent.membership_id, "mode": mode,
                  "subjects": [m.membership_id for m in subjects],
                  "with_id": sum(1 for h in handles if h.sid)}
    if cmd.name == "review":
        data["alias"] = "review"
    return Result(
        True,
        "\n".join(lines),
        post=text,
        # the request is about the subjects, not for them: waking one would spend a turn and
        # add to the session the agent is about to read (§26)
        post_skip=tuple(m.membership_id for m in subjects),
        warnings=(warn,) if warn else (),
        event="catchup",
        event_data=data,
    )


def apply(cmd: Command, room: Room, actor: Actor, svc: "RoomService") -> Result:
    """Run a parsed command. Role checks happen here too (defence in depth)."""
    store = svc.store
    # Refill first: whether ``/budget n`` raises or lowers is judged against the
    # budget that is really left now, not a window that has already rolled over.
    room = store.refill_budget(room.id)
    check_role(cmd, room, actor)
    name = cmd.name
    if name == "close":
        return Result(True, svc.close_room(room, actor), done=True)
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
    if name in ("catchup", "review"):
        return _catchup(cmd, room, svc)
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
