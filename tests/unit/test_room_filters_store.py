"""The two reads behind a room's filters, ``Store.room_addressed`` and ``Store.room_with_files`` (DESIGN.md §64,
#914).

What these catch: a Mentions query that finds other rows than ``home.addressed_to``, the one test of
"addressed to me" (it would show a line the unread count does not count, or miss one it does); a query that
reads another room; and either query walking the room's whole history instead of its own index."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from switchboard import db
from switchboard.broker import home
from switchboard.store import Store

MAX = 2**63 - 1


@pytest.fixture
def st(tmp_path: Path) -> Store:
    return Store(db.open_db(tmp_path / "f.db"))


def say(st: Store, room: int, name: str, sender: str, **kw: object) -> int:
    via = {"human": "web", "agent": "mcp", "meeting": "meeting", "system": "system"}[sender]
    return st.insert_message(room, sender_name=name, sender_kind=sender, via=via, text="x", **kw).id  # type: ignore[arg-type]


def plan(st: Store, sql: str, args: dict[str, object]) -> str:
    return " | ".join(r[3] for r in st.con.execute("EXPLAIN QUERY PLAN " + sql, args))


def test_mentions_finds_exactly_what_home_addressed_to_accepts(st: Store) -> None:
    """Over 600 random lines in two rooms (people, agents, a call and notices; mentions of alice, @humans,
    someone else or nobody; replies to alice, to bob or to an agent; alice's own lines naming herself), the
    query returns, for alice, exactly the lines of her room that ``home.addressed_to`` accepts, newest first,
    and none of the other room. Fails if the SQL and the one definition drift apart."""
    rng = random.Random(914)
    r1 = st.create_room("#build", "alice", 60, 0).id
    r2 = st.create_room("#other", "alice", 60, 0).id
    ids: dict[int, list[int]] = {r1: [], r2: []}
    senders = [("alice", "human"), ("Bob", "human"), ("claude-1", "agent"), ("zoom", "meeting")]
    for _ in range(600):
        room = rng.choice((r1, r2))
        name, kind = rng.choice(senders)
        mentions = rng.choice(([], ["alice"], ["humans"], ["codex-1"], ["alice", "codex-1"]))
        reply = rng.choice(ids[room]) if ids[room] and rng.random() < 0.4 else None
        notice = rng.random() < 0.05
        ids[room].append(
            say(
                st,
                room,
                "switchboard" if notice else name,
                "system" if notice else kind,
                kind="notice" if notice else "chat",
                mentions=mentions,
                reply_to=reply,
            )
        )
    got = st.room_addressed(r1, "Alice", MAX, 1000)
    assert [m.room_id for m, _ in got] == [r1] * len(got)
    want = []
    for m in reversed(st.history(r1, None, 1000)):
        parent = st.get_message(m.reply_to) if m.reply_to else None
        from_me = parent is not None and parent.sender_kind == "human" and parent.sender_name == "alice"
        if m.kind == "chat" and home.addressed_to(m, "alice", from_me):
            want.append(m.id)
    assert [m.id for m, _ in got] == want and len(want) > 50
    assert all(home.addressed_to(m, "alice", to_me) for m, to_me in got)


def test_files_finds_each_message_with_a_stored_file_once(st: Store) -> None:
    """Two stored files on one message give it once; a pending file, a file of no message and another room's
    file give nothing; ``before`` pages. Fails if the join repeats a message or reads another room."""
    r1 = st.create_room("#build", "alice", 60, 0).id
    r2 = st.create_room("#other", "alice", 60, 0).id
    a = say(st, r1, "alice", "human")
    b = say(st, r1, "zoom", "meeting")
    c = say(st, r1, "alice", "human")
    d = say(st, r2, "alice", "human")
    for n, (room, mid, state) in enumerate(
        [(r1, a, "stored"), (r1, a, "stored"), (r1, b, "stored"), (r1, c, "pending"), (r1, None, "stored"),
         (r2, d, "stored")]
    ):
        st.file_insert(
            file_id=f"{n:032x}", room_id=room, name="f.txt", size=1, sha256=None, content_type="",
            uploader_name="alice", uploader_kind="human", state=state, created_at=1.0, stored_at=1.0,
            message_id=mid,
        )  # fmt: skip
    assert [m.id for m in st.room_with_files(r1, MAX, 10)] == [b, a]
    assert [m.id for m in st.room_with_files(r1, b, 10)] == [a]
    assert [m.id for m in st.room_with_files(r1, MAX, 1)] == [b]


def test_each_query_walks_its_own_index_not_the_rooms_history(st: Store) -> None:
    """Mentions walks ``messages_addressed`` (the partial index of schema 59) and Files starts from the
    room's files, so neither reads every message of a long room with few matches. Fails if the Mentions
    query loses the terms that let SQLite use the partial index, or the index is missing."""
    r = st.create_room("#build", "alice", 60, 0).id
    calls: list[str] = []
    st.con.set_trace_callback(calls.append)
    st.room_addressed(r, "alice", MAX, 5)
    st.room_with_files(r, MAX, 5)
    st.con.set_trace_callback(None)
    args = {"room": r, "me": "alice", "mention": '"alice"', "before": MAX, "n": 5}
    mentions = plan(st, calls[0], args)
    files = plan(st, calls[1], args)
    assert "USING INDEX messages_addressed" in mentions, mentions
    assert "messages_room_id" not in mentions and "SCAN m" not in mentions, mentions
    assert files.startswith("SEARCH f USING INDEX files_room"), files
    assert "SCAN" not in files, files
