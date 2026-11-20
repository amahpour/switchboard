"""A room filtered to its Mentions or its Files over the web API (#914, DESIGN.md §64):
``GET /api/rooms/{slug}/filter?show=mentions|files&before=&limit=``.

On a hosted broker with the admin and bob, as the search tests set it up. What these catch: a filter that
lets a person read a room they cannot see; a Mentions that uses its own idea of "addressed to me" instead
of the home page's (``home.addressed_to``); a Files that counts a file still uploading, or misses a call's
snapshot; a page that stops at the newest messages, so older history is never reached; and a filter value
the broker does not know being read as All."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

import pytest
from conftest import FakeClock, InProcBroker
from test_passkeys import hosted_broker, stop
from test_people import Browser, join_as_bob, set_up

from switchboard.broker import room_filters
from switchboard.models import session_key


@pytest.fixture
def team() -> Iterator[tuple[InProcBroker, Browser, Browser, str]]:
    clock = FakeClock()
    b = hosted_broker(clock)
    admin = set_up(b)
    bob = join_as_bob(b, admin)
    try:
        assert admin.post("/api/rooms", {"name": "#build"}).status_code == 200
        me = admin.get("/api/me").json()["human"]
        yield b, admin, bob, me
    finally:
        bob.close()
        admin.close()
        stop(b)


VIA = {"human": "web", "agent": "mcp", "meeting": "meeting", "system": "system"}


def post(
    b: InProcBroker, room: str, text: str, *, name: str = "claude-1", kind: str = "agent", **kw: Any
) -> int:
    """A message as the broker stores one: an agent's (a member of the room), a person's, a call's or a
    system notice (``kind="system"``, kept as a notice)."""

    def run() -> int:
        store = b.state.store
        r = store.get_room(room)
        assert r is not None
        if kind == "agent":
            p = store.upsert_participant("test", session_key("test", "", name), host="")
            if store.find_member(r.id, name) is None:
                store.create_membership(r.id, p.id, name)
        return store.insert_message(
            r.id,
            sender_name=name,
            sender_kind=kind,
            via=VIA[kind],
            kind="notice" if kind == "system" else "chat",
            text=text,
            sender_harness="claude" if kind == "agent" else None,
            **kw,
        ).id

    return int(b.on_loop(run))


def filtered(who: Browser, slug: str = "build", **params: Any) -> Any:
    return who.get(f"/api/rooms/{slug}/filter?" + urlencode(params))


def page(who: Browser, slug: str = "build", **params: Any) -> dict[str, Any]:
    r = filtered(who, slug, **params)
    assert r.status_code == 200, r.text
    return r.json()


def texts(got: dict[str, Any]) -> list[str]:
    return [m["text"] for m in got["messages"]]


def attach(b: InProcBroker, room: str, message_id: int | None, *, state: str = "stored", by: str = "human") -> str:
    """A file row bound to ``message_id``, as an upload (or a call's snapshot, ``by="meeting"``) leaves it."""

    def run() -> str:
        store = b.state.store
        r = store.get_room(room)
        assert r is not None
        fid = secrets.token_hex(16)
        store.file_insert(
            file_id=fid,
            room_id=r.id,
            name="notes.txt" if by == "human" else "snapshot.png",
            size=3 if state == "stored" else None,
            sha256="a" * 64 if state == "stored" else None,
            content_type="text/plain",
            uploader_name="alice" if by == "human" else "zoom",
            uploader_kind=by,
            state=state,
            created_at=1.0,
            stored_at=2.0 if state == "stored" else None,
            message_id=message_id,
        )
        return fid

    return str(b.on_loop(run))


def test_a_signed_out_request_is_refused(team: tuple[InProcBroker, Browser, Browser, str]) -> None:
    """Fails if the route answers without a session."""
    b, *_ = team
    assert Browser(b).get("/api/rooms/build/filter?show=mentions").status_code == 401


def test_mentions_are_the_messages_the_home_page_calls_addressed_to_me(
    team: tuple[InProcBroker, Browser, Browser, str],
) -> None:
    """An agent's @mention of the viewer, @humans, another person naming them, and an agent's reply to
    something they said are in; agent chatter, a mention of someone else, the viewer's own message (even one
    naming themself), another person's reply to them and a notice are not. Fails if Mentions has a
    definition of its own, or reads only the newest lines."""
    b, admin, _bob, me = team
    mine = post(b, "#build", "what broke the build?", name=me, kind="human")
    post(b, "#build", f"@{me} the lockfile", mentions=[me])
    post(b, "#build", "@humans please review", mentions=["humans"])
    post(b, "#build", f"@{me} have a look", name="bob", kind="human", mentions=[me])
    post(b, "#build", "it was the cache", reply_to=mine)
    post(b, "#build", "agreed", name="bob", kind="human", reply_to=mine)
    post(b, "#build", "@codex-1 your turn", mentions=["codex-1"])
    post(b, "#build", "plain chatter")
    post(b, "#build", f"note to self @{me}", name=me, kind="human", mentions=[me])
    post(b, "#build", f"@{me} a notice", name="switchboard", kind="system", mentions=[me])
    got = page(admin, show="mentions")
    assert got["show"] == "mentions" and got["room"] == "#build" and got["next"] is None
    assert texts(got) == [f"@{me} the lockfile", "@humans please review", f"@{me} have a look", "it was the cache"]


def test_files_are_the_messages_with_a_stored_file_and_a_call_snapshot_counts(
    team: tuple[InProcBroker, Browser, Browser, str],
) -> None:
    """A message with a stored attachment and one with a call's snapshot are in, each with its ``files`` as
    the log draws them; a message whose file is still uploading, a message with no file and a file bound to
    no message are not. Fails if Files counts pending uploads, skips a snapshot, or repeats a message that
    carries two files."""
    b, admin, *_ = team
    a = post(b, "#build", "the log, attached", name="alice", kind="human")
    fa = attach(b, "#build", a)
    attach(b, "#build", a)
    s = post(b, "#build", "[snapshot] the shared screen", name="zoom", kind="meeting")
    attach(b, "#build", s, by="meeting")
    p = post(b, "#build", "still uploading", name="alice", kind="human")
    attach(b, "#build", p, state="pending")
    post(b, "#build", "no file here")
    attach(b, "#build", None)
    got = page(admin, show="files")
    assert texts(got) == ["the log, attached", "[snapshot] the shared screen"]
    assert fa in [f["id"] for f in got["messages"][0]["files"]] and len(got["messages"][0]["files"]) == 2
    assert got["messages"][1]["files"][0]["name"] == "snapshot.png"


def test_each_filter_pages_back_past_the_newest_history(
    team: tuple[InProcBroker, Browser, Browser, str],
) -> None:
    """With more mentions than a page, under a much larger room of chatter, the pages come newest first,
    each oldest first inside, and following ``next`` reaches the very first one with nothing repeated or
    skipped. Fails if the filter reads only the loaded page of history, or its cursor drops a message."""
    b, admin, _bob, me = team
    want = []
    for i in range(7):
        for j in range(30):
            post(b, "#build", f"chatter {i}.{j}")
        want.append(f"@{me} number {i}")
        mid = post(b, "#build", want[-1], mentions=[me])
        attach(b, "#build", mid)
    for show in ("mentions", "files"):
        seen: list[str] = []
        before: Any = ""
        pages = 0
        while True:
            got = page(admin, show=show, limit=3, before=before)
            assert len(got["messages"]) <= 3
            ids = [m["id"] for m in got["messages"]]
            assert ids == sorted(ids)
            seen = texts(got) + seen
            pages += 1
            if got["next"] is None:
                break
            before = got["next"]
        assert seen == want and pages == 3, show


def test_a_person_who_cannot_see_the_room_gets_nothing(
    team: tuple[InProcBroker, Browser, Browser, str],
) -> None:
    """Bob is not in the private #secret: each filter is "no such room", in the words of a room that does not
    exist, so neither a line nor the room's name is confirmed. Added, he reads it; removed, he is refused
    again. Fails if the route checks visibility its own way, or not at all."""
    b, admin, bob, _me = team
    assert admin.post("/api/rooms", {"name": "#secret", "private": True}).status_code == 200
    mid = post(b, "#secret", "@bob the launch codes", mentions=["bob"])
    attach(b, "#secret", mid)
    for show in ("mentions", "files"):
        r = filtered(bob, "secret", show=show)
        ghost = filtered(bob, "nowhere", show=show)
        assert r.status_code == ghost.status_code == 404
        assert r.json()["message"] == ghost.json()["message"].replace("nowhere", "secret")
        assert "launch" not in r.text
    assert admin.post("/api/rooms/secret/people", {"action": "add", "name": "bob"}).status_code == 200
    assert texts(page(bob, "secret", show="mentions")) == ["@bob the launch codes"]
    assert texts(page(bob, "secret", show="files")) == ["@bob the launch codes"]
    assert bob.post("/api/rooms/secret/people", {"action": "remove", "name": "bob"}).status_code == 200
    assert filtered(bob, "secret", show="files").status_code == 404


def test_what_the_request_names_is_cut_clamped_and_an_unknown_filter_refused(
    team: tuple[InProcBroker, Browser, Browser, str],
) -> None:
    """An unknown, empty or missing filter is a 400 that names the two it knows (never the whole room); a
    cursor that is not a whole number is a 400; a huge cursor or page size is clamped, not an error, and a
    page is never larger than ``MAX_PAGE``. Fails if an unknown value falls back to All, or a long
    parameter reaches ``int`` uncut."""
    b, admin, _bob, me = team
    post(b, "#build", "plain chatter")
    for show in ("all", "threads", "", "x" * 5000):
        r = filtered(admin, show=show)
        assert r.status_code == 400 and "mentions or files" in r.json()["message"], show
    assert admin.get("/api/rooms/build/filter").status_code == 400
    for before in ("-1", "1.5", "abc", "１２"):
        assert filtered(admin, show="files", before=before).status_code == 400, before
    for i in range(room_filters.MAX_PAGE + 5):
        post(b, "#build", f"@{me} {i}", mentions=[me])
    got = page(admin, show="Mentions", before="9" * 5000, limit="9" * 5000)
    assert len(got["messages"]) == room_filters.MAX_PAGE and got["next"] is not None
    assert len(page(admin, show="mentions", limit=0)["messages"]) == 1
