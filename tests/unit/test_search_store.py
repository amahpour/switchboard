"""The search index and ``Store.search`` (DESIGN.md §58, #724).

The index is kept by triggers, so these insert and delete through the store and the SQL a room's deletion
uses, and read through the one search method. What they catch: a notice or system line in the index, an
index that drifts from the messages (a delete that leaves a hit), a filter that is ignored, a cursor that
repeats or skips, and a search that reads a room it was not given."""

from __future__ import annotations

from pathlib import Path

import pytest

from switchboard import db, search
from switchboard.store import Store

NONCE = "abc123"


@pytest.fixture
def st(tmp_path: Path) -> Store:
    return Store(db.open_db(tmp_path / "s.db"))


def say(
    st: Store,
    room: int,
    text: str,
    *,
    name: str = "alice",
    kind: str = "human",
    ts: float | None = None,
    msg_kind: str = "chat",
    via: str = "web",
    harness: str | None = None,
) -> int:
    m = st.insert_message(
        room, sender_name=name, sender_kind=kind, via=via, text=text, kind=msg_kind, sender_harness=harness
    )
    if ts is not None:
        st.con.execute("UPDATE messages SET ts=? WHERE id=?", (ts, m.id))
    return m.id


def find(st: Store, rooms: list[int], q: str, **kw: object) -> list[int]:
    got = st.search(rooms, search.match_query(q) or "", nonce=NONCE, **kw)  # type: ignore[arg-type]
    return [h["id"] for h in got["hits"]]


def test_chat_lines_are_indexed_and_notices_and_system_lines_are_not(st: Store) -> None:
    """A person's, an agent's and a call's lines are found; a notice, a join line and a system chat line
    are not. Fails if the index covers everything in ``messages`` (a notice would be a result)."""
    r = st.create_room("#build", "alice", 60, 0).id
    a = say(st, r, "needle from a person")
    b = say(st, r, "needle from an agent", name="claude-1", kind="agent", via="mcp", harness="claude")
    c = say(st, r, "needle from a call", name="bob", kind="meeting", via="meeting")
    say(st, r, "needle in a notice", name="switchboard", kind="system", via="system", msg_kind="notice")
    say(st, r, "needle joined", name="claude-1", kind="agent", via="mcp", msg_kind="join")
    say(st, r, "needle from system chat", name="switchboard", kind="system", via="system")
    assert find(st, [r], "needle") == [c, b, a]


def test_the_index_follows_a_room_deletion_and_an_edit(st: Store) -> None:
    """A deleted message stops being a hit (the delete trigger), and an edit moves the hit to the new words
    (the update trigger; the store never edits a message, so the SQL is run by hand). Fails if the index
    keeps a deleted message, which a later 'delete' would also corrupt."""
    r = st.create_room("#build", "alice", 60, 0).id
    m = say(st, r, "alpha beta")
    st.con.execute("UPDATE messages SET text='gamma delta' WHERE id=?", (m,))
    assert find(st, [r], "alpha") == [] and find(st, [r], "gamma") == [m]
    st.con.execute("UPDATE messages SET kind='notice' WHERE id=?", (m,))
    assert find(st, [r], "gamma") == []
    st.con.execute("UPDATE messages SET kind='chat' WHERE id=?", (m,))
    assert find(st, [r], "gamma") == [m]
    st.con.execute("DELETE FROM messages WHERE id=?", (m,))
    assert find(st, [r], "gamma") == []
    # the search joins the messages, so a stale entry would hide there: ask the index itself (it holds one
    # document per searchable line, and its own check compares it with the lines)
    assert st.con.execute("SELECT COUNT(*) FROM messages_fts_docsize").fetchone()[0] == 0
    st.con.execute("INSERT INTO messages_fts(messages_fts) VALUES('integrity-check')")


def test_only_the_given_rooms_are_read(st: Store) -> None:
    """The rooms are the caller's to choose, and nothing outside them is returned or counted. Fails if the
    search reads every room and filters afterwards, or if the count includes another room's lines."""
    a = st.create_room("#a", "alice", 60, 0).id
    b = st.create_room("#b", "alice", 60, 0).id
    ia, ib = say(st, a, "shared word"), say(st, b, "shared word")
    assert find(st, [a], "shared") == [ia]
    assert find(st, [a, b], "shared") == [ib, ia]
    assert find(st, [], "shared") == []
    got = st.search([a], search.match_query("shared") or "", nonce=NONCE, count=True)
    assert got["total"] == 1


