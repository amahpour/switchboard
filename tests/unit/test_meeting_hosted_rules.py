"""Calls hosted on a person's machine: the rules (#623, DESIGN.md §54).

``HostedCalls`` over a real store and a ``FakeClock``, with a stand-in for the room service that stores
the lines it is asked to post (so ``meeting_id`` and the counts are the store's own) and records the
notices. Nothing here sleeps: time is the clock's, and the one long poll is woken by the event it waits
for. The broker, the key's two routes and the tool are driven for real in
``tests/integration/test_meeting_hosted.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import FakeClock

from switchboard import db
from switchboard.broker.service import ServiceError
from switchboard.config import Config, MeetingCfg
from switchboard.meeting import hosted, keys
from switchboard.meeting.hosted import HostedCalls, Refused, parse_segments, wait_in
from switchboard.meeting.lines import Segment
from switchboard.models import AgentTokenRow, Room, session_key
from switchboard.store import Store

KEY = "sbc_" + "ab" * 32
H = keys.key_hash(KEY)


class Service:
    """The parts of ``RoomService`` a hosted call uses, over the store: a posted line is a stored message."""

    def __init__(self, store: Store, cfg: Config) -> None:
        self.store, self.cfg = store, cfg
        self.notices: list[tuple[int, str]] = []
        self.published: list[int] = []

    @staticmethod
    def person_key(person_id: int | None) -> int:
        return 0 if person_id is None else person_id

    def person_name(self, key: int) -> str | None:
        if key == 0:
            return self.cfg.human_name
        p = self.store.person(key)
        return p.name if p else None

    def post_meeting_line(
        self, room_id: int, *, sender_name: str, text: str, mentions: list[str], **kw: Any
    ) -> Any:
        return self.store.insert_message(
            room_id,
            sender_name=sender_name,
            sender_kind="meeting",
            via="meeting",
            text=text,
            mentions=mentions,
            **kw,
        )

    def notice_in(self, room_id: int, text: str) -> None:
        self.notices.append((room_id, text))

    def publish_settings(self, room_id: int) -> None:
        self.published.append(room_id)


class World:
    def __init__(self, tmp_path: Path, clock: FakeClock, max_calls: int = 5) -> None:
        self.clock = clock
        self.cfg = Config(human_name="alice").replace(meeting=MeetingCfg(max_calls=max_calls))
        self.store = Store(db.open_db(tmp_path / "h.db"), clock)
        self.service = Service(self.store, self.cfg)
        self.state = SimpleNamespace(store=self.store, service=self.service, clock=clock, cfg=self.cfg)
        self.calls = HostedCalls(self.state)  # type: ignore[arg-type]
        self.build = self.store.create_room("#build", "alice", 60, 30)
        self.uploads = self.store.create_room("#uploads", "alice", 60, 30)
        self.n = 0
        self._laptop: AgentTokenRow | None = None

    def laptop(self) -> AgentTokenRow:
        """The owner's token for ``alice-laptop``: the machine a call is hosted from unless a test says."""
        if self._laptop is None:
            self._laptop = self.token("alice-laptop")
        return self._laptop

    def token(self, device: str = "alice-laptop", person: int | None = None) -> AgentTokenRow:
        row = self.store.agent_token_add(person, device, f"{self.n:064x}")
        self.n += 1
        assert row is not None
        return row

    def open(
        self, token: AgentTokenRow | None = None, room: Room | None = None, **params: Any
    ) -> dict[str, Any]:
        given = {"key_hash": H, "platform": "zoom", "model": "large-v3-turbo", **params}
        return self.calls.open(token or self.laptop(), room or self.build, given)

    def member(
        self,
        room: Room,
        name: str,
        *,
        device: str = "alice-laptop",
        session: str = "s-1",
        harness: str = "claude",
    ) -> Any:
        """An agent session of ``harness`` on ``device``, in ``room`` as ``name``: the machine's member."""
        p = self.store.upsert_participant(harness, session_key(harness, device, session), host=device)
        return self.store.create_membership(room.id, p.id, name)

    def register(
        self,
        token: AgentTokenRow | None = None,
        room: str = "#build",
        *,
        session: str | None = None,
        harness: str | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        given = {"room": room, "key_hash": H, "platform": "zoom", **params}
        return self.calls.register(token or self.laptop(), given, session=session, harness=harness)

    def row(self, room: Room | None = None) -> Any:
        return self.store.meeting_by_key(H) if room is None else self.store.open_meeting(room.id)

    def lines(self, room: Room | None = None) -> list[Any]:
        return [m for m in self.store.history((room or self.build).id) if m.sender_kind == "meeting"]

    def push(self, *segments: Segment) -> dict[str, Any]:
        return self.calls.push(self.row(), list(segments))

    def texts(self) -> list[str]:
        return [m.text for m in self.lines()]

    def notice_texts(self) -> list[str]:
        return [t for _r, t in self.service.notices]


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock)


