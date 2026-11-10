"""Calls hosted on a person's machine (DESIGN.md §54, #623).

The meeting bot can run on a person's own machine, for one call in one room, and push what it hears to
the broker instead of the broker pulling it from a bot beside it (§52). The broker's side is here:

- **Opening** (``register``, then ``open``): the host script on the person's machine, with the credential
  the plugin's other scripts hold (an agent token, or a listener key for its one session, §49.11), registers
  the sha256 of a key it made for this call, at ``POST /mcp/meeting/host``. It is no agent tool: the model
  never handles the key or even its hash. The call is the person's: its owner and its machine's label come
  from the token, not from anything the script says, and the room must have a member on that machine.
- **The key** is a bearer for three routes (``broker/call_http.py``) and nothing else: pushing segments
  (``push``), waiting for what is to be spoken (``speech``), and reporting departure (``left``).
  ``check`` is what each asks first. It ends
  with the call, and it is refused at once when the call is over or its machine's sign-in is revoked.
- **What a push becomes** is what the broker's own bot's lines become: the same ``Assembler`` joins
  segments into lines and the same ``ingest`` checks and posts them, so every source gets the same fixes.
  A line is stored with its call and the machine that heard it. Posts are paced at one a second.
- **Ending** (``stop``, ``end``): by a person, a closed room, a revoked sign-in, a host that never
  started sending or went quiet (``tick``, which reads the broker's clock, so a test steps it), or the
  broker stopping. What is left of the transcript is posted first, the key is refused from then on, and
  a waiting speech poll is answered with ``ended``.

All of it runs on the broker's loop and never waits, except ``speech``, which is a long poll. Memory is
the expiring private handoffs, the line being assembled, the lines waiting to be posted and spoken,
and the voice switch: a call does not outlive the process (the bridge's ``recover`` ends every open call
at the next start).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from switchboard.broker.service import ServiceError
from switchboard.envelope import clean
from switchboard.meeting import ingest, keys
from switchboard.meeting.ingest import LINE_GAP_S
from switchboard.meeting.lines import CAP, Assembler, Line, Segment
from switchboard.meeting.links import PLATFORM_LABEL, MeetingLink
from switchboard.meeting.names import hotwords
from switchboard.meeting.speech import call_text, speakable
from switchboard.meeting.turns import BUSY_NOTICE, MAX_ID, STALE_NOTICE, Turns
from switchboard.models import InvalidName, Meeting, Room, normalize_room, split_session_key
from switchboard.store import Conflict

if TYPE_CHECKING:  # pragma: no cover
    from switchboard.broker.app import BrokerState
    from switchboard.models import AgentTokenRow

log = logging.getLogger("switchboard.meeting")

# What a source may send (§54). Each is a claim from outside, cut or refused; none changes with a setting.
BODY_MAX = 256 * 1024  # bytes of one push or poll
BATCH_MAX = 100  # segments in one push
TEXT_MAX = 2 * CAP  # characters of one segment's text, before the assembler cuts a line at CAP
RATE_PER_S = 10.0  # segments (or requests) a second, sustained
BURST = float(BATCH_MAX)  # and at once: a whole batch is never refused for its size alone
SPEECH_WAIT_S = 25.0  # how long a speech poll waits when it does not say
SPEECH_WAIT_MAX_S = 30.0  # and the longest it may ask: far inside the Ingress's 16 minutes (DEPLOY.md)
SEEN_MAX = 4096  # segments remembered, to take none twice
HINTS_MAX = 100  # the room's names sent as the transcriber's hotwords, each cut to 40 characters
# A hosted call that has not gone live ends after ten minutes: joinly's own join is given 45 s
# (``bridge.JOIN_TIMEOUT_S``), and a first start also pulls the images and a 1.6 GB model (§52).
JOIN_WINDOW_S = 600.0
# A live one that has heard nothing from its key (no push, no poll) ends after five minutes: more than
# twice the 130 s a wait nobody answers is given (``rules.WAIT_DROP_GRACE_S``), so a poll that ran long or
# a laptop that slept a moment does not end it, and a host that is gone does.
QUIET_S = 300.0
# Registering a call is rare: a few tries a minute for a token is plenty, and a guesser or a loop is stopped.
HOST_PER_WINDOW = 5
HOST_WINDOW_S = 60.0
REQUEST_TTL_S = 600.0  # a room-to-host handoff is memory only and can be taken once
REQUEST_LINK_MAX = 2000  # bytes, within the host script and relay's secret-file limits
TICK_S = 0.5  # how often ``run`` ticks: half the gap between two lines, so a gap is never two

# why a call ended -> (the phrase kept in its row, or None; the notice the room reads, or None)
ENDS: dict[str, tuple[str | None, str | None]] = {
    "leave": (None, None),  # a person ended it: the command says so itself
    "the room closed": (None, "The call ended because the room closed."),
    "the broker stopped": (None, "The call ended because the broker stopped."),
    "revoked": (
        "its machine's sign-in was revoked",
        "The call ended: its machine's sign-in was revoked.",
    ),
    "never": (
        "its host never started sending",
        "The call ended: its host never started sending.",
    ),
    "bot_left": ("its bot left", "The call ended: its bot left."),
    "bot_stopped": ("its bot stopped", "The call ended: its bot stopped."),
    "join_failed": ("its bot could not join", "The call ended: its bot could not join."),
    "joinly_lost": ("its bot lost the call", "The call ended: its bot lost the call."),
    "quiet": ("its host went quiet", "The call ended: its host went quiet."),
}


@dataclass(frozen=True)
class _Request:
    """A secret held only in this process, never represented in logs or public state."""

    platform: str
    url: str = field(repr=False)
    expires: float


class Refused(Exception):
    """A request the key's routes refuse: the status, the word the answer carries and a sentence."""

    def __init__(self, status: int, code: str, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


@dataclass(frozen=True)
class Checked:
    """What ``HostedCalls.check`` says of a key: the call when it may act, else ``why`` is the word the
    route answers with (401 ``unknown_key`` or ``revoked``, 410 ``ended``)."""

    call: Meeting | None
    why: str = ""


@dataclass
class _Call:
    """What a hosted call holds in memory: the line being assembled, the lines waiting to be posted
    and to be spoken, the voice switch, the polls waiting, and when its key was last heard from."""

    meeting_id: int
    room_id: int
    now: float
    live: bool = False
    asm: Assembler = field(default_factory=Assembler)
    queue: deque[Line] = field(default_factory=deque)
    warned: bool = False  # told the room this burst of lines was too many
    last_post: float | None = None
    voice: bool = False
    turns: Turns = field(default_factory=Turns)
    waiters: set[asyncio.Event] = field(default_factory=set)
    polling: int = 0
    seq: int = 0  # the newest speech poll: an older one is answered empty at once
    ended: bool = False
    last_seen: float = 0.0
    tokens: float = BURST
    refilled: float = 0.0
    seen: set[tuple[float, float, str, str | None]] = field(default_factory=set)
    seen_order: deque[tuple[float, float, str, str | None]] = field(default_factory=deque)

    def __post_init__(self) -> None:
        self.last_seen = self.refilled = self.now

    def first_time(self, seg: Segment) -> bool:
        """False for a segment this call was given before, as the bridge skips one it has read already
        (§52): a relay that retries a push whose answer was lost must not post its lines twice. The
        memory is the last ``SEEN_MAX``."""
        key = (seg.start, seg.end, seg.text, seg.speaker)
        if key in self.seen:
            return False
        self.seen.add(key)
        self.seen_order.append(key)
        if len(self.seen_order) > SEEN_MAX:
            self.seen.discard(self.seen_order.popleft())
        return True


def parse_segments(body: Any) -> list[Segment]:
    """The segments of a push body, checked: a list of at most ``BATCH_MAX`` objects with text, and a
    finite start and end in seconds, and a speaker or none. Anything else refuses the whole push (nothing
    is half taken). Text and speaker are cut here, and cleaned: they are claims from outside."""
    raw = body.get("segments") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        raise Refused(400, "bad_request", "segments must be a list")
    if len(raw) > BATCH_MAX:
        raise Refused(413, "too_large", f"at most {BATCH_MAX} segments in one push")
    out: list[Segment] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise Refused(400, "bad_request", "each segment needs text")
        times = []
        for k in ("start", "end"):
            v = item.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise Refused(400, "bad_request", "each segment needs a start and an end in seconds")
            times.append(float(v))
        speaker = item.get("speaker")
        name = clean(speaker).strip()[: ingest.SENDER_MAX] if isinstance(speaker, str) else ""
        out.append(Segment(text=item["text"][:TEXT_MAX], start=times[0], end=times[1], speaker=name or None))
    return out


def wait_in(body: Any) -> float:
    """How long a speech poll may wait, in seconds: what it asks (a number, 0 to ``SPEECH_WAIT_MAX_S``),
    or ``SPEECH_WAIT_S`` when it asks nothing. A value that is not a number is refused."""
    asked = body.get("wait") if isinstance(body, dict) else None
    if asked is None:
        return SPEECH_WAIT_S
    if isinstance(asked, bool) or not isinstance(asked, (int, float)) or not math.isfinite(asked):
        raise Refused(400, "bad_request", "wait is a number of seconds")
    return min(max(float(asked), 0.0), SPEECH_WAIT_MAX_S)


def done_in(body: Any) -> int | None:
    """Only a bounded integer can complete a spoken line; malformed claims fail closed (§52.2)."""
    value = body.get("done") if isinstance(body, dict) else None
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= MAX_ID:
        raise Refused(400, "bad_request", "done must be a positive speech id")
    return value


def turns_in(body: Any) -> bool:
    """Whether this relay reports when each line ends (every relay since #697). One that doesn't has its
    slot freed at hand-off. Anything but a boolean is refused, never read as either (§52.2)."""
    if not isinstance(body, dict) or "turns" not in body:
        return False
    value = body["turns"]
    if type(value) is not bool:
        raise Refused(400, "bad_request", "turns must be true or false")
    return value


class HostedCalls:
    """The calls hosted on people's machines that are joining or live, on the broker's loop."""

    def __init__(self, state: "BrokerState") -> None:
        self.state = state
        self._calls: dict[int, _Call] = {}  # meeting id -> what it holds
        self._rooms: dict[int, int] = {}  # room id -> its hosted call's meeting id
        self._requests: dict[tuple[int, int | None], _Request] = {}
        self._asked: dict[int, deque[float]] = {}  # token id -> when it last tried to register a call

    # ------------------------------------------------------------------ a person's room-to-host handoff
    def request(self, room: Room, person_id: int | None, link: MeetingLink) -> list[int]:
        """Hold a link for ten minutes only if this person has credential-backed agents in this room.
        Return their membership ids: normal human-message delivery wakes them, respecting every guard."""
        self._expire_requests()
        try:
            size = len(link.url.encode("utf-8"))
        except UnicodeError:
            raise ServiceError("bad_request", "That meeting link is not valid UTF-8.") from None
        if size > REQUEST_LINK_MAX:
            raise ServiceError("bad_request", "That meeting link is too long.")
        store = self.state.store
        targets = []
        for membership in store.room_memberships(room.id):
            p = store.get_participant(membership.participant_id)
            token = store.agent_token_for_device(p.host) if p is not None and p.active and p.host else None
            if token is not None and token.person_id == person_id:
                targets.append(membership.id)
        if not targets:
            self._requests.pop((room.id, person_id), None)
            return []
        if store.open_meeting(room.id) is not None:
            raise ServiceError("conflict", "This room already has a call. End it first.")
        if store.open_hosted_meeting_of(person_id) is not None:
            raise ServiceError("conflict", "You already host a call. End it first.")
        if store.count_open_meetings() >= self.state.cfg.meeting.max_calls:
            raise ServiceError("conflict", "This broker is already following its maximum number of calls.")
        self._requests[room.id, person_id] = _Request(
            link.platform, link.url, self.state.clock.now() + REQUEST_TTL_S
        )
        return targets

    def _expire_requests(self) -> None:
        now = self.state.clock.now()
        for key, request in list(self._requests.items()):
            room = self.state.store.room_by_id(key[0])
            if now >= request.expires or room is None or room.closed:
                del self._requests[key]

    def forget_requests(self, room_id: int) -> None:
        for key in [key for key in self._requests if key[0] == room_id]:
            del self._requests[key]

    # ------------------------------------------------------------------ opening (the host script)
    def register(
        self,
        token: "AgentTokenRow",
        params: dict[str, Any],
        *,
        session: str | None,
        harness: str | None = None,
    ) -> dict[str, Any]:
        """What ``POST /mcp/meeting/host`` does once the caller is known (§54): count the attempt against the
        token's allowance, find the room (it must have a member on this token's machine, in ``session`` when
        one is named), then ``open`` the call. ``session`` is the listener key's own session or what a token's
        script names, never what a key's body says. Never returns, logs or stores a key."""
        self._admit(token.id)
        given = params.get("room")
        if not isinstance(given, str) or not given.strip():
            raise ServiceError("bad_request", "room is required, e.g. #build")
        try:
            name = normalize_room(given)
        except InvalidName as e:
            raise ServiceError("bad_request", str(e)) from None
        room = self.room_for(token, name, session, harness)
        from_room = params.get("from_room", False)
        if not isinstance(from_room, bool):
            raise ServiceError("bad_request", "from_room must be a boolean")
        if not from_room:
            return self.open(token, room, params)
        given = params.get("platform")
        if given not in (None, "") and keys.platform_in(given) is None:
            raise ServiceError("bad_request", "platform is zoom, meet, teams or unknown")
        self._expire_requests()
        pending = self._requests.get((room.id, token.person_id))
        if pending is None:
            raise ServiceError(
                "conflict", "No pending meeting request for you in this room. Ask again in the room."
            )
        # No await between validation, opening and consuming: only the first successful registration wins.
        # Base64 keeps the one secret field independent of shell/JSON quoting; scripts decode it privately.
        result = self.open(token, room, {**params, "platform": pending.platform})
        result["link_b64"] = base64.b64encode(pending.url.encode("utf-8")).decode("ascii")
        return result

    def _admit(self, token_id: int) -> None:
        """Count one attempt, whatever its outcome, or refuse with 429 once ``HOST_PER_WINDOW`` were made in
        the last ``HOST_WINDOW_S`` by the broker's clock. The memory is a few numbers a token."""
        now = self.state.clock.now()
        for tid in [t for t, q in self._asked.items() if not q or now - q[-1] >= HOST_WINDOW_S]:
            del self._asked[tid]  # a token that has been quiet for the window starts again
        q = self._asked.setdefault(token_id, deque())
        while q and now - q[0] >= HOST_WINDOW_S:
            q.popleft()
        if len(q) >= HOST_PER_WINDOW:
            raise Refused(
                429,
                "rate_limited",
                f"too many tries: at most {HOST_PER_WINDOW} a minute",
                retry_after=HOST_WINDOW_S - (now - q[0]),
            )
        q.append(now)

    def room_for(
        self, token: "AgentTokenRow", name: str, session: str | None = None, harness: str | None = None
    ) -> Room:
        """The room ``name`` (or the one renamed from it) when the machine behind ``token`` has a member in
        it: a member of ``session`` when one is named (a listener key's own session, or the one a token's
        script says), else any member on that machine. A member is on a machine when its session is on the
        token's device, which is unique among live tokens, so a member of another token's machine never
        counts. A room that does not exist, one the machine has no member in and one the machine's agent
        left are the same refusal, so a script learns nothing of rooms it is not in."""
        store = self.state.store
        room = store.get_room(name) or store.room_by_alias(name)
        if room is not None:
            for m in store.room_memberships(room.id):
                p = store.get_participant(m.participant_id)
                if p is None or not p.active or p.host != token.device:
                    continue
                if session is not None:
                    parts = split_session_key(p.session_key)
                    if parts is None or parts[2] != session or (harness is not None and parts[0] != harness):
                        continue
                return room
        raise ServiceError("not_member", f"no agent on this machine is in {name}")

    def open(self, token: "AgentTokenRow", room: Room, params: dict[str, Any]) -> dict[str, Any]:
        """Open a hosted call in ``room`` for the machine behind ``token`` (the caller checked that the
        machine has a member in ``room``). ``params`` hold the sha256 of the call's key, the platform and the
        model. Never returns, logs or stores a key. Refused when the room has a call, the person already
        hosts one, the broker follows as many calls as it may, or the key was registered before."""
        store, svc, cfg = self.state.store, self.state.service, self.state.cfg
        h = keys.hash_in(params.get("key_hash"))
        if h is None:
            raise ServiceError(
                "bad_request",
                "key_hash is the 64 hex characters of the key's sha256: send the hash, never the key",
            )
        given = params.get("platform")
        platform = "unknown" if given in (None, "") else keys.platform_in(given)
        if platform is None:
            raise ServiceError("bad_request", "platform is zoom, meet, teams or unknown")
        model = keys.model_in(params.get("model", ""))
        if store.open_meeting(room.id) is not None:
            raise ServiceError(
                "conflict", f"{room.name} already has a call. A call has one source: end it first."
            )
        if store.open_hosted_meeting_of(token.person_id) is not None:
            raise ServiceError("conflict", "you already host a call: end it before hosting another")
        limit = cfg.meeting.max_calls
        if store.count_open_meetings() >= limit:
            raise ServiceError(
                "conflict",
                f"This broker follows at most {limit} {'call' if limit == 1 else 'calls'} at once,"
                " and that many are open.",
            )
        if store.meeting_by_key(h) is not None:
            raise ServiceError("conflict", "that key was used for a call before: make a new one")
        who = svc.person_name(svc.person_key(token.person_id)) or "someone"
        try:
            row = store.insert_hosted_meeting(
                room.id,
                started_by=who,
                platform=platform,
                key_hash=h,
                token_id=token.id,
                owner_person_id=token.person_id,
                host=token.device,
                model=model,
            )
        except Conflict:
            raise ServiceError(
                "conflict", f"{room.name} already has a call, or that key was used before"
            ) from None
        self._state(row)
        svc.start_hands_free(room.id, row.id, self)  # the room's settings, if a person made any (§52.3)
        self.forget_requests(room.id)  # any successful start cancels stale handoffs for this room
        fp = keys.fingerprint(h)
        store.add_event(
            "meeting_host",
            room_id=room.id,
            data={"meeting": row.id, "platform": platform, "host": token.device, "by": who, "key": fp},
        )
        svc.publish_settings(room.id)
        note = svc.hands_free_note(room)
        svc.notice_in(
            room.id,
            f"{who} is hosting {_a_call(platform)} from {token.device}" + (f". {note}" if note else ""),
        )
        return {
            "room": room.name,
            "state": row.state,
            "platform": platform,
            "host": token.device,
            "fingerprint": fp,
        }

    # ------------------------------------------------------------------ the key
    def check(self, key: str) -> Checked:
        """Whether ``key`` opens a call now. A key that was never registered and one that is malformed look
        the same. The call of a key whose machine's token was revoked is ended here, at once."""
        store = self.state.store
        if keys.KEY_RE.fullmatch(key) is None:
            return Checked(None, "unknown_key")
        row = store.meeting_by_key(keys.key_hash(key))
        if row is None or not row.hosted:
            return Checked(None, "unknown_key")
        if row.state == "ended":
            return Checked(None, "ended")
        if not self._token_live(row):
            self.end(row, "revoked")
            return Checked(None, "revoked")
        return Checked(row)

    def _token_live(self, row: Meeting) -> bool:
        tok = self.state.store.agent_token_by_id(row.token_id) if row.token_id is not None else None
        return tok is not None and tok.revoked_at is None

    # ------------------------------------------------------------------ pushing
    def push(self, row: Meeting, segments: list[Segment]) -> dict[str, Any]:
        """Take a checked push. The first one, even an empty one, marks the call live. Segments are joined
        into lines, and at most one line is posted now (the rest wait their turn, ``tick`` posts them)."""
        row = self._fresh(row)
        now = self.state.clock.now()
        st = self._state(row)
        st.last_seen = now
        self._take(st, max(1, len(segments)), now)
        if not st.live:
            self._go_live(row, st)
        taken = 0
        for seg in segments:
            if not st.first_time(seg):
                continue  # a retry of a push the broker already took
            taken += 1
            st.turns.hear(now)
            self._wake(st)
            for line in st.asm.add(seg, now):
                self._enqueue(row, st, line)
        for line in st.asm.poll(now):
            self._enqueue(row, st, line)
        self._drain(row, st, now)
        return {"accepted": taken, "duplicate": len(segments) - taken, "ended": False}

    def _fresh(self, row: Meeting) -> Meeting:
        """The call as it is now. A request is checked at the door and then waits for its body, and the call
        may end meanwhile: it must not be taken in (or bring the call's memory back) once it is over."""
        now = self.state.store.meeting_by_id(row.id)
        if now is None or now.state == "ended":
            raise Refused(410, "ended", "the call has ended")
        return now

    def _go_live(self, row: Meeting, st: _Call) -> None:
        st.live = True
        self.state.store.set_meeting_live(row.id)
        self.state.service.publish_settings(row.room_id)
        self.state.service.notice_in(row.room_id, f"Transcribing {_a_call(row.platform)} from {row.host}.")

    def _take(self, st: _Call, cost: float, now: float) -> None:
        """Spend ``cost`` of the call's allowance (``RATE_PER_S`` refilled up to ``BURST``), or refuse."""
        st.tokens = min(BURST, st.tokens + max(0.0, now - st.refilled) * RATE_PER_S)
        st.refilled = now
        if st.tokens < cost:
            wait = (cost - st.tokens) / RATE_PER_S
            raise Refused(
                429, "rate_limited", f"too fast: at most {RATE_PER_S:g} segments a second", retry_after=wait
            )
        st.tokens -= cost

    def _enqueue(self, row: Meeting, st: _Call, line: Line) -> None:
        def overflow() -> None:
            self.state.service.notice_in(row.room_id, ingest.OVERFLOW_NOTICE)

        st.warned = ingest.enqueue(st.queue, line, st.warned, overflow)

    def _drain(self, row: Meeting, st: _Call, now: float, *, everything: bool = False) -> None:
        """Post the next waiting line when its turn has come (one a second), or all of them, at the end."""
        while st.queue and (everything or st.last_post is None or now - st.last_post >= LINE_GAP_S):
            self._post(row, st.queue.popleft())
            st.last_post = now
            if not everything:
                return

    def _post(self, row: Meeting, line: Line) -> None:
        try:
            incoming = ingest.receive(self.state, row, line)
            if incoming is not None:
                incoming.post(self.state)
        except Exception:
            log.warning("hosted call %s: a line could not be posted", row.id)  # never its words

    # ------------------------------------------------------------------ speaking
    def voice(self, room_id: int) -> bool:
        """Whether this room's hosted call has voice on. Off until a person turns it on."""
        st = self._calls.get(self._rooms.get(room_id, -1))
        return st is not None and st.voice

    def set_voice(self, room_id: int, on: bool) -> bool:
        """Turn voice on or off for the room's hosted call. True when that changed it. Turning it off drops
        the lines still waiting. A waiting poll is answered now, so the relay learns it."""
        st = self._calls.get(self._rooms.get(room_id, -1))
        if st is None or on == st.voice:
            return False
        st.voice = on
        if not on:
            st.turns.clear_pending()
        self._wake(st)
        return True

    def speak(self, room_id: int, text: str, *, membership_id: int | None = None) -> None:
        """Offer the call's one speech slot; its relay reports completion before it is free again."""
        st = self._calls.get(self._rooms.get(room_id, -1))
        if st is None or not st.voice or not st.live:
            return
        body = call_text(text)
        line = speakable(body) if body is not None else ""
        if not line:
            return
        self._expire_speech(st)
        if not st.turns.offer(line, self.state.clock.now(), membership_id):
            self.state.service.call_voice_notice(room_id, BUSY_NOTICE, membership_id)
            return
        self._wake(st)

    def _expire_speech(self, st: _Call) -> None:
        expired = st.turns.expire(self.state.clock.now())
        if expired is not None:
            self.state.service.call_voice_notice(st.room_id, STALE_NOTICE, expired.membership_id)

    def _speech_ready(self, st: _Call) -> bool:
        self._expire_speech(st)
        at = st.turns.ready_at()
        return st.voice and at is not None and self.state.clock.now() >= at

    async def speech(
        self, row: Meeting, wait_s: float, done: int | None = None, *, turns: bool = False
    ) -> dict[str, Any]:
        """The long poll: what the relay is to speak (only while voice is on), the room's names as the
        transcriber's hotwords, the voice switch and whether the call is over. Answered at once when there
        is a line to say or the call ended, and otherwise after ``wait_s`` seconds, or when the voice
        switch changes or a newer poll comes. Only the newest poll is given lines."""
        row = self._fresh(row)
        now = self.state.clock.now()
        st = self._state(row)
        st.last_seen = now
        self._take(st, 1, now)
        if done is not None:
            st.turns.finish(done)
        st.seq += 1
        mine = st.seq
        self._wake(st)  # an older poll that is still waiting is answered, empty
        st.polling += 1
        try:
            started = st.voice
            loop = asyncio.get_running_loop()
            deadline = loop.time() + wait_s
            while not (st.ended or self._speech_ready(st) or st.voice != started or st.seq != mine):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                ev = asyncio.Event()
                st.waiters.add(ev)
                try:
                    await asyncio.wait_for(ev.wait(), remaining)
                except TimeoutError:
                    break
                finally:
                    st.waiters.discard(ev)
        finally:
            st.polling -= 1
            st.last_seen = self.state.clock.now()
        line = st.turns.take(self.state.clock.now()) if st.seq == mine and st.voice else None
        if line is not None and not turns:
            st.turns.finish(line.id)  # an older relay never says a line ended: the 30 s interval paces it
        words = [w[:40] for w in hotwords(self.state.store.active_names(row.room_id))[:HINTS_MAX]]
        return {
            "lines": [line.text] if line is not None else [],
            "speech_id": line.id if line is not None else None,
            "hints": words,
            "voice": st.voice,
            "ended": st.ended,
            "tts_voice": self.state.cfg.meeting.tts_voice,
        }

    @staticmethod
    def _wake(st: _Call) -> None:
        for ev in st.waiters:
            ev.set()

    # ------------------------------------------------------------------ ending
    def left(self, row: Meeting, reason: Any) -> None:
        """The key's relay left. Only fixed reasons reach storage or notices; unknown input is generic.
        Recheck after reading the body: an ended key must never affect a newer call in the room."""
        row = self._fresh(row)
        reasons = {"stopped": "bot_stopped", "join_failed": "join_failed", "joinly_lost": "joinly_lost"}
        self.end(row, reasons.get(reason, "bot_left") if isinstance(reason, str) else "bot_left")

    def may_end(self, row: Meeting, person_id: int | None, admin: bool) -> bool:
        """Who may end a hosted call: the person it belongs to, or an admin (the owner is one)."""
        return admin or person_id == row.owner_person_id

    def stop(self, room_id: int, reason: str) -> bool:
        """End the room's hosted call, if it has one: True when it did, so a caller that would also stop the
        broker's own bot knows there was nothing for it to do."""
        self.forget_requests(room_id)
        row = self.state.store.open_meeting(room_id)
        if row is None or not row.hosted:
            return False
        self.end(row, reason)
        return True

    def end(self, row: Meeting, reason: str) -> None:
        """End ``row``: post what is left of the transcript, mark it ended (its key is refused from now
        on), answer a waiting poll with ``ended``, and tell the room. ``reason`` is a key of ``ENDS``."""
        error, notice = ENDS[reason]
        store, svc = self.state.store, self.state.service
        st = self._calls.pop(row.id, None)
        self._rooms.pop(row.room_id, None)
        if st is not None:
            for line in st.asm.flush():
                st.queue.append(line)
            self._drain(row, st, self.state.clock.now(), everything=True)
        try:
            store.end_meeting(row.id, error)
            store.add_event(
                "meeting_leave",
                room_id=row.room_id,
                data={"meeting": row.id, "platform": row.platform, "source": "hosted", "reason": reason},
            )
            svc.publish_settings(row.room_id)
            if notice:
                svc.notice_in(row.room_id, notice)
        except Exception:
            log.warning("hosted call %s: could not record the end", row.id)
        if st is not None:
            st.ended = True
            st.turns.clear_pending()
            self._wake(st)

    def shutdown(self) -> None:
        """The broker stops: every hosted call ends, so no row is left open for the next start."""
        self._requests.clear()
        for row in self.state.store.open_hosted_meetings():
            self.end(row, "the broker stopped")

    # ------------------------------------------------------------------ time
    def tick(self) -> None:
        """Once every ``TICK_S``: end a call whose machine's sign-in was revoked, that never went live
        within ``JOIN_WINDOW_S``, or that is live and has heard nothing from its key for ``QUIET_S``; close a
        line that has been quiet and post the next one waiting when its turn has come. All by the broker's
        clock. A call that is still joining is given its whole window: the host may be pulling an image or
        a model, and says nothing until it is in the call."""
        self._expire_requests()
        now = self.state.clock.now()
        for row in self.state.store.open_hosted_meetings():
            st = self._state(row)
            if st.polling:
                st.last_seen = now  # a poll that is waiting is the host being there
            if not self._token_live(row):
                self.end(row, "revoked")
            elif not st.live:
                if now - row.started_at >= JOIN_WINDOW_S:  # however much its key was heard from meanwhile
                    self.end(row, "never")
            elif now - st.last_seen >= QUIET_S:
                self.end(row, "quiet")
            else:
                if self._speech_ready(st):
                    self._wake(st)
                for line in st.asm.poll(now):
                    self._enqueue(row, st, line)
                self._drain(row, st, now)

    async def run(self) -> None:
        """``tick`` forever: a task of the broker's lifespan."""
        while True:
            await asyncio.sleep(TICK_S)
            try:
                self.tick()
            except Exception:
                log.exception("hosted calls tick failed")

    def _state(self, row: Meeting) -> _Call:
        st = self._calls.get(row.id)
        if st is None:
            now = self.state.clock.now()
            st = self._calls[row.id] = _Call(row.id, row.room_id, now, live=row.state == "live")
            self._rooms[row.room_id] = row.id
        return st


def _a_call(platform: str) -> str:
    label = PLATFORM_LABEL.get(platform)
    return f"a {label} call" if label else "a call"
