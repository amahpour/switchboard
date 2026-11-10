"""Human slash commands (DESIGN.md §10).

Reachable only through ``human.command`` over the UDS (peer-checked) or an
authenticated web session. An agent's ``say("/pause")`` is stored literally
and never parsed. Commands that reduce activity need ``human_cli``; commands
that raise it need ``human`` (the web session, or test trust). ``/catchup``
(§26) and ``/review`` (§37.8) post one chat message as the human, so they need what
``switchboard say`` needs: ``human_cli``. (``/review`` was ``/catchup``'s alias in 0.3,
removed in 0.4; it opens a review board now.) ``/close`` (§28.3) ends every membership,
so it reduces activity: ``human_cli``. ``/quiet`` reduces what wakes an agent, so
``human_cli``; ``/unquiet`` raises it, so ``human`` (§50). ``/meeting`` joins a call,
which raises what the room's agents can be asked to do, so ``human`` (§52); so do its
``voice on|off`` (agents speaking in the call) and ``snapshot`` (a picture of its shared screen).
``/room voice|listen on|off`` (§52.3) sets what every call in the room starts with, so it raises
what agents can do and needs ``human`` too; ``/room`` alone only reads them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from switchboard import reviews
from switchboard.broker import catchup
from switchboard.delivery.rules import parse_mentions
from switchboard.meeting.links import PLATFORM_LABEL, LinkError, MeetingLink, parse_link
from switchboard.meeting.screen import Asker
from switchboard.models import MAX_BUDGET, MAX_HOPS, SCREEN_NAME_RE, Room
from switchboard.reviews import ReviewError
from switchboard.store import Conflict

if TYPE_CHECKING:  # pragma: no cover
    from switchboard.broker.service import RoomService

ROLE_RANK = {"anon": 0, "human_cli": 1, "human": 2}

HELP_TEXT = """\
commands (type them in the web UI; the CLI runs them with `switchboard cmd '#room' /...`):
  /pause              freeze every agent wake in this room
  /resume             unfreeze the room and reset the loop guard (web only)
  /rename <new-name>  rename this room (web only)
  /budget             show the wake budget
  /budget <n>         set the wakes left this hour (raising it is web only)
  /hops               show the loop guard: agent messages in a row / the limit
  /hops <n>           set the loop guard limit, 0-1000; 0 turns it off
                      (raising it or turning it off is web only)
  /hold <name>        stop delivering to one agent (messages stay queued)
  /release <name>     resume delivery to a held agent (web only)
  /quiet <name>       wake this agent only for messages addressed to it
  /unquiet <name>     wake it for every message again (web only)
  /wake <name>        wake an agent now for the messages waiting for it (web only)
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
  /review <url> [note]
                      open this room's review board for a pull request or merge
                      request, and ask its agents to review it on the board
  /pin <id>           pin a chat message (its #number): every agent that joins is shown
                      it, and the web UI lists it in the room header; at most 5 (web only)
  /unpin <id>         unpin it
  /meeting            say whether this room is transcribing a call
  /meeting <link>     transcribe a Zoom, Meet or Teams call here (web only)
  /meeting <link> mine  ask your own agent to host it; no model sees the link (web only)
  /meeting leave      leave the call and stop transcribing (web only)
  /meeting listen <name> [off]  wake this agent on call chatter (on: web only)
  /meeting voice on   let agents speak in the call: a say() that starts with @call
                      is read aloud to everyone on it. Off when a call starts unless /room voice on (web only)
  /meeting voice off  stop that (web only)
  /meeting snapshot   post a picture of the call's shared screen (web only)
  /room               show this room's call settings (hands-free calls)
  /room voice on|off  every call here starts with Voice on (web only)
  /room listen on|off every agent here listens on calls (web only)
  /who                list members
  /status             room and delivery status
  /help               this list
  //text              post text that starts with a single /"""

_NO_ARGS = frozenset({"pause", "resume", "who", "status", "help", "close"})
_ONE_NAME = frozenset({"kick", "hold", "release", "wake", "quiet", "unquiet"})
_ONE_NUMBER = {"budget": MAX_BUDGET, "hops": MAX_HOPS}  # /name [n], 0 <= n <= max
_ONE_MESSAGE = frozenset({"pin", "unpin"})  # /name <message id>
KNOWN = (
    _NO_ARGS
    | _ONE_NAME
    | set(_ONE_NUMBER)
    | _ONE_MESSAGE
    | {"catchup", "review", "rename", "meeting", "room"}
)

_STATIC_ROLES = {
    "pause": "human_cli",
    "resume": "human",
    "rename": "human",
    "kick": "human_cli",
    "close": "human_cli",  # every agent leaves: it reduces activity, like /kick (§28.3)
    "hold": "human_cli",
    "release": "human",
    "wake": "human",  # raises activity: a wake now, past the hold and the budget (#137)
    "quiet": "human_cli",  # fewer wakes: unaddressed chatter never starts a turn (#548)
    "unquiet": "human",  # more wakes again, so the web session, like /release
    "who": "human_cli",
    "status": "human_cli",
    "help": "human_cli",
    "catchup": "human_cli",  # posts a human chat message, like `switchboard say`
    "review": "human_cli",  # the same: one chat message, and a board (§37.8)
    # a pin puts a message in front of every agent that joins, which raises what agents act on:
    # a person's web session (#292). Taking one away reduces it, as /kick and /hold do.
    "pin": "human",
    "unpin": "human_cli",
    "room": "human",  # hands-free calls: what every call here starts with (§52.3); /room alone is read-only
    "meeting": "human",  # joining a call lets anyone on it address an agent (§52)
}

REVIEW_USAGE = "/review <url> [note]"


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
    # who, on a hosted broker with people (§32): their name and id (None: the owner, and
    # every CLI caller, whose name is ``human_name``)
    name: str | None = None
    person_id: int | None = None
    # an admin (the owner, or a person whose row says so, §51): who may end a call that is not theirs (§54)
    admin: bool = False

    def who(self, human_name: str) -> str:
        return self.name or human_name


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
    pins_changed: bool = False  # a pin or an unpin: the page's header strip is sent again
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
    if name == "meeting":
        return _parse_meeting(args, raw)
    if name == "room":
        return _parse_room(args, raw)
    if name == "catchup":
        # parsed from the raw text: a quoted topic keeps its spaces
        return _parse_catchup(raw[1:].split(None, 1)[1] if args else "", raw)
    if name == "review":
        return _parse_review(raw)
    if name == "rename" and len(args) != 1:
        raise CommandError("bad_request", "usage: /rename <new-name>")
    if name in _NO_ARGS and args:
        raise CommandError("bad_request", f"/{name} takes no arguments")
    if name in _ONE_NAME:
        if len(args) != 1:
            raise CommandError("bad_request", f"usage: /{name} <name>")
        if not SCREEN_NAME_RE.match(args[0].lower().lstrip("@")):
            raise CommandError("bad_request", f"/{name}: not a valid screen name")
        args = (args[0].lower().lstrip("@"),)
    if name in _ONE_MESSAGE:
        if len(args) != 1 or not args[0].lstrip("#").isascii() or not args[0].lstrip("#").isdigit():
            raise CommandError("bad_request", f"usage: /{name} <message id>, the #number on a message")
        digits = args[0].lstrip("#").lstrip("0") or "0"
        if len(digits) > 19 or int(digits) < 1:  # a message id is a positive SQLite integer
            raise CommandError("bad_request", f"/{name}: not a message id")
        args = (digits,)
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


MEETING_USAGE = "usage: /meeting [<link> [mine]|leave|snapshot|voice on|voice off|listen <name> [off]]"


def _parse_meeting(args: tuple[str, ...], raw: str) -> Command:
    """``/meeting``, ``/meeting leave``, ``/meeting snapshot``, ``/meeting voice on|off``, or
    ``/meeting <link> [mine]``. The link is one argument, kept only until ``apply`` hands it to
    the bridge or the expiring private handoff (§54)."""
    if not args:
        return Command(name="meeting", args=(), raw=raw)
    word = args[0].lower()
    if len(args) == 1 and word in ("leave", "snapshot"):
        return Command(name="meeting", args=(word,), raw=raw)
    if word == "listen":
        if len(args) not in (2, 3) or (len(args) == 3 and args[2].lower() != "off"):
            raise CommandError("bad_request", "usage: /meeting listen <name> [off]")
        target = _screen_name("meeting listen", args[1])
        return Command(
            name="meeting", args=("listen", target) + (("off",) if len(args) == 3 else ()), raw=raw
        )
    if word == "voice":
        if len(args) == 2 and args[1].lower() in ("on", "off"):
            return Command(name="meeting", args=("voice", args[1].lower()), raw=raw)
        raise CommandError("bad_request", "usage: /meeting voice on|off")
    if len(args) == 2 and args[1].lower() == "mine":
        return Command(name="meeting", args=(args[0], "mine"), raw=raw)
    if len(args) != 1:
        raise CommandError("bad_request", MEETING_USAGE)
    return Command(name="meeting", args=(args[0],), raw=raw)


ROOM_USAGE = "usage: /room [voice|listen on|off]"


def _parse_room(args: tuple[str, ...], raw: str) -> Command:
    """``/room`` (show), ``/room voice on|off`` and ``/room listen on|off``: a room's hands-free
    call settings (§52.3). Anything else is refused, never guessed at."""
    if not args:
        return Command(name="room", args=(), raw=raw)
    if len(args) == 2 and args[0].lower() in ("voice", "listen") and args[1].lower() in ("on", "off"):
        return Command(name="room", args=(args[0].lower(), args[1].lower()), raw=raw)
    raise CommandError("bad_request", ROOM_USAGE)


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
            raise CommandError(
                "bad_request",
                f"/catchup: the topic is too long ({len(topic)} characters; at most {catchup.TOPIC_MAX})",
            )
        return Command(
            name="catchup", args=(agent, "topic", topic, catchup.clean_text(after[end + 1 :])), raw=raw
        )
    if after[0] in _SINGLE_QUOTES:
        inner = after[1:]
        end = next((i for i, ch in enumerate(inner) if ch in _SINGLE_QUOTES), len(inner))
        raise CommandError(
            "bad_request",
            f"/catchup: a topic goes in double quotes: /catchup {agent} on"
            f' "{catchup.clean_text(inner[:end])}"',
        )
    parts = after.split(None, 1)
    member = _screen_name("catchup", parts[0])
    if member == agent:
        raise CommandError("bad_request", "/catchup: an agent can't catch up on itself; name another member")
    note = catchup.clean_text(parts[1]) if len(parts) > 1 else ""
    return Command(name="catchup", args=(agent, "member", member, note), raw=raw)


def _parse_review(raw: str) -> Command:
    """``/review <url> [note...]`` (§37.8): ``args`` ``(url, note)``, the URL checked as the
    board's ``open`` checks it, the note cleaned like chat text."""
    words = raw[1:].split(None, 2)
    if len(words) < 2:
        raise CommandError("bad_request", f"usage: {REVIEW_USAGE}")
    try:
        url = reviews.url(words[1])
    except ReviewError as e:
        raise CommandError("bad_request", f"/review: {e}") from None
    note = catchup.clean_text(words[2]) if len(words) > 2 else ""
    return Command(name="review", args=(url, note), raw=raw)


def review_request(agents: list[str], url: str, note: str) -> str:
    """The message ``/review`` posts as the person: switchboard's words, the person's URL and
    their note, and no agent's text (#177). The mentions wake each agent once."""
    text = (
        " ".join("@" + a for a in agents)
        + f" Please review {url} together on this room's review board. Raise each finding with"
        ' review(action="raise"), with its file and lines, and write its detail in Markdown (a'
        " Mermaid diagram when a picture explains it better than words); concede or contest each"
        " other's, and when one of yours is contested, drop it or insist on it with your reason"
        ' (review(action="insist")): settle disagreements between you, and leave a person only'
        ' what you still disagree on. Ask with review(action="ask") what only a person can decide,'
        " with the option you recommend, and agree with the others' questions or pick another"
        ' option with review(action="concur"). review(action="show") shows the board. The room may'
        " have other boards open: give every call this pull request's link as its url."
    )
    return text + (f"\n\nNote: {note}" if note else "")


def _review(cmd: Command, room: Room, actor: Actor, svc: "RoomService") -> Result:
    """Open the room's board for the URL, as the person, and hand back the request to post
    (§37.8): a board for that URL that was closed comes back with its findings, and a room may
    have others open (§37.14). Every check comes before the board opens, so a refusal leaves
    nothing behind."""
    url, note = cmd.args
    agents = [m.name for m in svc.store.members(room.id)]
    if not agents:
        raise CommandError(
            "bad_request", f"/review: no agents in {room.name} to review it: ask one to join first"
        )
    text = review_request(agents, url, note)
    limit = svc.cfg.delivery.max_msg_chars
    if len(text) > limit:
        fits = max(limit - (len(text) - len(note)), 0)
        raise CommandError(
            "bad_request",
            f"/review: the note is too long ({len(note)} characters; at most {fits} fit in one message)",
        )
    if svc.boards is None:  # a RoomService on its own, as the human side's unit tests build it
        raise CommandError("bad_request", "/review: this broker has no review boards")
    try:
        how = svc.boards.person_open(room, actor.who(svc.cfg.human_name), url)
    except ReviewError as e:
        raise CommandError("bad_request", f"/review: {e}") from None
    who = " and ".join([", ".join(agents[:-1]), agents[-1]] if len(agents) > 1 else agents)
    # a board kept after it was closed comes back with its findings (§37.14); an open one stays as it is
    lines = [
        {
            "reopened": f"opened the review board for {url} again, with its findings and history,"
            f" and asked {who} to review it",
            "kept": f"the review board for {url} is open already: asked {who} to review it",
        }.get(how, f"opened the review board for {url} and asked {who} to review it")
    ]
    if room.paused:
        lines.append(f"{room.name} is paused: the request goes out after /resume")
    return Result(True, "\n".join(lines), post=text)


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
    if cmd.name == "room" and not cmd.args:
        return "human_cli"  # reading the settings changes nothing
    if cmd.name == "meeting" and cmd.args[:1] == ("listen",) and len(cmd.args) == 3:
        return "human_cli"
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
        text = (
            f"{room.name}: {hops_state(room)} (agents may message each other without limit;"
            " /hops <n> turns it back on)"
        )
    else:
        text = (
            f"{room.name}: {hops_state(room)} (the room pauses after {room.hop_limit} agent"
            f" messages in a row with none from {human})"
        )
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


def hop_limit_notice(old: int, new: int) -> str:
    """The room notice for a changed hop limit: what ``/hops n`` and the room's own settings
    dialog (#131) both write, so the wording for turning the loop guard on or off is the same
    whichever one changed it."""
    if new == 0:
        return f"turned the loop guard off (hop limit was {old})"
    if old == 0:
        return f"turned the loop guard on with a hop limit of {new}"
    return f"set the hop limit to {new} (was {old})"


def _hops_set(room: Room, n: int, svc: "RoomService") -> Result:
    """Set the room's hop limit. It never un-pauses: a room the loop guard paused needs /resume."""
    old = room.hop_limit
    if n == old:
        return Result(
            True, f"{room.name}: the hop limit is already {n}; {hops_state(room)}" + _hops_pause_hint(room)
        )
    room = svc.store.set_hop_limit(room.id, n)
    guard_paused = room.paused and room.paused_reason == "loop guard"
    notice = hop_limit_notice(old, n)
    if n == 0:
        head = f"loop guard off (hop limit 0, was {old}); agents may message each other without limit"
        tail = (
            "; the room is still paused by the loop guard: /resume to continue"
            if guard_paused
            else _hops_pause_hint(room)
        )
    else:
        head = f"loop guard on, hop limit {n}" if old == 0 else f"hop limit set to {n} (was {old})"
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
    ordinary human message."""
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
            raise CommandError(
                "bad_request",
                f"/{cmd.name}: {agent.name} is the only agent in {room.name}: there is nobody to catch up on",
            )
    since = catchup.window_start(mode, svc.clock.now(), agent.joined_at)
    handles = [
        catchup.handle(m, svc.store.get_participant(m.participant_id), svc.cfg, agent.host) for m in subjects
    ]
    topic = target if mode == "topic" else ""
    limit = svc.cfg.delivery.max_msg_chars
    base = catchup.request_text(agent.name, mode, handles, since=since, max_chars=limit, topic=topic, note="")
    # the room's pinned messages ride along (#292, §48) when the request still fits with them
    pins = [p.message for p in svc.store.pins(room.id)]
    with_pins = catchup.request_text(
        agent.name,
        mode,
        handles,
        since=since,
        max_chars=limit,
        topic=topic,
        note="",
        pins=pins,
        room=room.name,
    )
    left_out = bool(pins) and len(with_pins) > limit
    if len(base) > limit or left_out:
        pins = []
    text = catchup.request_text(
        agent.name,
        mode,
        handles,
        since=since,
        max_chars=limit,
        topic=topic,
        note=note,
        pins=pins,
        room=room.name,
    )
    if len(base) > limit:
        raise CommandError(
            "bad_request",
            f"/{cmd.name}: its request needs {len(base)} characters, but [delivery] max_msg_chars is {limit}",
        )
    if len(text) > limit:
        fits = limit - (len(text) - len(note))
        raise CommandError(
            "bad_request",
            f"/{cmd.name}: the note is too long ({len(note)} characters; at most {fits} fit in one message)",
        )
    lines: list[str] = []
    what = {
        "member": f"{subjects[0].name}'s work",
        "topic": f'"{topic}" across {len(handles)} session(s)',
        "room": "what the room did",
    }[mode]
    lines.append(f"asked {agent.name} to catch up on {what} since {catchup.when(since)}; it got:")
    lines += [f"  {h.line()}" + (f" ({h.why})" if h.why else "") for h in handles]
    if pins:
        lines.append(f"  and the room's {len(pins)} pinned message(s)")
    elif left_out:
        lines.append(
            f"  the room's pinned messages are left out: with them the request is over {limit} characters"
        )
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
    data: dict[str, Any] = {
        "agent": agent.membership_id,
        "mode": mode,
        "subjects": [m.membership_id for m in subjects],
        "with_id": sum(1 for h in handles if h.sid),
    }
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


def _pin(cmd: Command, room: Room, actor: Actor, svc: "RoomService") -> Result:
    """``/pin <id>`` and ``/unpin <id>`` (#292, §48). A pin is stored, shown in the room header and
    at every join and ``/catchup``; the room gets a notice saying who pinned what, so the log
    keeps the record. Already pinned, or not pinned, changes nothing and says so."""
    mid = int(cmd.args[0])
    who = actor.who(svc.cfg.human_name)
    if cmd.name == "pin":
        changed, msg = svc.pin_message(room, mid, who)
        if not changed:
            return Result(True, f"message {mid} is already pinned in {room.name}")
        return Result(
            True,
            f"pinned message {mid} ({msg.sender_name}'s) in {room.name}: agents that join are shown it",
            notice=f"pinned message {mid} ({msg.sender_name}'s)",
            pins_changed=True,
            event="pin",
            event_data={"message_id": mid},
        )
    changed, msg = svc.unpin_message(room, mid)
    if not changed:
        return Result(True, f"message {mid} isn't pinned in {room.name}")
    return Result(
        True,
        f"unpinned message {mid} in {room.name}",
        notice=f"unpinned message {mid} ({msg.sender_name}'s)",
        pins_changed=True,
        event="unpin",
        event_data={"message_id": mid},
    )


def _meeting(cmd: Command, room: Room, actor: Actor, svc: "RoomService") -> Result:
    """Start, leave, or describe the room's call. Nothing returned or recorded contains the link."""
    if not cmd.args:
        row = svc.store.open_meeting(room.id)
        if row is None:
            return Result(True, f"{room.name}: no call is being transcribed")
        label = PLATFORM_LABEL.get(row.platform, "call")
        if row.hosted:
            return Result(True, f"{room.name}: transcribing a {label} call, hosted on {row.host}")
        if row.state == "joining":
            return Result(True, f"{room.name}: joining a {label} call")
        return Result(True, f"{room.name}: transcribing a {label} call")
    if cmd.args == ("leave",):
        row = svc.store.open_meeting(room.id)
        if row is None:
            return Result(True, f"{room.name}: no call is being transcribed")
        label = PLATFORM_LABEL.get(row.platform, "call")
        if row.hosted:
            # a call hosted on a person's machine: only that person, or an admin, may end its key (§54)
            if svc.hosted_calls is None or not svc.hosted_calls.may_end(row, actor.person_id, actor.admin):
                raise CommandError(
                    "forbidden", f"Only {row.started_by}, who hosts it, or an admin can end this call."
                )
            svc.hosted_calls.stop(room.id, "leave")
        elif svc.meetings is not None:
            svc.meetings.stop(room.id, "leave")
        else:
            svc.store.end_meeting(row.id, None)
        return Result(
            True,
            f"{room.name}: leaving the {label} call",
            notice="stopped transcribing the call",
            room_changed=True,
            event="meeting_leave",
            event_data={"platform": row.platform},
        )
    if cmd.args == ("snapshot",):
        return _meeting_snapshot(room, actor, svc)
    if cmd.args[:1] == ("listen",):
        return _meeting_listen(cmd, room, svc)
    if cmd.args[:1] == ("voice",):
        return _meeting_voice(cmd.args[1] == "on", room, svc)
    try:
        link = parse_link(cmd.args[0])
    except LinkError:
        raise CommandError("bad_request", "That link isn't a Zoom, Meet or Teams https link.") from None
    if cmd.args[1:] == ("mine",):
        return _meeting_mine(room, actor, svc, link)
    if not svc.cfg.meeting.joinly_url:
        return Result(
            True,
            "No meeting server is set. Add [meeting] joinly_url in config.toml, or set"
            " SWITCHBOARD_JOINLY_URL; see DEPLOY.md.",
        )
    if svc.store.open_meeting(room.id) is not None:
        raise CommandError("conflict", f"{room.name} is already transcribing a call. /meeting leave ends it.")
    # the guard on the bot's memory (§52): one joinly holds about 2.7 GiB a live call, so past the limit
    # the call is refused here, before a row is written or the link goes anywhere. The count is across
    # every room, and the message gives only the numbers: the person may not see the rooms that hold them.
    limit = svc.cfg.meeting.max_calls
    if svc.store.count_open_meetings() >= limit:
        raise CommandError(
            "conflict",
            f"This broker follows at most {limit} {'call' if limit == 1 else 'calls'} at once, and that many"
            " are open. Leave one first (/meeting leave in its room).",
        )
    label = PLATFORM_LABEL[link.platform]
    try:
        row = svc.store.insert_meeting(
            room.id, started_by=actor.who(svc.cfg.human_name), platform=link.platform
        )
    except Conflict:
        raise CommandError(
            "conflict", f"{room.name} is already transcribing a call. /meeting leave ends it."
        ) from None
    if svc.hosted_calls is not None:
        svc.hosted_calls.forget_requests(room.id)
    if svc.meetings is None:
        svc.store.end_meeting(row.id, "the meeting bridge is not running")
        return Result(True, "No meeting server is running. See DEPLOY.md.")
    svc.start_hands_free(room.id, row.id, svc.meetings)  # the room's settings, if a person made any (§52.3)
    svc.meetings.start(room.id, link.url)
    note = svc.hands_free_note(room)
    return Result(
        True,
        f"{room.name}: joining a {label} call",
        notice=f"is joining a {label} call" + (f". {note}" if note else ""),
        room_changed=True,
        event="meeting_join",
        event_data={"platform": link.platform},
    )


def _meeting_mine(room: Room, actor: Actor, svc: "RoomService", link: "MeetingLink") -> Result:
    """Keep the secret out of every persisted command result, notice, event and model message (§54)."""
    if svc.hosted_calls is None:
        return Result(True, "Hosted calls are not available.")
    targets = svc.hosted_calls.request(room, actor.person_id, link)
    if not targets:
        return Result(
            True, f"{room.name}: no agent acting for you is in this room. Join one first, then ask again."
        )
    members = svc.store.room_memberships(room.id)
    names = " ".join(f"@{m.screen_name}" for m in members if m.id in targets)
    return Result(
        True,
        f"{room.name}: asked your own agent to host the call. The request expires in ten minutes.",
        notice="asked their own agent to host the call",
        post=f"{names} Run your host-meeting-bot skill for {room.name} with --from-room and empty stdin. "
        "This is your person's /meeting mine handoff. The script receives the link privately. "
        "Do not ask for, read, print or repeat the link. "
        "The first successful host takes it; do not retry a refusal.",
        post_skip=tuple(m.id for m in members if m.id not in targets),
        event="meeting_mine",
        event_data={"platform": link.platform},
    )


def _meeting_listen(cmd: Command, room: Room, svc: "RoomService") -> Result:
    """A person's opt-in to waking on unaddressed call text (§52.1)."""
    member = svc.store.find_member(room.id, cmd.args[1])
    if member is None:
        raise CommandError("not_found", f"no such member in {room.name}: {cmd.args[1]}")
    on = len(cmd.args) == 2
    row = svc.store.open_meeting(room.id)
    if row is None:
        raise CommandError("conflict", f"{room.name}: no call is being transcribed")
    if member.listen == on:
        return Result(True, f"{member.name} is {'already listening' if on else 'not listening'}")
    if not svc.store.set_listen(member.membership_id, on, row.id, svc.cfg.delivery.listen_max_wakes):
        raise CommandError("conflict", f"{member.name} has reached the listening wake cap for this call")
    warning = catchup.approvals_warning(member.name, member.approval_mode) if on else None
    notice = f"{'started' if on else 'stopped'} {member.name} listening to the call"
    return Result(
        True,
        notice,
        notice=notice,
        warnings=(warning,) if warning else (),
        members_changed=True,
        event="meeting_listen",
        event_data={"membership_id": member.membership_id, "on": on},
    )


def _room_calls(cmd: Command, room: Room, svc: "RoomService") -> Result:
    """A room's hands-free call settings (§52.3). Only a person gets here: ``apply`` checked the role,
    and an agent's ``say("/room ...")`` is stored as text and never parsed. Turning one on also applies
    to a call that is open now, so a person on a phone does not have to wait for the next call; turning
    one off leaves the open call as it is (``/meeting voice off`` and the call bar change that)."""
    if not cmd.args:
        voice, listen = ("on" if x else "off" for x in (room.call_voice, room.call_listen))
        return Result(
            True, f"{room.name}: hands-free voice is {voice}; hands-free listening is {listen} (calls here)"
        )
    setting, word = cmd.args
    on = word == "on"
    if not svc.store.set_room_call(room.id, setting, on):
        return Result(True, f"{room.name}: {_ROOM_CALL_LABEL[setting]} is already {word}")
    row = svc.store.open_meeting(room.id)
    if on and row is not None:
        if setting == "voice":
            voices = svc.hosted_calls if row.hosted else svc.meetings
            if voices is not None:
                voices.set_voice(room.id, True)
        else:
            svc.listen_everyone(room.id, row.id)
    notice = f"turned {word} {_ROOM_CALL_LABEL[setting]}: {_ROOM_CALL_MEANING[setting][on]}"
    return Result(
        True,
        f"{room.name}: {_ROOM_CALL_LABEL[setting]} is {word}",
        notice=notice,
        room_changed=True,
        members_changed=setting == "listen",
        event="room_calls",
        event_data={"setting": setting, "on": on},
    )


_ROOM_CALL_LABEL = {"voice": "hands-free voice", "listen": "hands-free listening"}
_ROOM_CALL_MEANING = {
    "voice": {
        True: "every call here starts with Voice on, so an agent's @call message is read aloud",
        False: "calls here start with Voice off again",
    },
    "listen": {
        True: "every agent here listens on calls, without being named",
        False: "agents here listen on a call only when a person turns it on for them",
    },
}


def _meeting_voice(on: bool, room: Room, svc: "RoomService") -> Result:
    """Let agents speak in the room's call, or stop them (§52). In memory for this call only."""
    row = svc.store.open_meeting(room.id)
    if row is None:
        return Result(True, f"{room.name}: no call is being transcribed")
    voices = svc.hosted_calls if row.hosted else svc.meetings  # whoever follows this call holds its voice
    if voices is None:
        return Result(True, "No meeting server is running. See DEPLOY.md.")
    word = "on" if on else "off"
    if not voices.set_voice(room.id, on):
        return Result(True, f"{room.name}: voice is already {word}")
    if on:
        notice = (
            "turned voice on: an agent's message that starts with @call is read aloud to everyone on the call"
        )
    else:
        notice = "turned voice off: agents no longer speak in the call"
    return Result(
        True,
        f"{room.name}: voice is {word}",
        notice=notice,
        room_changed=True,
        event="meeting_voice",
        event_data={"on": on},
    )


def _meeting_snapshot(room: Room, actor: Actor, svc: "RoomService") -> Result:
    """Post a picture of the call's shared screen as this person's message (§52). It arrives a
    moment later; a refusal (the bot is not in yet, or one was taken lately) is the reply."""
    row = svc.store.open_meeting(room.id)
    if row is None:
        return Result(True, f"{room.name}: no call is being transcribed")
    if row.hosted:
        raise CommandError(
            "conflict", "A snapshot isn't available yet for a call hosted on a person's machine."
        )
    if svc.meetings is None:
        return Result(True, "No meeting server is running. See DEPLOY.md.")
    asker = Asker(actor.who(svc.cfg.human_name), actor.person_id, actor.via)
    why = svc.meetings.request_snapshot(room.id, asker)
    if why is not None:
        raise CommandError("conflict", why)
    return Result(
        True,
        f"{room.name}: asking the bot for a snapshot of the call",
        event="meeting_snapshot",
    )


def apply(cmd: Command, room: Room, actor: Actor, svc: "RoomService") -> Result:
    """Run a parsed command. Role checks happen here too (defence in depth)."""
    store = svc.store
    # Refill first: whether ``/budget n`` raises or lowers is judged against the
    # budget that is really left now, not a window that has already rolled over.
    room = store.refill_budget(room.id)
    check_role(cmd, room, actor)
    name = cmd.name
    if name == "meeting":
        return _meeting(cmd, room, actor, svc)
    if name == "room":
        return _room_calls(cmd, room, svc)
    if name == "close":
        return Result(True, svc.close_room(room, actor), done=True)
    if name == "rename":
        renamed = svc.rename_room(room, cmd.args[0], actor.who(svc.cfg.human_name))
        return Result(True, f"{room.name} renamed to {renamed.name}", done=True)
    if name == "help":
        return Result(True, HELP_TEXT)
    if name == "who":
        return Result(True, svc.who_text(room))
    if name == "status":
        return Result(True, svc.status_text(room))
    if name == "pause":
        if room.paused:
            return Result(True, f"{room.name} is already paused ({room.paused_reason or 'by you'})")
        store.set_paused(room.id, True, "paused by " + actor.who(svc.cfg.human_name))
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
            f"{room.name} resumed; loop guard reset"
            if was
            else f"{room.name} was not paused; loop guard reset",
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
    if name == "catchup":
        return _catchup(cmd, room, svc)
    if name == "review":
        return _review(cmd, room, actor, svc)
    if name in _ONE_MESSAGE:
        return _pin(cmd, room, actor, svc)
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
    if name == "wake":
        # §8.2, #137: what a person's own message would do for the chatter waiting on the hold
        if member.held:
            return Result(True, f"{member.name} is held: /release {member.name} first")
        if room.paused:
            return Result(True, f"{room.name} is paused: /resume first")
        waiting = [
            i for i in store.pending_items(member.membership_id) if i.prio == 0 and i.notified_at is None
        ]
        if not waiting:
            return Result(True, f"nothing is waiting for {member.name}")
        n = len(waiting)
        return Result(
            True,
            f"waking {member.name} for {n} waiting message{'s' if n != 1 else ''}",
            members_changed=True,
            event="wake",
            event_data={"membership_id": member.membership_id},
        )
    if name == "quiet":
        if member.quiet:
            return Result(True, f"{member.name} is already quiet")
        store.set_quiet(member.membership_id, True)
        return Result(
            True,
            f"quieted {member.name}: it wakes only for messages addressed to it",
            notice=f"quieted {member.name}: it wakes only for messages addressed to it",
            members_changed=True,
            event="quiet",
            event_data={"membership_id": member.membership_id},
        )
    if name == "unquiet":
        if not member.quiet:
            return Result(True, f"{member.name} is not quiet")
        store.set_quiet(member.membership_id, False)
        return Result(
            True,
            f"unquieted {member.name}: it wakes for every message again",
            notice=f"unquieted {member.name}",
            members_changed=True,
            event="unquiet",
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