def seg(text: str, start: float, end: float, speaker: str | None = "bob") -> Segment:
    return Segment(text=text, start=start, end=end, speaker=speaker)


# ------------------------------------------------------------------------------------------ opening
def test_a_hosted_call_belongs_to_the_machine_and_person_of_the_token_not_to_what_the_agent_says(
    w: World,
) -> None:
    """The owner, the machine's label and the person's name come from the token; the agent supplies only
    the key's hash, the platform and the model, and a long model name is cut. The answer says what to check
    (the fingerprint) and holds neither the key nor its hash, and the room is told who hosts and from where.
    Fails if any of the three is taken from the arguments, the hash is echoed, or the notice is missing."""
    bob = w.store.person_add("bob", b"b" * 16, "x" * 64, 3600.0).id
    token = w.token("bobs-laptop", bob)
    got = w.open(token, model="m" * 200, device="mallory-pc", owner="alice", host="elsewhere")
    row = w.row()
    assert (row.source, row.state, row.platform, row.host) == ("hosted", "joining", "zoom", "bobs-laptop")
    assert (row.started_by, row.owner_person_id, row.token_id, row.key_hash) == ("bob", bob, token.id, H)
    assert row.model == "m" * keys.MODEL_MAX and row.lines_pushed == 0
    assert (
        got["fingerprint"] == keys.fingerprint(H)
        and got["state"] == "joining"
        and got["host"] == "bobs-laptop"
    )
    assert H not in json.dumps(got) and KEY not in json.dumps(got)
    assert w.notice_texts() == ["bob is hosting a Zoom call from bobs-laptop"]
    assert w.service.published == [w.build.id]
    [event] = w.store.recent_events(room_id=w.build.id, kinds=["meeting_host"])
    assert event.data["key"] == keys.fingerprint(H) and event.data["host"] == "bobs-laptop"
    assert H not in json.dumps(event.data) and KEY not in json.dumps(event.data)


def test_a_call_that_does_not_say_its_platform_is_unknown_and_the_room_is_told_so_without_one(
    w: World,
) -> None:
    """``platform`` may be left out or empty: the call is ``unknown`` and the notice names no platform.
    Fails if the empty value is refused, or a platform is invented."""
    bob = w.store.person_add("bob", b"b" * 16, "x" * 64, 3600.0).id
    w.open(w.laptop(), w.build, key_hash="cd" * 32, platform="")
    w.open(w.token("bobs-laptop", bob), w.uploads, key_hash="ef" * 32, platform=None)
    assert [r.platform for r in w.store.open_hosted_meetings()] == ["unknown", "unknown"]
    assert w.notice_texts() == [
        "alice is hosting a call from alice-laptop",
        "bob is hosting a call from bobs-laptop",
    ]


@pytest.mark.parametrize(
    "params, says",
    [
        ({"key_hash": KEY}, "key_hash is the 64 hex"),  # the key itself, where its hash belongs
        ({"key_hash": H.upper()}, "key_hash is the 64 hex"),
        ({"key_hash": "ab" * 31}, "key_hash is the 64 hex"),
        ({"key_hash": None}, "key_hash is the 64 hex"),
        ({"platform": "webex"}, "platform is zoom, meet, teams or unknown"),
        ({"platform": 3}, "platform is zoom, meet, teams or unknown"),
    ],
)
def test_a_claim_that_is_not_one_of_the_known_values_is_refused_and_nothing_is_kept(
    w: World, params: dict[str, Any], says: str
) -> None:
    """Fails closed: a hash that is not 64 lower-case hex (the key itself included) and a platform that is not
    one of four are refused, with a sentence that never repeats the value, and no call is opened."""
    with pytest.raises(ServiceError) as e:
        w.open(**params)
    assert e.value.code == "bad_request" and says in e.value.message
    assert KEY not in e.value.message and "webex" not in e.value.message
    assert w.store.open_hosted_meetings() == [] and w.service.notices == []


# ------------------------------------------------------------------------------ registering a call
def test_a_machine_with_an_agent_in_the_room_may_host_a_call_there_and_no_other_machine_may(w: World) -> None:
    """The room needs a member on the machine the token belongs to. Alice's desktop has claude-1 in #build, so
    her laptop's token is refused for it (the same person, another machine), and so is a stranger's; the
    desktop's own is let in and the answer is the five things it may say. Fails if any machine will do, or the
    refusal leaves a call behind."""
    w.member(w.build, "claude-1", device="alice-desktop")
    for token in (w.laptop(), w.token("bobs-laptop")):
        with pytest.raises(ServiceError) as e:
            w.register(token)
        assert e.value.code == "not_member" and "#build" in e.value.message
    assert w.store.open_hosted_meetings() == []
    got = w.register(w.token("alice-desktop"))
    assert got == {
        "room": "#build",
        "state": "joining",
        "platform": "zoom",
        "host": "alice-desktop",
        "fingerprint": keys.fingerprint(H),
    }


