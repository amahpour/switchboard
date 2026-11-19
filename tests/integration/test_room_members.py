"""A public room's members on a hosted broker (#843, DESIGN.md §63): people join and leave public rooms,
and ``@humans``, Home's "waiting on you" and so the notifications built on it reach only a room's
members. An agent joining makes its person a member. Who agents answer to does not change, private rooms
keep their own people, and a broker with one person sees nothing new."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import InProcBroker
from test_agents_http import THREAD_B, Codex, make_token
from test_passkeys import HOST, Browser, hosted_broker, stop
from test_people import join_as_bob, set_up

from switchboard import db
from switchboard.paths import Paths
from switchboard.store import Store


@pytest.fixture
def team() -> Iterator[tuple[InProcBroker, Browser, Browser]]:
    """A hosted broker: alice (the owner) and bob; alice made #build and is its only member, and
    claude-1 (alice's, on the broker's own machine) is in it."""
    b = hosted_broker()
    alice = set_up(b)
    bob = join_as_bob(b, alice)
    try:
        assert alice.post("/api/rooms", {"name": "#build"}).status_code == 200

        def seed() -> None:
            store = b.state.store
            p = store.upsert_participant("test", "test:claude-1", host="")
            room = store.get_room("#build")
            assert room is not None
            store.create_membership(room.id, p.id, "claude-1")

        b.on_loop(seed)
        yield b, alice, bob
    finally:
        bob.close()
        alice.close()
        stop(b)


def rooms(br: Browser) -> dict[str, dict[str, Any]]:
    r = br.get("/api/rooms")
    assert r.status_code == 200, r.text
    return {x["name"]: x for x in r.json()["rooms"]}


def people(br: Browser, slug: str, action: str, name: str) -> Any:
    return br.post(f"/api/rooms/{slug}/people", {"action": action, "name": name})


def agent_says(b: InProcBroker, text: str, mentions: list[str]) -> int:
    """claude-1 says ``text`` in #build, its ``mentions`` as the broker would have matched them."""

    def run() -> int:
        store = b.state.store
        room = store.get_room("#build")
        assert room is not None
        m = store.find_member(room.id, "claude-1")
        assert m is not None
        return store.insert_message(
            room.id,
            sender_name="claude-1",
            sender_kind="agent",
            via="mcp",
            text=text,
            sender_membership_id=m.membership_id,
            sender_harness="test",
            mentions=mentions,
        ).id

    return int(b.on_loop(run))


def waiting(br: Browser) -> list[tuple[str, str]]:
    r = br.get("/api/home")
    assert r.status_code == 200, r.text
    return [(c["kind"], c["room"]) for c in r.json()["waiting"]]


def notices(br: Browser, slug: str) -> list[str]:
    msgs = br.get(f"/api/rooms/{slug}/messages").json()["messages"]
    return [m["text"] for m in msgs if m["kind"] == "notice"]


def test_the_creator_is_a_member_and_anyone_else_joins(team: tuple[InProcBroker, Browser, Browser]) -> None:
    """A room's creator is its first member; bob sees the public room but is not a member until he joins
    it, which says so in the room. Fails if a public room is everyone's again, if bob can't join, or if
    joining twice is taken as news."""
    _b, alice, bob = team
    assert rooms(alice)["#build"]["member"] is True
    got = rooms(bob)["#build"]
    assert (got["member"], got["people"], got["joined_with"]) == (False, ["alice"], None)
    r = people(bob, "build", "add", "bob")
    assert (r.status_code, r.json()["text"]) == (200, "you joined #build")
    assert r.json()["room"]["people"] == ["alice", "bob"]
    assert rooms(bob)["#build"]["member"] is True
    assert "bob joined #build" in notices(alice, "build")
    r = people(bob, "build", "add", "bob")
    assert (r.status_code, r.json()["message"]) == (409, "you're in #build already")


