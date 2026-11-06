"""The call's details, for the one person looking (#623, DESIGN.md §54): ``RoomService.call_detail``.

The call is a member of the room in the page: a row in Members that opens its details, with **End this
call** for whoever may end it. The settings frame goes to everyone in the room, so what *this* viewer may do
can't ride in it; the page asks for it (``GET /api/rooms/{slug}/call``), and the answer is built here. These
tests run the real service and the real hosted-calls rules over a real store and a ``FakeClock``; the route
and the page are driven in ``tests/integration/test_call_detail_web.py`` and ``tests/e2e/test_calls_ui.py``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import FakeClock

from switchboard import db
from switchboard.broker.hub import Hub
from switchboard.broker.service import BrokerInfo, RoomService
from switchboard.config import Config
from switchboard.meeting import keys
from switchboard.meeting.hosted import HostedCalls
from switchboard.models import Room
from switchboard.store import Store

KEY = "sbc_" + "ab" * 32
H = keys.key_hash(KEY)


class World:
    def __init__(self, tmp_path: Path, clock: FakeClock) -> None:
        self.clock = clock
        self.cfg = Config(human_name="alice")
        self.store = Store(db.open_db(tmp_path / "c.db"), clock)
        self.svc = RoomService(self.store, Hub(), self.cfg, BrokerInfo(port=7419, test_mode=True), clock)
        self.svc.hosted_calls = HostedCalls(
            SimpleNamespace(store=self.store, service=self.svc, clock=clock, cfg=self.cfg)  # type: ignore[arg-type]
        )
        self.room = self.store.create_room("#build", "alice", 60, 30)
        self.n = 0

    def person(self, name: str) -> int:
        return self.store.person_add(name, hashlib.sha256(name.encode()).digest()[:16], "hash", 3600).id

    def host(
        self, person: int | None, device: str, room: Room | None = None, *, platform: str = "zoom"
    ) -> None:
        """A call hosted from ``device``, the token's (and so the call's) person being ``person``."""
        token = self.store.agent_token_add(person, device, f"{self.n:064x}")
        self.n += 1
        assert token is not None
        self.svc.hosted_calls.open(
            token, room or self.room, {"key_hash": H, "platform": platform, "model": "large-v3-turbo"}
        )

    def detail(self, person_id: int | None, admin: bool = False, room: Room | None = None) -> Any:
        return self.svc.call_detail(room or self.room, person_id, admin)


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock)


def test_a_hosted_call_may_be_ended_by_its_person_and_by_an_admin_and_by_no_one_else(w: World) -> None:
    """``can_end`` is the rule ``/meeting leave`` holds for a hosted call: the person whose it is, or an admin
    (the owner is one). Bob hosts; dave is an ordinary person; erin is an admin the owner made. Fails if the
    answer is the same for every viewer, if another ordinary person may end it, or if the person it belongs to
    or an admin may not."""
    bob, dave, erin = w.person("bob"), w.person("dave"), w.person("erin")
    w.host(bob, "bobs-laptop")
    assert w.detail(bob)["can_end"] is True  # whose call it is
    assert w.detail(dave)["can_end"] is False  # someone else, not an admin
    assert w.detail(erin, admin=True)["can_end"] is True  # an admin of the broker
    assert w.detail(None, admin=True)["can_end"] is True  # the owner, who is one
    assert w.detail(None)["can_end"] is False  # a viewer with no person and no admin: not the owner's call


def test_the_owner_s_call_is_the_owner_s_and_an_ordinary_person_may_not_end_it(w: World) -> None:
    """A call hosted from the owner's machine (``owner_person_id`` None) is the owner's: the owner may end it,
    and so may an admin; an ordinary person may not, though their own ``person_id`` is not None. Fails if
    None is taken for "anyone"."""
    bob = w.person("bob")
    w.host(None, "alice-laptop")
    assert w.detail(None, admin=True)["can_end"] is True
    assert w.detail(bob, admin=True)["can_end"] is True
    assert w.detail(bob)["can_end"] is False


def test_the_broker_s_own_call_may_be_ended_by_anyone_who_may_use_leave_call_today(w: World) -> None:
    """The bot's call has no owner check in ``/meeting leave`` (§52), so the page shows End this call to
    everyone who can see the room, and nothing else of a hosted call: no key, no model, no lines. The server
    still decides: this only says what would be refused. Fails if the bot's call is refused to ordinary
    people, or if it carries a hosted call's fields."""
    bob = w.person("bob")
    row = w.store.insert_meeting(w.room.id, started_by="alice", platform="meet")
    d = w.detail(bob)
    assert d["can_end"] is True and w.detail(None, admin=True)["can_end"] is True
    assert (d["source"], d["platform"], d["state"], d["started_by"]) == ("server", "meet", "joining", "alice")
    assert not {"host", "fingerprint", "model", "lines"} & set(d)
    assert d["live_at"] is None and d["started_at"] == row.started_at