def test_a_named_session_has_to_be_a_member_of_that_room_on_that_machine_and_of_that_harness(
    w: World,
) -> None:
    """With a session named (a listener key's own, or what a token's script says) the room needs a member of
    that session on the token's machine: another session of the machine, the same id on another machine and
    the same id of another harness all fail. Fails if the session is ignored or its machine or harness is."""
    w.member(w.build, "claude-1", session="s-1")
    w.member(w.uploads, "claude-2", session="s-2")
    w.member(w.uploads, "claude-3", device="alice-desktop", session="s-1")  # the same id, another machine
    for room, session, harness in (
        ("#build", "s-2", None),  # the machine's other session
        ("#uploads", "s-1", None),  # this session is not in #uploads on this machine
        ("#build", "s-1", "codex"),  # a codex session with that id is not claude's
        ("#build", "nobody", None),
    ):
        with pytest.raises(ServiceError) as e:
            w.register(room=room, session=session, harness=harness)
        assert e.value.code == "not_member", (room, session, harness)
    assert w.store.open_hosted_meetings() == []
    assert w.register(session="s-1", harness="claude")["room"] == "#build"


def test_a_member_that_left_or_whose_session_ended_no_longer_counts(w: World) -> None:
    """Only a live session with a live membership is a member on the machine. Fails if a left or ended one
    still lets its machine host a call into the room."""
    gone = w.member(w.build, "claude-1", session="s-1")
    ended = w.member(w.build, "claude-2", session="s-2")
    w.store.end_membership(gone.id, "left")
    w.store.end_participant(ended.participant_id)
    # a session that ended with a membership left behind cannot happen (ending one leaves its rooms), and
    # if it ever did the room would not count it
    stale = w.member(w.build, "claude-4", session="s-4")
    w.store.con.execute("UPDATE participants SET ended_at=? WHERE id=?", (w.clock.now(), stale.participant_id))
    for session in (None, "s-1", "s-2", "s-4"):
        with pytest.raises(ServiceError, match="no agent on this machine"):
            w.register(session=session)
    w.member(w.build, "claude-3", session="s-3")
    assert w.register()["room"] == "#build"


def test_a_room_may_be_asked_for_by_a_name_it_had_and_a_wrong_room_is_the_same_refusal(
    w: World, clock: FakeClock
) -> None:
    """A renamed room is still found by its old name (as the agent tools find it, #316); a room that does not
    exist is refused exactly like one the machine has no member in, so the answer tells nothing about which
    rooms there are; a name that is not a room name, or none, is a bad request. Fails if the old name stops
    working, or a missing room is told from a room the machine is not in."""
    w.member(w.build, "claude-1")
    w.store.rename_open_room(w.build.id, "#ship", expect="#build")
    assert w.register(room="#build")["room"] == "#ship"
    w.store.end_meeting(w.store.open_meeting(w.build.id).id, None)  # type: ignore[union-attr]
    clock.advance(hosted.HOST_WINDOW_S)  # (five tries a minute, below)
    said = set()
    for room in ("#uploads", "#nowhere"):
        with pytest.raises(ServiceError) as e:
            w.register(room=room, key_hash="cd" * 32)
        said.add((e.value.code, e.value.message.replace(room, "#r")))
    assert len(said) == 1
    clock.advance(hosted.HOST_WINDOW_S)
    for room in (None, "", "   ", "not a room!", 7):
        with pytest.raises(ServiceError) as e:
            w.register(room=room, key_hash="cd" * 32)  # type: ignore[arg-type]
        assert e.value.code == "bad_request"


def test_a_token_may_try_to_register_five_times_a_minute_by_the_clock_and_every_try_counts(
    w: World, clock: FakeClock
) -> None:
    """Five tries within a minute, whatever their outcome (a refused one counts), then 429 with the seconds
    until the oldest try is a minute old; the window slides, so that try leaving gives one back. Another token
    has its own, and a token that was quiet for the window starts again. Fails if the limit is not per token,
    is not by the broker's clock, a refused try is free, or the memory of quiet tokens is kept for ever."""
    w.member(w.build, "claude-1")
    w.member(w.uploads, "claude-2", device="alice-desktop")
    desktop = w.token("alice-desktop")
    for _ in range(hosted.HOST_PER_WINDOW):
        with pytest.raises(ServiceError):
            w.register(key_hash="no")
        clock.advance(1.0)  # the tries are at 0, 1, 2, 3 and 4 seconds
    with pytest.raises(Refused) as e:
        w.register(key_hash="no")
    assert (e.value.status, e.value.code) == (429, "rate_limited")
    assert e.value.retry_after == pytest.approx(hosted.HOST_WINDOW_S - 5.0)
    with pytest.raises(ServiceError):
        w.register(desktop, "#uploads", key_hash="no")  # the other token's own allowance
    clock.advance(hosted.HOST_WINDOW_S - 5.0)  # a minute since the first try
    assert w.register()["room"] == "#build"
    with pytest.raises(
        Refused
    ):  # four of the earlier tries are still inside the window, and this one is a fifth
        for _ in range(hosted.HOST_PER_WINDOW):
            w.register(key_hash="no")
    clock.advance(hosted.HOST_WINDOW_S)
    with pytest.raises(ServiceError):
        w.register(
            desktop, "#uploads", key_hash="no"
        )  # asking for another token drops the quiet one's record
    assert set(w.calls._asked) == {desktop.id}