def test_a_member_adds_someone_and_a_person_only_leaves_themself(
    team: tuple[InProcBroker, Browser, Browser],
) -> None:
    """Only a member adds another person; nobody takes someone else out of a public room; leaving says so
    in the room. Fails if a stranger to the room can sign others up, or one person can remove another."""
    _b, alice, bob = team
    alice.post("/api/rooms", {"name": "#ops"})
    assert people(alice, "ops", "remove", "alice").status_code == 200  # alice leaves her own room
    r = people(bob, "ops", "add", "alice")
    assert (r.status_code, r.json()["message"]) == (400, "join #ops first to add someone to it")
    r = people(alice, "build", "add", "bob")
    assert (r.status_code, r.json()["text"]) == (200, "added bob")
    assert "alice added bob to #build" in notices(bob, "build")
    r = people(bob, "build", "remove", "alice")
    assert (r.status_code, r.json()["message"]) == (
        400,
        "people leave #build themselves: you can't take alice out of it",
    )
    r = people(bob, "build", "remove", "nobody")
    assert (r.status_code, r.json()["message"]) == (404, "no such person: nobody")
    r = people(bob, "build", "remove", "bob")
    assert (r.status_code, r.json()["text"]) == (200, "you left #build")
    assert rooms(bob)["#build"]["member"] is False
    assert "bob left #build" in notices(alice, "build")
    r = people(bob, "build", "remove", "bob")
    assert (r.status_code, r.json()["message"]) == (404, "bob isn't in #build")


def test_at_humans_and_questions_wait_only_on_a_rooms_members(
    team: tuple[InProcBroker, Browser, Browser],
) -> None:
    """@humans in #build waits on alice, its member, and not on bob, who can still read it; once bob
    joins, the next one waits on him too, but what was said before he joined does not. Fails if
    "waiting on you" (and so the notifications built on it) still reaches everyone on the broker, or if
    joining brings back old questions."""
    b, alice, bob = team
    first = agent_says(b, "@humans deploy at noon?", ["humans"])
    agent_says(b, "@bob are you there?", ["bob"])
    assert waiting(alice) == [("question", "#build")]
    assert waiting(bob) == []
    texts = [m["id"] for m in bob.get("/api/rooms/build/messages").json()["messages"]]
    assert first in texts  # he can read it: a public room is everyone's to see (§44)
    assert people(bob, "build", "add", "bob").status_code == 200
    assert waiting(bob) == []  # history, not news
    agent_says(b, "@humans one more thing", ["humans"])
    assert waiting(bob) == [("question", "#build")]


def test_leaving_clears_unread_and_home_and_the_agents_stay(
    team: tuple[InProcBroker, Browser, Browser],
) -> None:
    """Leaving moves the seen mark to the room's newest message (no unread left), takes the room's cards
    off Home, and keeps every agent in the room. Fails if leaving keeps pinging, leaves an unread count
    behind, or takes the person's agents out of a public room."""
    b, alice, _bob = team
    agent_says(b, "@alice which branch?", ["alice"])
    assert waiting(alice) == [("question", "#build")]
    assert people(alice, "build", "remove", "alice").status_code == 200
    assert waiting(alice) == []
    got = rooms(alice)["#build"]
    assert got["member"] is False and got["seen"] == got["last_id"]
    assert [m["name"] for m in alice.get("/api/rooms/build/members").json()["members"]] == ["claude-1"]


