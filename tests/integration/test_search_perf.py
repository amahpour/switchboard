"""How long a search takes on a large database (#724, DESIGN.md §58.9).

The route runs on the broker's event loop, so a slow search stalls every page and agent. This plants 100,000
chat lines in 100 rooms (a quarter of them in private rooms the person is not in), then runs real searches
through ``broker.search.run``, which is the route's whole body: the rooms the person can see, the safe query,
the index and the snippets. The bound is relative to the machine: a search must take less than a share of the
time a plain read of every line's text takes to find one word in Python, so a slow runner does not
fail it and an index
that is not used (a scan, a filter applied after the count) does."""

from __future__ import annotations

import re
import statistics
import time
from pathlib import Path

from conftest import InProcBroker

from switchboard.broker import search as search_api

ROOMS, PER_ROOM = 100, 1000  # 100,000 lines
MAX_SHARE = 0.5
RARE, COMMON = "zebrafish", "upload"


def seed(b: InProcBroker) -> None:
    store = b.state.store
    store.con.execute("BEGIN")
    for r in range(ROOMS):
        room = store.create_room(f"#room-{r:03d}", "alice", 60, 0)
        if r % 4 == 3:  # private rooms that bob is not in
            store.con.execute("UPDATE rooms SET private=1 WHERE id=?", (room.id,))
        rows = []
        for i in range(PER_ROOM):
            text = f"line {i} about the {COMMON} limit and the old endpoint number {r * PER_ROOM + i}"
            if i == 500 and r % 10 == 0:
                text += f" {RARE}"
            rows.append((room.id, 1.0 + i, "claude-1", "agent", "mcp", "chat", text, "[]"))
        store.con.executemany(
            "INSERT INTO messages(room_id, ts, sender_name, sender_kind, via, kind, text, mentions)"
            " VALUES(?,?,?,?,?,?,?,?)",
            rows,
        )
    store.con.execute("COMMIT")


def plan_of(b: InProcBroker, q: str) -> list[str]:
    """The query plan of every SELECT a search runs, read from SQLite itself: the statements are caught as
    the real ``search_api.run`` executes them, then each is asked for its ``EXPLAIN QUERY PLAN``."""
    con = b.state.store.con
    seen: list[str] = []

    def collect() -> None:
        con.set_trace_callback(lambda sql: seen.append(sql) if sql.lstrip().startswith("SELECT") else None)
        try:
            search_api.run(b.state, 7, q=q)
        finally:
            con.set_trace_callback(None)

    b.on_loop(collect)
    return [
        row[3]
        for sql in seen
        if "messages_fts" in sql
        for row in b.on_loop(lambda s=sql: con.execute("EXPLAIN QUERY PLAN " + s).fetchall())
    ]


def test_searching_100k_lines_stays_fast(tmp_home: Path) -> None:
    """A search uses the full-text index, and costs a small share of reading every line.

    Two checks, because a timing alone cannot tell the failures apart on every query. The plan check is
    exact: each query walks ``messages_fts`` by its match, and a message is only looked up by its id (a
    ``LIKE`` over the table, or the index used the wrong way round, shows as a scan, a lookup by room or a
    sort there). The timing check keeps its bound: the median, over seven rounds, of the search time divided
    by the time to read every line, each round measuring both back to back.
    Why back to back: the common-word query is mostly a fixed cost (about 1,000 rows looked up for the
    capped count, a page, its snippets), so it takes 0.15 of a scan here against 0.03 for the rare word, and
    a runner that went slow for a few seconds moved the search and a scan measured earlier by different
    amounts: 42 ms against 82 ms on macOS (0.52), the flake. A scan taken in the same round shares the
    load. The answers are real too: a first page of 50 with a count, only the visible rooms."""
    b = InProcBroker(tmp_home).start()
    try:
        # planting 100,000 lines through the index's triggers took more than the default 10 s on a slow runner
        b.on_loop(seed, b, timeout=300)
        store = b.state.store
        viewer = 7  # a person with no private rooms: three quarters of the lines are theirs

        def round_of(q: str) -> tuple[float, float, dict[str, object]]:
            t = time.perf_counter()
            out = search_api.run(b.state, viewer, q=q)
            searched = time.perf_counter() - t
            t = time.perf_counter()
            sum(1 for (text,) in store.con.execute("SELECT text FROM messages") if RARE in text)
            return searched, time.perf_counter() - t, out

        for q, check in (
            (RARE, lambda o: o["total"] == 10 - len([r for r in range(0, ROOMS, 10) if r % 4 == 3])),
            (
                COMMON + " limit",
                lambda o: (
                    len(o["hits"]) == 50 and o["total"] == 1000 and o["capped"]  # type: ignore[arg-type]
                ),
            ),
            (f'"the {COMMON} limit"', lambda o: len(o["hits"]) == 50),  # type: ignore[arg-type]
        ):
            plan = plan_of(b, q)
            # the index is walked by its match, and a message is only ever looked up by its id
            assert any(re.fullmatch(r"SCAN f VIRTUAL TABLE INDEX \d+:M\d+", ln) for ln in plan), plan
            assert all(
                "INTEGER PRIMARY KEY" in ln for ln in plan if re.match(r"(SCAN|SEARCH) (m|messages)\b", ln)
            ), plan
            assert not [ln for ln in plan if "TEMP B-TREE" in ln], plan  # no sort of every match
            rounds = [b.on_loop(round_of, q) for _ in range(7)]
            assert check(rounds[-1][2]), rounds[-1][2]["total"]  # type: ignore[index]
            share = statistics.median(s / r for s, r, _ in rounds)
            ms = statistics.median(s for s, _, _ in rounds) * 1000
            scan_ms = statistics.median(r for _, r, _ in rounds) * 1000
            print(f"search {q!r}: median {ms:.1f} ms; reading every line once {scan_ms:.1f} ms ({share:.2f})")
            assert share < MAX_SHARE, (
                f"search {q!r} took {ms:.0f} ms, over {MAX_SHARE:.0%} of the {scan_ms:.0f} ms to read all"
            )
    finally:
        b.stop()
