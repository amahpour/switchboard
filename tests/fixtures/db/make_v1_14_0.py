"""Write ``v1_14_0.sql``: a schema-60 database made by switchboard 1.14.0, dumped as SQL, with a member of
every harness 1.14.0 knows.

The v60 -> v61 migration (#851, DESIGN.md §49.15) only adds Grok Build to the CHECK on
``participants.harness``; ``tests/unit/test_db_migrate_v61.py`` tests it on this dump. The rows come from
1.14.0's own store: an agent over HTTPS of each harness (``claude``, ``codex``, ``cursor``, ``devin``,
``opencode``, ``test`` and ``unknown``) joined to a room, with an agent token, a chat line from one of them
and one from the owner, bob as a person, and 60's own tables in use (the owner's webhook, saved with
``webhook_set``, and a sent key), so the step is seen to leave every table's rows alone. No broker, no
processes, a fixed clock, made-up ids, keys and addresses (``hooks.example.com``), nobody's data.

It must run against the 1.14.0 code (schema version 60), not this checkout's:

    git worktree add --detach <tmp>/sb-1140 v1.14.0
    cd <tmp>/sb-1140 && uv sync
    uv run python <this repo>/tests/fixtures/db/make_v1_14_0.py <this repo>/tests/fixtures/db/v1_14_0.sql

It imports only the installed ``switchboard`` package, never this repo's tests.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

from switchboard import db
from switchboard.store import Store

T0 = 1_790_000_000.0
HARNESSES = ("claude", "codex", "cursor", "devin", "opencode", "test", "unknown")
OWNER_URL = "https://hooks.example.com/services/T000/B000/ownersecret?token=t0"


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def now(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t

    def advance(self, s: float) -> float:
        self.t += s
        return self.t


def h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def main(out: Path) -> None:
    if db.SCHEMA_VERSION != 60:
        raise SystemExit(f"run this against switchboard 1.14.0 (schema 60), not schema {db.SCHEMA_VERSION}")
    with tempfile.TemporaryDirectory() as td:
        con = db.open_db(Path(td) / "switchboard.db")
        clock = Clock(T0)
        store = Store(con, clock)  # type: ignore[arg-type]
        room = store.create_room("#build", "alice", 60, 15)
        store.person_add("bob", b"\x01" * 16, h("one-time"), 3600.0)
        store.agent_token_add(None, "laptop", h("token-laptop"))
        clock.advance(1.0)
        members = []
        for i, harness in enumerate(HARNESSES):
            thread = f"00000000-0000-4000-8000-00000000{i:04x}"
            p = store.upsert_participant(
                harness,
                f"{harness}@laptop:{thread}",
                host="laptop",
                status="idle",
                status_at=clock.now(),
                status_src="hook",
                tier=f"{harness}:https",
                session_id=thread,
                approval_mode="prompting",
                hooks_seen_at=clock.now(),
            )
            members.append(store.create_membership(room.id, p.id, f"{harness}-1", h(f"cred-{harness}")))
            clock.advance(0.5)
        store.insert_message(
            room.id, sender_name="alice", sender_kind="human", via="web", text="@claude-1 run the tests"
        )
        clock.advance(1.0)
        store.insert_message(
            room.id,
            sender_name="claude-1",
            sender_kind="agent",
            via="mcp",
            text="all green",
            sender_membership_id=members[0].id,
            sender_harness="claude",
        )
        store.webhook_set(0, {"url": OWNER_URL, "wait_s": 300.0}, second_default_s=1800.0)
        hook = store.webhook_get(0)
        assert hook is not None
        store.webhook_keep_sent(hook["id"], "first", hook["first_gen"], ["q:build:2"], clock.now())
        con.commit()
        # iterdump writes the FTS5 table's own rows (``INSERT INTO "messages_fts"``) before the statement
        # that makes the table, so loading the dump would fail there; its shadow tables (``messages_fts_*``)
        # are dumped too and hold the same index, so those lines are left out.
        lines = (s for s in con.iterdump() if not s.startswith('INSERT INTO "messages_fts" '))
        out.write_text("\n".join(lines) + "\n")
        con.close()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