def test_an_agent_joining_makes_its_person_a_member(team: tuple[InProcBroker, Browser, Browser]) -> None:
    """bob's agent (a Codex session on bob's device) joins #build, so bob is a member, and his room list
    says which agent did it until he opens the room. alice left #build first, so bob is its only member,
    and the agent's join text still names alice and bob as its users: who agents answer to has not moved.
    Fails if an agent's join leaves its person out, if the note never goes away, or if the agent answers
    only to the room's members (then its only user would be bob)."""
    b, alice, bob = team
    bobs = Codex(b, make_token(b, "bobs-laptop", b.state.store.person_named("bob").id))
    bobs.c = httpx.Client(base_url=f"http://127.0.0.1:{b.port}", timeout=30.0, headers={"host": HOST})
    try:
        assert bobs.start().status_code == 200
        assert people(alice, "build", "remove", "alice").status_code == 200
        joined = bobs.call("join", THREAD_B, room="#build", screen_name="codex-1")
        assert joined["ok"], joined
        assert rooms(alice)["#build"]["people"] == ["bob"]
        assert "Your users are alice and bob (kind=human)" in joined["text"]
        got = rooms(bob)["#build"]
        assert (got["member"], got["joined_with"]) == (True, "codex-1")
        assert waiting(bob) == []  # what was there before is history
        assert bob.post("/api/rooms/build/seen", {"id": got["last_id"]}).status_code == 200
        assert rooms(bob)["#build"]["joined_with"] is None
        # leaving keeps the agent in the room; joining again by hand is not "with" anything
        assert people(bob, "build", "remove", "bob").status_code == 200
        names = [m["name"] for m in alice.get("/api/rooms/build/members").json()["members"]]
        assert "codex-1" in names
        assert people(bob, "build", "add", "bob").status_code == 200
        assert rooms(bob)["#build"]["joined_with"] is None
    finally:
        bobs.c.close()


def test_a_late_seen_request_keeps_the_joined_with_note(team: tuple[InProcBroker, Browser, Browser]) -> None:
    """The note "joined with codex-1" goes when bob reads past where his agent's join started him, not on
    any seen request: one his page sent for what it showed before the join, arriving late, leaves it, and
    one that reaches the agent's join line clears it. Fails if a stale seen POST erases the note before
    bob has seen it."""
    b, _alice, bob = team
    bobs = Codex(b, make_token(b, "bobs-laptop", b.state.store.person_named("bob").id))
    bobs.c = httpx.Client(base_url=f"http://127.0.0.1:{b.port}", timeout=30.0, headers={"host": HOST})
    try:
        assert bobs.start().status_code == 200
        assert bobs.call("join", THREAD_B, room="#build", screen_name="codex-1")["ok"]
        got = rooms(bob)["#build"]
        assert got["seen"] < got["last_id"]  # the join line is after where he starts
        assert bob.post("/api/rooms/build/seen", {"id": got["seen"]}).status_code == 200
        assert rooms(bob)["#build"]["joined_with"] == "codex-1"
        assert bob.post("/api/rooms/build/seen", {"id": got["last_id"]}).status_code == 200
        assert rooms(bob)["#build"]["joined_with"] is None
    finally:
        bobs.c.close()


def boom(*_args: Any, **_kw: Any) -> Any:
    """A write that fails the way a full disk or a crash between two commits would."""
    raise sqlite3.OperationalError("disk I/O error")


def test_an_agents_membership_and_its_persons_commit_together(
    team: tuple[InProcBroker, Browser, Browser], monkeypatch: pytest.MonkeyPatch
) -> None:
    """bob's agent joining #build and bob becoming a member are one write: when bob's half fails, the
    agent is not left in the room either, so the join that is tried again is a new one and makes bob a
    member. Fails if the agent's membership commits on its own: the retry is then the same session
    joining again, which never adds its person."""
    b, alice, bob = team
    store = b.state.store
    bobs = Codex(b, make_token(b, "bobs-laptop", store.person_named("bob").id))
    bobs.c = httpx.Client(base_url=f"http://127.0.0.1:{b.port}", timeout=30.0, headers={"host": HOST})
    try:
        assert bobs.start().status_code == 200
        with monkeypatch.context() as mp:
            mp.setattr(store, "room_person_add", boom)
            failed = bobs.raw("join", THREAD_B, room="#build", screen_name="codex-1")
            assert '"ok": true' not in failed.text and '\\"ok\\": true' not in failed.text, failed.text
        names = [m["name"] for m in alice.get("/api/rooms/build/members").json()["members"]]
        assert "codex-1" not in names
        joined = bobs.call("join", THREAD_B, room="#build", screen_name="codex-1")
        assert joined["ok"], joined
        assert rooms(bob)["#build"]["member"] is True
    finally:
        bobs.c.close()