def test_what_it_says_of_a_hosted_call(w: World, clock: FakeClock) -> None:
    """Who started it, which machine, the key's fingerprint, the model, the lines it has sent and when it
    went live (nothing while it is joining, the clock's time once it is). Fails if a field is missing, the
    lines are not the row's count, or ``live_at`` is the time the call began."""
    bob = w.person("bob")
    w.host(bob, "bobs-laptop")
    row = w.store.meeting_by_key(H)
    assert row is not None
    joining = w.detail(bob)
    assert (joining["state"], joining["live_at"], joining["lines"]) == ("joining", None, 0)
    clock.advance(30)
    w.store.set_meeting_live(row.id)
    w.store.con.execute("UPDATE meetings SET lines_pushed=142 WHERE id=?", (row.id,))
    live = w.detail(bob)
    assert live["state"] == "live" and live["live_at"] == clock.now() and live["live_at"] > live["started_at"]
    assert (live["source"], live["platform"], live["host"]) == ("hosted", "zoom", "bobs-laptop")
    assert (live["started_by"], live["model"], live["lines"]) == ("bob", "large-v3-turbo", 142)
    assert live["fingerprint"] == keys.fingerprint(H) and live["voice"] is False


def test_it_carries_no_person_id_no_key_and_no_hash_to_the_page(w: World) -> None:
    """The answer goes to a browser: the owner's id (an internal number), the token's id, the key's hash and
    the key are in none of it, only the owner's name, the machine's label and the key's short fingerprint.
    Fails if a column of the row is copied across, or the hash or the key appears anywhere in the text."""
    bob = w.person("bob")
    w.host(bob, "bobs-laptop")
    d = w.detail(bob)
    text = json.dumps(d)
    assert not {"owner_person_id", "token_id", "key_hash", "id", "error"} & set(d)
    assert H not in text and KEY not in text and "sbc_" not in text
    assert str(bob) not in {str(v) for v in d.values() if not isinstance(v, bool | float)}
    assert d["fingerprint"] == keys.fingerprint(H)


def test_no_call_an_ended_call_and_a_call_the_page_cannot_show_are_none(w: World) -> None:
    """Nothing to show: a room with no call, one whose call ended, and a hosted call that doesn't say whose
    machine it is (the same rule the settings frame holds), so the page never draws a half-known call."""
    assert w.detail(None, admin=True) is None
    w.host(None, "alice-laptop")
    row = w.store.meeting_by_key(H)
    assert row is not None and w.detail(None, admin=True) is not None
    w.store.con.execute("UPDATE meetings SET host=NULL WHERE id=?", (row.id,))
    assert w.detail(None, admin=True) is None
    w.store.con.execute("UPDATE meetings SET host='alice-laptop' WHERE id=?", (row.id,))
    w.store.end_meeting(row.id, None)
    assert w.detail(None, admin=True) is None


def test_a_hosted_call_that_does_not_say_its_platform_is_shown_as_unknown(w: World) -> None:
    """A hosted call may leave its platform out: it is ``unknown``, and the page then says "Call" and draws
    the generic icon. Fails if it is dropped or given a platform."""
    w.host(None, "alice-laptop", platform="unknown")
    d = w.detail(None, admin=True)
    assert d is not None and d["platform"] == "unknown" and d["source"] == "hosted"


def test_in_a_private_room_the_started_by_name_is_given_only_when_they_are_in_the_room(w: World) -> None:
    """A private room's people alone see it (§44), and no name is carried into a room it is not in: the
    person who started the call is named only if they are one of the room's people (as §29.10 does for an
    agent's owner); otherwise the field is None and the page says nothing about who. A public room names them.
    Fails if a person outside the room is named."""
    bob, carol = w.person("bob"), w.person("carol")
    private = w.store.create_room("#secret", "alice", 60, 30, private_for=0)
    w.host(bob, "bobs-laptop", private)
    assert w.detail(None, admin=True, room=private)["started_by"] is None  # bob is not in it
    w.store.room_person_add(private.id, bob, 0)
    assert w.detail(None, admin=True, room=private)["started_by"] == "bob"
    w.store.end_meeting(w.store.meeting_by_key(H).id, None)  # type: ignore[union-attr]
    row = w.store.insert_meeting(private.id, started_by="carol", platform="zoom")
    assert row and w.detail(None, admin=True, room=private)["started_by"] is None  # carol is not in it
    w.store.end_meeting(row.id, None)
    public = w.store.insert_meeting(w.room.id, started_by="carol", platform="teams")
    assert public and w.detail(carol, room=w.room)["started_by"] == "carol"


def test_the_settings_frame_still_says_nothing_that_depends_on_who_is_looking(w: World) -> None:
    """The frame (``settings.meeting``) goes to everyone in the room: it has the fields the bar needs and none
    of the details, so nothing per viewer, no start time and no count can reach someone who must not have
    them. Fails if ``can_end`` or a detail is added to it."""
    bob = w.person("bob")
    w.host(bob, "bobs-laptop")
    frame = w.svc.settings(w.room)["meeting"]
    assert set(frame) == {"state", "platform", "voice", "source", "host", "fingerprint"}
    assert not {"can_end", "started_by", "lines", "model", "live_at", "started_at"} & set(frame)