def test_a_room_with_a_call_open_cannot_have_a_second_whatever_the_source(w: World) -> None:
    """A call has one source: a room that the broker's own bot is following, or that another machine hosts,
    refuses. Fails if a hosted call can start beside either."""
    w.store.insert_meeting(w.build.id, started_by="alice", platform="zoom")
    with pytest.raises(ServiceError, match="already has a call"):
        w.open()
    w.store.end_meeting(w.store.open_meeting(w.build.id).id, None)  # type: ignore[union-attr]
    w.open()
    with pytest.raises(ServiceError, match="already has a call"):
        w.open(w.token("bobs-laptop"), key_hash="cd" * 32)


def test_a_person_hosts_one_call_at_a_time_across_their_machines_but_two_people_may_each_host_one(
    w: World,
) -> None:
    """One hosted call per person: a second token of the same person is refused (the owner's tokens are the
    owner's), even in another room, and ends when the first does. Another person is not held by it. Fails if
    the limit is per token or per room."""
    w.open(w.laptop())
    desktop = w.token("alice-desktop")
    with pytest.raises(ServiceError, match="you already host a call"):
        w.open(desktop, w.uploads, key_hash="cd" * 32)
    bob = w.store.person_add("bob", b"b" * 16, "x" * 64, 3600.0).id
    w.open(w.token("bobs-laptop", bob), w.uploads, key_hash="cd" * 32)
    assert len(w.store.open_hosted_meetings()) == 2
    w.calls.stop(w.build.id, "leave")
    w.open(desktop, w.build, key_hash="ef" * 32)  # her first one is over


def test_the_broker_follows_at_most_max_calls_across_both_sources(tmp_path: Path, clock: FakeClock) -> None:
    """``[meeting] max_calls`` counts every open call, the broker's own and hosted ones. The refusal gives
    only the number (the agent may not see the rooms that hold the calls). Fails if hosted calls go past
    it, or the answer names a room."""
    w = World(tmp_path, clock, max_calls=1)
    w.store.insert_meeting(w.uploads.id, started_by="alice", platform="zoom")
    with pytest.raises(ServiceError) as e:
        w.open()
    assert e.value.code == "conflict" and "at most 1 call at once" in e.value.message
    assert "#uploads" not in e.value.message
    assert w.store.open_hosted_meetings() == []


def test_a_key_is_for_one_call_and_cannot_be_registered_again_even_after_it_ended(w: World) -> None:
    """The hash is unique across every call ever opened: a key that was used is not taken again, which is
    what keeps one key from ever naming two calls. Fails if an ended call's key can be registered anew."""
    w.open()
    w.calls.stop(w.build.id, "leave")
    with pytest.raises(ServiceError, match="used for a call before"):
        w.open(w.token("alice-desktop"))


# ------------------------------------------------------------------------------------------- the key
def test_a_key_opens_only_its_own_call_and_only_while_the_call_and_its_machine_are_live(w: World) -> None:
    """``check`` is what both routes ask first. A key that was never registered and one that is malformed
    look alike. A registered key opens its call; once the call ended the word is ``ended``; and a call whose
    machine's token was revoked is ended on the spot, with the word ``revoked``. Fails if a refusal gives
    anything away, a revoked machine keeps pushing, or an ended call's key still opens it."""
    token = w.laptop()
    w.open(token)
    assert w.calls.check(KEY).call.id == w.row().id  # type: ignore[union-attr]
    other = "sbc_" + "cd" * 32
    for bad in (other, "", "sbc_", "sbl_" + "ab" * 32, KEY.upper(), KEY + "0", "Bearer " + KEY):
        got = w.calls.check(bad)
        assert (got.call, got.why) == (None, "unknown_key"), bad
    w.store.agent_token_revoke(token.id, None)
    got = w.calls.check(KEY)
    assert (got.call, got.why) == (None, "revoked")
    assert w.row().state == "ended" and "its machine's sign-in was revoked" in w.row().error
    assert w.notice_texts()[-1] == "The call ended: its machine's sign-in was revoked."
    assert w.calls.check(KEY).why == "ended"


