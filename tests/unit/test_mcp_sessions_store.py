"""What opened a token's MCP sessions, kept so a restart can open them again (DESIGN.md §49.2, #823)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from conftest import FakeClock

from switchboard import db
from switchboard.broker.agent_http import REOPEN_IDLE_S, REOPEN_KEEP
from switchboard.models import AgentTokenRow
from switchboard.store import Store


def sid_hash(n: int) -> str:
    return hashlib.sha256(f"session-{n}".encode()).hexdigest()


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> Store:
    return Store(db.open_db(tmp_path / "y.db"), clock)


def token(store: Store, n: int = 1) -> AgentTokenRow:
    row = store.agent_token_add(None, f"device{n}", hashlib.sha256(f"token-{n}".encode()).hexdigest())
    assert row is not None
    return row


def opened(store: Store, t: AgentTokenRow, n: int) -> None:
    store.mcp_session_opened(t.id, sid_hash(n), "Cursor", "1.0.0", "2025-11-25", REOPEN_KEEP, REOPEN_IDLE_S)


def test_a_session_is_kept_for_its_own_token_only(store: Store) -> None:
    """The record is the token's: another token asking for the same ID's hash finds nothing, and a DELETE
    forgets it. Fails if a record could be read across tokens, or outlives the client ending it."""
    mine, theirs = token(store, 1), token(store, 2)
    opened(store, mine, 1)
    got = store.mcp_session(mine.id, sid_hash(1))
    assert got is not None
    assert (got.client_name, got.client_version, got.protocol) == ("Cursor", "1.0.0", "2025-11-25")
    assert store.mcp_session(theirs.id, sid_hash(1)) is None
    store.mcp_session_forget(mine.id, sid_hash(1))
    assert store.mcp_session(mine.id, sid_hash(1)) is None


def test_a_token_keeps_only_its_newest_sessions(store: Store, clock: FakeClock) -> None:
    """A client that opens a session at every start can't grow the table without end: past ``REOPEN_KEEP``
    the token's oldest goes, and another token's are untouched. Fails if the table only grows."""
    mine, theirs = token(store, 1), token(store, 2)
    opened(store, theirs, 0)
    for n in range(1, REOPEN_KEEP + 2):
        opened(store, mine, n)
        clock.advance(1)
    assert store.mcp_session(mine.id, sid_hash(1)) is None
    assert store.mcp_session(mine.id, sid_hash(2)) is not None
    assert store.mcp_session(mine.id, sid_hash(REOPEN_KEEP + 1)) is not None
    count = store.con.execute("SELECT COUNT(*) FROM mcp_sessions WHERE token_id=?", (mine.id,)).fetchone()[0]
    assert count == REOPEN_KEEP
    assert store.mcp_session(theirs.id, sid_hash(0)) is not None


def test_a_month_old_session_is_swept_when_a_new_one_opens(store: Store, clock: FakeClock) -> None:
    """Sessions of any token older than ``REOPEN_IDLE_S`` are removed as a new one is opened, so the
    sessions of a device nobody uses any more don't stay. Fails if they stay."""
    old, new = token(store, 1), token(store, 2)
    opened(store, old, 1)
    clock.advance(REOPEN_IDLE_S + 1)
    opened(store, new, 2)
    assert store.mcp_session(old.id, sid_hash(1)) is None
    assert store.mcp_session(new.id, sid_hash(2)) is not None