def test_joining_a_public_room_and_its_starting_mark_commit_together(
    team: tuple[InProcBroker, Browser, Browser], monkeypatch: pytest.MonkeyPatch
) -> None:
    """bob joining #build, its notice and his seen mark moving to the newest message are one write: when
    the mark fails, he is not a member and nothing was said, and joining again works and starts him from
    now, so the @humans from before he joined does not wait on him. Fails if the membership commits
    without its mark: bob is then a member who sees old questions, and the retry is "you're in already"."""
    b, alice, bob = team
    agent_says(b, "@humans deploy at noon?", ["humans"])
    svc = b.state.service
    bob_id = b.state.store.person_named("bob").id
    with monkeypatch.context() as mp:
        mp.setattr(b.state.store, "home_mark_seen", boom)
        with pytest.raises(sqlite3.OperationalError):
            b.on_loop(lambda: svc.add_room_person(svc.room("#build"), "bob", bob_id))
    assert rooms(bob)["#build"]["member"] is False
    assert "bob joined #build" not in notices(alice, "build")
    r = people(bob, "build", "add", "bob")
    assert (r.status_code, r.json()["text"]) == (200, "you joined #build")
    assert waiting(bob) == []
    assert notices(alice, "build").count("bob joined #build") == 1


def test_a_private_room_keeps_its_people(team: tuple[InProcBroker, Browser, Browser]) -> None:
    """A private room's members are its people, as before (§44): bob is in his, alice can't see it, and
    the last person still can't leave. Fails if the public rules leak into a private room."""
    _b, alice, bob = team
    assert bob.post("/api/rooms", {"name": "#secret", "private": True}).status_code == 200
    assert rooms(bob)["#secret"]["member"] is True and "#secret" not in rooms(alice)
    r = people(bob, "secret", "remove", "bob")
    assert r.status_code == 400 and r.json()["message"].startswith("the last person can't leave #secret")


def test_removing_someone_takes_them_out_of_public_rooms_quietly(
    team: tuple[InProcBroker, Browser, Browser],
) -> None:
    """Someone removed from switchboard is no longer a member of any room, and a public room is not told
    (only a private room says a person left). Fails if a removed person's rows stay behind."""
    b, alice, bob = team
    assert people(bob, "build", "add", "bob").status_code == 200
    bob_id = b.state.store.person_named("bob").id
    before = notices(alice, "build")
    assert alice.post(f"/api/people/{bob_id}/remove", {}).status_code == 200
    assert b.state.store.rooms_of_person(bob_id) == []
    assert notices(alice, "build") == before


def test_with_one_person_every_public_room_is_theirs(broker: InProcBroker) -> None:
    """A desktop broker has one person: every public room is theirs, even one with no row for them, and
    there is nothing to join or leave. Fails if a desktop sidebar would empty, or Leave were offered."""
    web = broker.web_client()
    try:
        r = web.post("/api/rooms", json={"name": "#solo"}, headers=broker.write_headers())
        assert r.status_code == 200

        def bare() -> None:  # a room made before members counted, that nobody had touched
            broker.state.store.create_room("#untouched", "alice", 60, 6)

        broker.on_loop(bare)
        got = web.get("/api/rooms").json()["rooms"]
        assert {x["name"]: x["member"] for x in got} == {"#solo": True, "#untouched": True}
        assert all(x["joined_with"] is None for x in got)
        r = web.post(
            "/api/rooms/solo/people",
            json={"action": "remove", "name": "alice"},
            headers=broker.write_headers(),
        )
        assert (r.status_code, r.json()["message"]) == (
            400,
            "#solo is everyone's: there is only one person on this switchboard",
        )
    finally:
        web.close()