# ------------------------------------------------------------------------------------------ pushing
def test_the_first_push_even_an_empty_one_marks_the_call_live(w: World) -> None:
    """The relay's first push, with nothing in it, says it is in the call: the bar changes from joining to
    transcribing. Fails if only a push with words marks it live, or it is marked live twice."""
    w.open()
    assert w.row().state == "joining"
    got = w.calls.push(w.row(), [])
    assert got == {"accepted": 0, "duplicate": 0, "ended": False}
    assert w.row().state == "live" and w.service.published == [w.build.id, w.build.id]
    assert w.notice_texts()[-1] == "Transcribing a Zoom call from alice-laptop."
    w.calls.push(w.row(), [])
    assert w.service.published == [w.build.id, w.build.id] and len(w.service.notices) == 2


def test_segments_become_lines_the_same_way_the_brokers_own_bot_makes_them_and_each_is_stored_with_its_call(
    w: World, clock: FakeClock
) -> None:
    """Segments of one speaker with under two seconds between them are one line; a pause of two seconds or
    another speaker ends it. A call-out wakes only the member named. Each line is stored as a ``meeting``
    message with the call's id and the machine that heard it, and the call counts it. Fails if the shared
    assembler or matcher is not used, or a line is stored without its call."""
    w.open()
    w.push(seg("Morning.", 0, 1), seg("Quick one", 1.5, 2.5), seg("Claude one, can you check", 3.0, 4.0))
    w.push(seg("the upload limit?", 4.2, 5.0))
    clock.advance(3)  # quiet on the wall clock: the line closes
    w.calls.tick()
    assert w.texts() == ["Morning. Quick one Claude one, can you check the upload limit?"]
    [line] = w.lines()
    assert (line.sender_name, line.sender_kind, line.via, line.sender_host) == (
        "bob",
        "meeting",
        "meeting",
        "alice-laptop",
    )
    assert w.row().lines_pushed == 1
    con = w.store.con
    assert con.execute("SELECT meeting_id FROM messages WHERE id=?", (line.id,)).fetchone()[0] == w.row().id


def test_a_call_out_in_a_pushed_line_addresses_the_member_it_names(w: World, clock: FakeClock) -> None:
    """The member's name is matched on the room's active names, as for the broker's own bot, so only that
    member is mentioned. Fails if the match is skipped for a pushed line."""
    m = w.store.upsert_participant("test", session_key("test", "", "c1"), status="idle", tier="mcp-only")
    w.store.create_membership(w.build.id, m.id, "claude-1")
    w.open()
    w.push(seg("Claude one, can you check the upload limit?", 0, 3))
    clock.advance(3)
    w.calls.tick()
    assert w.lines()[0].mentions == ["claude-1"]


def test_lines_are_posted_at_most_one_a_second_and_the_rest_wait_their_turn(
    w: World, clock: FakeClock
) -> None:
    """A burst of lines is paced as the broker's own bot's are: the first goes at once, the next only when
    a second has passed on the clock. Fails if a burst is posted in one go, or a waiting line is lost."""
    w.open()
    w.push(*(seg(f"line {i}", i * 5.0, i * 5.0 + 1, speaker=f"s{i}") for i in range(4)))
    assert w.texts() == ["line 0"]
    clock.advance(0.6)
    w.calls.tick()
    assert w.texts() == ["line 0"]  # not yet a second
    clock.advance(0.6)
    w.calls.tick()
    assert w.texts() == ["line 0", "line 1"]
    clock.advance(1.1)
    w.calls.tick()
    assert w.texts() == ["line 0", "line 1", "line 2"]
    clock.advance(0.3)  # the last line closes now (2.5 s without a word), but a second has not passed
    w.calls.tick()
    assert w.texts() == ["line 0", "line 1", "line 2"]
    clock.advance(1.0)
    w.calls.tick()
    assert w.texts() == ["line 0", "line 1", "line 2", "line 3"]


def test_a_push_that_is_sent_twice_is_taken_once(w: World, clock: FakeClock) -> None:
    """A relay retries a push whose answer was lost: the lines must not be in the room twice. The broker
    skips a segment it has been given, as it skips one it has read already from its own bot. Fails if a
    retry posts again."""
    w.open()
    batch = [seg("hello there", 0, 1)]
    assert w.calls.push(w.row(), batch)["accepted"] == 1
    again = w.calls.push(w.row(), batch)
    assert (again["accepted"], again["duplicate"]) == (0, 1)
    clock.advance(3)
    w.calls.tick()
    assert w.texts() == ["hello there"]


def test_pushes_are_limited_to_ten_segments_a_second_with_a_burst_of_a_hundred(
    w: World, clock: FakeClock
) -> None:
    """A source that sends faster than a call can talk is refused with a 429 and when to try again, and
    nothing of the refused push is taken. The allowance fills at ten a second to a hundred. An empty push
    costs one, so a flood of those is refused too. Fails if the limit is missing, takes part of a refused
    batch, or never refills."""
    w.open()

    def burst(n: int, base: float) -> list[Segment]:
        return [seg(f"s{base}-{i}", base + i * 5.0, base + i * 5.0 + 1, speaker=f"p{i}") for i in range(n)]

    w.push(*burst(100, 0))  # the whole burst
    with pytest.raises(Refused) as e:
        w.push(*burst(1, 1000))
    assert (e.value.status, e.value.code) == (429, "rate_limited") and 0 < e.value.retry_after <= 0.2  # type: ignore[operator]
    clock.advance(0.25)  # two and a half segments' worth
    assert w.push(*burst(2, 2000))["accepted"] == 2
    clock.advance(1.0)  # ten and a half
    with pytest.raises(Refused):
        w.push(*burst(11, 3000))  # eleven in a second
    assert w.push(*burst(10, 4000))["accepted"] == 10  # the refused push took nothing, so ten still fit
    clock.advance(1.0)
    for _ in range(10):
        w.calls.push(w.row(), [])  # an empty push costs one
    with pytest.raises(Refused):
        w.calls.push(w.row(), [])