def test_filters_narrow_by_sender_time_and_calls(st: Store) -> None:
    """Sender (any case), a time window (since inclusive, until exclusive) and calls off each narrow the
    hits and combine. Fails if a filter is ignored or the bounds are off by one."""
    r = st.create_room("#build", "alice", 60, 0).id
    a = say(st, r, "deploy now", ts=100.0)
    b = say(st, r, "deploy later", name="codex-1", kind="agent", via="mcp", harness="codex", ts=200.0)
    c = say(st, r, "deploy on the call", name="bob", kind="meeting", via="meeting", ts=300.0)
    assert find(st, [r], "deploy", sender="CODEX-1") == [b]
    assert find(st, [r], "deploy", since=200.0) == [c, b]
    assert find(st, [r], "deploy", until=200.0) == [a]
    assert find(st, [r], "deploy", calls=False) == [b, a]
    assert find(st, [r], "deploy", since=150.0, until=300.0, calls=False) == [b]
    assert find(st, [r], "deploy", sender="nobody") == []


def test_pages_are_newest_first_with_a_cursor_that_neither_repeats_nor_skips(st: Store) -> None:
    """120 matches come as 50, 50 and 20, newest first, each page starting after the last id of the one
    before; ``more`` is false on the last. Fails if the cursor is inclusive or the page is not cut at 50."""
    r = st.create_room("#build", "alice", 60, 0).id
    ids = [say(st, r, f"page word {i}") for i in range(120)]
    m = search.match_query("page word") or ""
    seen: list[int] = []
    before = None
    sizes = []
    while True:
        got = st.search([r], m, nonce=NONCE, before=before, limit=999)
        sizes.append(len(got["hits"]))
        seen += [h["id"] for h in got["hits"]]
        if not got["more"]:
            break
        before = got["hits"][-1]["id"]
    assert sizes == [50, 50, 20]
    assert seen == ids[::-1]


def test_the_count_is_capped(st: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """A common word does not make the count walk the whole index: past the cap it says so."""
    r = st.create_room("#build", "alice", 60, 0).id
    for i in range(7):
        say(st, r, f"cap word {i}")
    monkeypatch.setattr(Store, "SEARCH_COUNT_CAP", 5)
    got = st.search([r], search.match_query("cap") or "", nonce=NONCE, count=True)
    assert (got["total"], got["capped"]) == (5, True)


def test_snippets_carry_markers_that_parse_to_offsets(st: Store) -> None:
    """The snippet is the index's text with the hits wrapped, and ``parse_snippet`` turns it into text and
    ranges that cover the matched words, diacritics and case folded and a prefix included."""
    r = st.create_room("#build", "alice", 60, 0).id
    say(st, r, "Plan: the migration fails, we need a ROLLBACK path with Café style naming")
    got = st.search([r], search.match_query("cafe rollb") or "", nonce=NONCE)
    text, marks = search.parse_snippet(got["hits"][0]["snippet"], NONCE)
    assert [text[a:b] for a, b in marks] == ["ROLLBACK", "Café"]


def test_matching_is_by_word_with_diacritics_and_case_folded(st: Store) -> None:
    r = st.create_room("#build", "alice", 60, 0).id
    m = say(st, r, "RÉSUMÉ of the schema 49 migration")
    assert find(st, [r], "resume") == [m]
    assert find(st, [r], '"schema 49"') == [m]
    assert find(st, [r], '"49 schema"') == []
    assert find(st, [r], "migr") == [m]
    assert find(st, [r], "igration") == []  # a prefix, not a substring


def test_around_a_message_is_a_window_of_its_own_room(st: Store) -> None:
    """20 before, the message, 20 after, any kind of line, oldest first; a message of another room is not
    found. Fails if the window crosses rooms or leaves out the notices the log shows."""
    r = st.create_room("#build", "alice", 60, 0).id
    other = st.create_room("#other", "alice", 60, 0).id
    ids = []
    for i in range(60):
        ids.append(say(st, r, f"line {i}", msg_kind="notice" if i == 29 else "chat"))
        say(st, other, f"elsewhere {i}")
    got = st.history_around(r, ids[30])
    assert [m.id for m in got] == ids[10:51]
    assert [m.id for m in st.history_around(r, ids[0])] == ids[:21]
    assert st.history_around(other, ids[30]) == []
    assert st.history_around(r, 999999) == []