def test_what_the_one_person_used_stays_theirs_when_a_second_arrives() -> None:
    """With one person on the broker every public room is theirs, so opening one or posting in one is
    recorded as membership, quietly, the way day one counts it: the day bob is added, alice is still in
    #seen (she opened it) and #said (she posted there), and not in #idle, which she never touched, nor in
    #gone, which she left while someone else was here, though she opened it since. Fails if what the one
    person used is lost when a second person arrives, or if recording it undoes a Leave."""
    b = hosted_broker()
    alice = set_up(b)
    bob: Browser | None = None
    try:

        def old_rooms() -> None:  # rooms from before members counted, that nobody had used by day one
            store = b.state.store
            for n in ("#seen", "#said", "#idle", "#gone"):
                room = store.create_room(n, "alice", 60, 6)
                store.insert_message(
                    room.id,
                    sender_name="switchboard",
                    sender_kind="system",
                    via="system",
                    kind="notice",
                    text="made before members counted",
                )
            gone = store.get_room("#gone")
            store.add_event("room_people", room_id=gone.id, data={"what": "leave", "person": 0, "by": 0})

        b.on_loop(old_rooms)
        assert all(x["member"] for x in rooms(alice).values())  # alone, every public room is hers
        for slug in ("seen", "gone"):
            last = rooms(alice)[f"#{slug}"]["last_id"]
            assert alice.post(f"/api/rooms/{slug}/seen", {"id": last}).status_code == 200
        assert alice.post("/api/rooms/said/say", {"text": "hello"}).status_code == 200
        bob = join_as_bob(b, alice)
        got = {n: x["member"] for n, x in rooms(alice).items()}
        assert got == {"#seen": True, "#said": True, "#idle": False, "#gone": False}
    finally:
        if bob is not None:
            bob.close()
        alice.close()
        stop(b)


OLD_DB = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"


def test_an_upgraded_broker_fills_the_members_once_at_its_start(tmp_home: Path) -> None:
    """Day one is the broker's own start: a broker started on the 0.37.2 database (schema 28), with a post
    of bob's added in #bench, migrates it and makes each person a member of the public rooms they used:
    the owner #build, #bench (her posts) and the closed #old (her agent), bob #bench, and #secret keeps its
    own people. bob then leaves #bench, and a restart on the same home does not put him back. Fails if the
    start never fills the members (every sidebar on an upgraded broker would empty), or fills them again
    at every start."""
    paths = Paths.from_home(tmp_home)
    paths.ensure()
    old = sqlite3.connect(paths.db)
    try:
        old.executescript(OLD_DB.read_text())
        old.execute(
            "INSERT INTO messages(room_id, ts, sender_name, sender_kind, via, kind, text, sender_person_id)"
            " VALUES(2, 1790000016.0, 'bob', 'human', 'web', 'chat', 'on it', 1)"
        )
        old.commit()
    finally:
        old.close()
    b = InProcBroker(tmp_home).start()
    try:
        store = b.state.store
        assert {r: sorted(b.on_loop(store.room_people, r)) for r in (1, 2, 3, 4)} == {
            1: [0],
            2: [0, 1],
            3: [0],
            4: [0, 1],
        }
        assert b.on_loop(store.room_person_remove, 2, 1)
        b.restart()
        store = b.state.store
        assert b.on_loop(store.room_people, 2) == [0]
        assert b.on_loop(store.meta_get, Store.DAY_ONE_KEY) is not None
    finally:
        b.stop()


def test_a_room_with_members_can_still_be_deleted(team: tuple[InProcBroker, Browser, Browser]) -> None:
    """Deleting a room removes its members' rows with it (§28.6), and says so: the plan, the result and
    the ``room_delete`` record count them, as they count its agents' memberships. A room id is reused, and
    a row left behind would make someone a member of the next room, or fail the delete's foreign key
    check. Fails if the rows stay, or if the plan and the record leave them out."""
    b, alice, bob = team
    assert people(bob, "build", "add", "bob").status_code == 200
    room = b.state.store.get_room("#build")
    assert room is not None

    def delete() -> tuple[dict[str, int], dict[str, int]]:
        store = b.state.store
        for m in store.room_memberships(room.id):
            store.end_membership(m.id, "kick")
        plan = store.room_delete_counts(room.id)
        return plan, store.delete_room(
            room.id,
            name=room.name,
            created_at=room.created_at,
            expect_counts=db.row_counts(store.con, db.TABLES),
            event={},
        )

    plan, removed = b.on_loop(delete)
    assert plan["room_people"] == removed["room_people"] == 2
    assert b.state.store.room_people(room.id) == []
    record = b.state.store.con.execute(
        "SELECT data FROM events WHERE kind='room_delete' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert '"room_people": 2' in record[0]