# ------------------------------------------------------------------------------- what is accepted
def test_a_push_body_is_checked_whole_text_and_speaker_are_cut_and_nothing_is_half_taken() -> None:
    """Fields are cut and cleaned (a claim from outside) and a body that is not a list of segments with a
    finite start and end is refused as a whole. A batch past one hundred is 413. Fails if text or a
    speaker is kept long, a NaN or a boolean passes as a time, or one bad segment lets the rest through."""
    got = parse_segments(
        {
            "segments": [
                {"speaker": "x" * 90 + "\x1b[2J", "text": "t" * 5000, "start": 0, "end": 1.5, "role": "bot"},
                {"speaker": "", "text": "no speaker", "start": 1, "end": 2},
                {"text": "none", "start": 2, "end": 3, "speaker": 7},
            ]
        }
    )
    assert got[0].speaker == "x" * 40 and len(got[0].text) == hosted.TEXT_MAX == 2000
    assert got[0].role == "participant"  # a pushed segment is always the call's speech: no role is taken
    assert [s.speaker for s in got[1:]] == [None, None]
    assert parse_segments({"segments": []}) == []
    for bad in (
        None,
        [],
        {},
        {"segments": "x"},
        {"segments": [None]},
        {"segments": [{"text": 5, "start": 0, "end": 1}]},
        {"segments": [{"text": "a", "start": True, "end": 1}]},
        {"segments": [{"text": "a", "start": float("nan"), "end": 1}]},
        {"segments": [{"text": "a", "start": 0, "end": float("inf")}]},
        {"segments": [{"text": "a", "start": "0", "end": 1}]},
        {"segments": [{"text": "a", "start": 0}]},
        {"segments": [{"text": "ok", "start": 0, "end": 1}, {"nope": 1}]},
    ):
        with pytest.raises(Refused) as e:
            parse_segments(bad)
        assert e.value.status == 400, bad
    with pytest.raises(Refused) as e:
        parse_segments({"segments": [{"text": "a", "start": 0, "end": 1}] * (hosted.BATCH_MAX + 1)})
    assert (e.value.status, e.value.code) == (413, "too_large")
    assert len(parse_segments({"segments": [{"text": "a", "start": 0, "end": 1}] * hosted.BATCH_MAX})) == 100


def test_a_speech_poll_waits_a_number_of_seconds_up_to_thirty() -> None:
    """The wait is the poll's own ask, held to 30 s (far inside the Ingress's 16 minutes); none asked is 25;
    anything that is not a number is refused. Fails if a huge wait is honoured, or text passes."""
    assert wait_in({}) == 25.0 and wait_in(None) == 25.0 and wait_in({"wait": 10}) == 10.0
    assert wait_in({"wait": 9999}) == 30.0 and wait_in({"wait": -5}) == 0.0 and wait_in({"wait": 0}) == 0.0
    for bad in ("5", True, [1], float("nan"), float("inf")):
        with pytest.raises(Refused):
            wait_in({"wait": bad})


# ------------------------------------------------------------------------------------------- ending
def test_ending_posts_what_is_left_refuses_the_key_and_leaves_the_lines_with_their_label(w: World) -> None:
    """A call that ends posts the line still being assembled, marks the row ended and refuses its key from
    then on. The lines already pushed keep their call and their machine. Ending twice changes nothing.
    Fails if the last words are lost, the key still opens the call, or the label goes with the call."""
    w.open()
    w.push(seg("first", 0, 1), seg("and the last words", 1.2, 2.0))
    assert w.texts() == []  # still one open line
    assert w.calls.stop(w.build.id, "leave") is True
    assert w.texts() == ["first and the last words"]
    assert w.lines()[0].sender_host == "alice-laptop" and w.row().lines_pushed == 1
    assert w.row().state == "ended" and w.row().error is None
    assert w.calls.check(KEY).why == "ended"
    assert w.calls.stop(w.build.id, "leave") is False  # nothing hosted is open now
    assert len(w.service.notices) == 2  # hosting, and live: a person's /meeting leave says the rest itself


async def test_a_request_that_waited_for_its_body_is_refused_when_the_call_ended_meanwhile(w: World) -> None:
    """A request is checked at the door and then waits for its body, and the call may end in between. What it
    holds is then the call as it was: it must be refused, and must not bring the ended call's memory back (a
    ghost that would still say voice is on, and post lines under the ended call). Fails if a push or a poll
    with the old row is taken."""
    w.open()
    w.calls.push(w.row(), [])
    stale = w.row()
    w.calls.end(stale, "leave")
    for attempt in (lambda: w.calls.push(stale, [seg("too late", 0, 1)]), lambda: w.calls.speech(stale, 0)):
        with pytest.raises(Refused) as e:
            result = attempt()
            if asyncio.iscoroutine(result):
                await result
        assert (e.value.status, e.value.code) == (410, "ended")
    assert w.calls._calls == {} and w.calls._rooms == {}
    assert w.texts() == []


def test_stopping_a_room_with_no_hosted_call_does_nothing_and_says_so(w: World) -> None:
    """The room service asks the hosted calls first when a room closes, then the broker's own bot: this
    answers False for a room whose call is the bot's or none. Fails if it ends the bot's call."""
    assert w.calls.stop(w.build.id, "the room closed") is False
    own = w.store.insert_meeting(w.build.id, started_by="alice", platform="zoom")
    assert w.calls.stop(w.build.id, "the room closed") is False
    assert w.store.meeting_by_id(own.id).state == "joining"  # type: ignore[union-attr]


def test_who_may_end_a_hosted_call(w: World) -> None:
    """The person it belongs to, or an admin. Fails if another person can end it, or the person it belongs
    to cannot."""
    bob = w.store.person_add("bob", b"b" * 16, "x" * 64, 3600.0).id
    dave = w.store.person_add("dave", b"d" * 16, "x" * 64, 3600.0).id
    w.open(w.token("bobs-laptop", bob))
    row = w.row()
    assert w.calls.may_end(row, bob, False) is True
    assert w.calls.may_end(row, dave, False) is False
    assert w.calls.may_end(row, dave, True) is True  # an admin
    assert w.calls.may_end(row, None, False) is False  # not the owner's call


# ------------------------------------------------------------------------------------------ timeouts
def test_a_hosted_call_that_never_goes_live_ends_after_ten_minutes(w: World, clock: FakeClock) -> None:
    """The host script started and no push came: the call is not kept open for ever. By the clock, never a
    sleep: nine minutes fifty-nine is fine, ten ends it, with a notice and the phrase in its row, and the
    key is refused. Fails if it never ends, ends early, or ends silently."""
    w.open()
    clock.advance(hosted.JOIN_WINDOW_S - 1)  # nothing heard for nine minutes: the host may be pulling a model
    w.calls.tick()
    assert w.row().state == "joining"
    clock.advance(1)
    w.calls.tick()
    assert (w.row().state, w.row().error) == ("ended", "its host never started sending")
    assert w.notice_texts()[-1] == "The call ended: its host never started sending."
    assert w.calls.check(KEY).why == "ended"


def test_a_live_call_that_hears_nothing_from_its_key_ends_after_five_minutes_and_any_request_counts(
    w: World, clock: FakeClock
) -> None:
    """A host that has gone (a laptop shut, a crashed relay) ends its call; one that is only slow does not:
    a push, an empty one too, or a poll starts the five minutes again. Fails if the clock is not reset by a
    request, or a gone host keeps the call and its slot."""
    w.open()
    w.calls.push(w.row(), [])  # live
    clock.advance(hosted.QUIET_S - 1)
    w.calls.tick()
    assert w.row().state == "live"
    w.calls.push(w.row(), [])  # heard from again
    clock.advance(hosted.QUIET_S - 1)
    w.calls.tick()
    assert w.row().state == "live"
    clock.advance(1)
    w.calls.tick()
    assert (w.row().state, w.row().error) == ("ended", "its host went quiet")
    assert w.notice_texts()[-1] == "The call ended: its host went quiet."


async def test_a_host_that_is_waiting_in_a_poll_is_not_quiet(w: World, clock: FakeClock) -> None:
    """The relay holds a long poll open, so a call with nobody speaking is alive as long as that poll is
    waiting. Fails if a call is ended for quiet while its relay is polling."""
    w.open()
    w.calls.push(w.row(), [])
    poll = asyncio.ensure_future(w.calls.speech(w.row(), 30))
    await asyncio.sleep(0)  # let it start waiting
    clock.advance(hosted.QUIET_S * 3)
    w.calls.tick()
    assert w.row().state == "live"
    w.calls.end(w.row(), "leave")
    assert (await poll)["ended"] is True


def test_a_token_that_is_revoked_ends_its_call_at_the_next_tick_without_waiting_for_the_relay(
    w: World, clock: FakeClock
) -> None:
    """Revoking the machine's sign-in takes the call down at once: a silent relay would otherwise leave the
    bar saying the call is live. Fails if only a request notices."""
    token = w.laptop()
    w.open(token)
    w.calls.push(w.row(), [])
    w.store.agent_token_revoke(token.id, None)
    w.calls.tick()
    assert (w.row().state, w.row().error) == ("ended", "its machine's sign-in was revoked")


def test_the_broker_stopping_ends_every_hosted_call_and_says_so(w: World) -> None:
    w.open()
    w.calls.shutdown()
    assert w.row().state == "ended" and w.notice_texts()[-1] == "The call ended because the broker stopped."


# -------------------------------------------------------------------------------------------- voice
def test_an_at_call_line_is_queued_only_while_voice_is_on_and_the_host_is_in_the_call(w: World) -> None:
    """The same rules as the broker's own bot (§52): voice starts off; a line is queued only for the host
    that has gone live, only if it starts with ``@call``, and is made speakable. Turning voice off drops the
    waiting lines. Fails if a line is queued with voice off, before the host is in, or without ``@call``."""
    w.open()
    w.calls.speak(w.build.id, "@call hello everyone")
    assert w.calls.voice(w.build.id) is False
    assert w.calls.set_voice(w.build.id, True) is True
    assert w.calls.set_voice(w.build.id, True) is False  # no change
    w.calls.speak(w.build.id, "@call before the host is in")
    w.calls.push(w.row(), [])  # live
    w.calls.speak(w.build.id, "no at-call here")
    w.calls.speak(w.build.id, "@call **hello** [everyone](https://example.com)")
    w.calls.speak(w.build.id, "@call ")
    assert list(w.calls._calls[w.row().id].speech) == ["hello everyone"]
    assert w.calls.set_voice(w.build.id, False) is True
    assert list(w.calls._calls[w.row().id].speech) == []


def test_at_most_three_lines_wait_to_be_spoken_and_the_overflow_is_told_once(w: World) -> None:
    """A slow relay must not let spoken lines pile up: a fourth is dropped, with one notice for the burst.
    Fails if the queue grows, or every drop is announced."""
    w.open()
    w.calls.push(w.row(), [])
    w.calls.set_voice(w.build.id, True)
    for i in range(6):
        w.calls.speak(w.build.id, f"@call line {i}")
    assert list(w.calls._calls[w.row().id].speech) == ["line 0", "line 1", "line 2"]
    assert sum("waiting to be spoken" in t for t in w.notice_texts()) == 1


async def test_the_speech_poll_answers_with_the_lines_the_hints_the_voice_state_and_the_end(w: World) -> None:
    """What the relay learns each round: the lines to say (only with voice on), the room's names as the
    transcriber's hotwords, whether voice is on, and whether the call is over. A line is given once. Fails
    if lines are given with voice off, repeated, or the end is not said."""
    m = w.store.upsert_participant("test", session_key("test", "", "c1"), status="idle", tier="mcp-only")
    w.store.create_membership(w.build.id, m.id, "claude-1")
    w.open()
    w.calls.push(w.row(), [])
    quiet = await w.calls.speech(w.row(), 0)
    assert quiet["lines"] == [] and quiet["voice"] is False and quiet["ended"] is False
    assert "Claude" in quiet["hints"] and "Claude one" in quiet["hints"]
    assert all(len(h) <= 40 for h in quiet["hints"])
    w.calls.set_voice(w.build.id, True)
    w.calls.speak(w.build.id, "@call the limit is ten megabytes")
    got = await w.calls.speech(w.row(), 0)
    assert got["lines"] == ["the limit is ten megabytes"] and got["voice"] is True
    assert (await w.calls.speech(w.row(), 0))["lines"] == []
    w.calls.end(w.row(), "leave")
    assert w.calls.check(KEY).why == "ended"


async def test_a_waiting_poll_is_answered_the_moment_there_is_something_to_say(w: World) -> None:
    """The long poll is not a poll-and-sleep: a line from an agent, a change of the voice switch, or the end
    of the call answers it at once, and a newer poll answers an older one, empty. Waits are on events, so
    this finishes at once. Fails if the poll sits out its wait, or two polls both get the line."""
    w.open()
    w.calls.push(w.row(), [])
    w.calls.set_voice(w.build.id, True)

    first = asyncio.ensure_future(w.calls.speech(w.row(), 30))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(w.calls.speech(w.row(), 30))
    assert (await asyncio.wait_for(first, 5))["lines"] == []  # superseded
    w.calls.speak(w.build.id, "@call hello")
    assert (await asyncio.wait_for(second, 5))["lines"] == ["hello"]

    third = asyncio.ensure_future(w.calls.speech(w.row(), 30))
    await asyncio.sleep(0)
    w.calls.set_voice(w.build.id, False)
    got = await asyncio.wait_for(third, 5)
    assert got["voice"] is False and got["lines"] == []

    fourth = asyncio.ensure_future(w.calls.speech(w.row(), 30))
    await asyncio.sleep(0)
    w.calls.end(w.row(), "leave")
    assert (await asyncio.wait_for(fourth, 5))["ended"] is True
