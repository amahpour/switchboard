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
    got = store.mcp_session(mine.id, sid_hash(1), REOPEN_IDLE_S)
    assert got is not None
    assert (got.client_name, got.client_version, got.protocol) == ("Cursor", "1.0.0", "2025-11-25")
    assert store.mcp_session(theirs.id, sid_hash(1), REOPEN_IDLE_S) is None
    store.mcp_session_forget(mine.id, sid_hash(1))
    assert store.mcp_session(mine.id, sid_hash(1), REOPEN_IDLE_S) is None


def test_a_token_keeps_only_its_newest_sessions(store: Store, clock: FakeClock) -> None:
    """A client that opens a session at every start can't grow the table without end: past ``REOPEN_KEEP``
    the token's oldest goes, and another token's are untouched. Fails if the table only grows."""
    mine, theirs = token(store, 1), token(store, 2)
    opened(store, theirs, 0)
    for n in range(1, REOPEN_KEEP + 2):
        opened(store, mine, n)
        clock.advance(1)
    assert store.mcp_session(mine.id, sid_hash(1), REOPEN_IDLE_S) is None
    assert store.mcp_session(mine.id, sid_hash(2), REOPEN_IDLE_S) is not None
    assert store.mcp_session(mine.id, sid_hash(REOPEN_KEEP + 1), REOPEN_IDLE_S) is not None
    count = store.con.execute("SELECT COUNT(*) FROM mcp_sessions WHERE token_id=?", (mine.id,)).fetchone()[0]
    assert count == REOPEN_KEEP
    assert store.mcp_session(theirs.id, sid_hash(0), REOPEN_IDLE_S) is not None


def test_a_month_old_session_is_swept_when_a_new_one_opens(store: Store, clock: FakeClock) -> None:
    """Sessions of any token older than ``REOPEN_IDLE_S`` are removed as a new one is opened, so the
    sessions of a device nobody uses any more don't stay. Fails if they stay."""
    old, new = token(store, 1), token(store, 2)
    opened(store, old, 1)
    clock.advance(REOPEN_IDLE_S + 1)
    opened(store, new, 2)
    left = store.con.execute("SELECT COUNT(*) FROM mcp_sessions WHERE token_id=?", (old.id,)).fetchone()[0]
    assert left == 0  # gone with no lookup of it
    assert store.mcp_session(new.id, sid_hash(2), REOPEN_IDLE_S) is not None


def test_a_session_opened_again_starts_its_month_again(store: Store, clock: FakeClock) -> None:
    """A Cursor that stays open through restarts keeps one session ID for weeks: each time the broker opens
    it again its month restarts, so it is not swept while its client still holds the ID. Fails if the sweep
    counts only from the first open."""
    mine, other = token(store, 1), token(store, 2)
    opened(store, mine, 1)
    clock.advance(REOPEN_IDLE_S - 10)
    assert store.mcp_session_reopened(mine.id, sid_hash(1), "2025-11-25")
    clock.advance(20)
    opened(store, other, 2)  # the sweep runs here
    assert store.mcp_session(mine.id, sid_hash(1), REOPEN_IDLE_S) is not None


@pytest.mark.parametrize("by_id", [False, True])
def test_a_revoked_token_keeps_no_session_to_open_again(store: Store, by_id: bool) -> None:
    """A revoked token can't be used, so what its sessions were is kept for nothing: revoking it, by its
    person or by the broker, removes them, and another token's stay. Fails if they wait for the sweep."""
    mine, theirs = token(store, 1), token(store, 2)
    opened(store, mine, 1)
    opened(store, theirs, 2)
    revoked = store.agent_token_revoke_id(mine.id) if by_id else store.agent_token_revoke(mine.id, None)
    assert revoked is not None
    assert store.mcp_session(mine.id, sid_hash(1), REOPEN_IDLE_S) is None
    assert store.mcp_session(theirs.id, sid_hash(2), REOPEN_IDLE_S) is not None


def test_a_record_past_its_month_is_not_reopened_even_unswept(store: Store, clock: FakeClock) -> None:
    """The month is checked when a session would be opened again, not only when a new initialize sweeps:
    with nothing opened since, a record just past ``REOPEN_IDLE_S`` is refused (and deleted) and one just
    inside is found. Fails if the lookup reads the record whatever its age (#903, F3)."""
    mine = token(store, 1)
    opened(store, mine, 1)
    clock.advance(REOPEN_IDLE_S - 1)
    assert store.mcp_session(mine.id, sid_hash(1), REOPEN_IDLE_S) is not None  # just inside
    opened(store, mine, 2)
    clock.advance(REOPEN_IDLE_S + 1)
    assert store.mcp_session(mine.id, sid_hash(2), REOPEN_IDLE_S) is None  # just past, never swept
    left = store.con.execute("SELECT COUNT(*) FROM mcp_sessions WHERE token_id=?", (mine.id,)).fetchone()[0]
    assert left == 1  # session 2 deleted by the lookup; session 1 waits for its own lookup or a sweep


def test_a_reopen_whose_record_went_is_refused(store: Store) -> None:
    """The broker binds a reopened session only if its record is still there after the handshake: a DELETE
    or a revoke that ran meanwhile wins. Fails if ``mcp_session_reopened`` says yes for a gone record."""
    mine = token(store, 1)
    opened(store, mine, 1)
    store.mcp_session_forget(mine.id, sid_hash(1))
    assert not store.mcp_session_reopened(mine.id, sid_hash(1), "2025-11-25")


def test_removing_a_person_removes_their_tokens_sessions(store: Store) -> None:
    """``person_remove`` revokes all of a person's tokens in one UPDATE, not through the one-token revokes
    that delete ``mcp_sessions``: their records must go in the same transaction, and nobody else's. Fails if
    a removed person's sessions stay to be opened again (#903, F7)."""
    alice = store.person_add("alice", b"a" * 16, "0" * 64, 3600.0)
    bob = store.person_add("bob", b"b" * 16, "1" * 64, 3600.0)
    hers = [store.agent_token_add(alice.id, f"alice-{n}", f"{n:064x}") for n in (1, 2)]
    his = store.agent_token_add(bob.id, "bob-1", f"{3:064x}")
    assert his is not None and all(t is not None for t in hers)
    for n, t in enumerate([*hers, his]):
        assert t is not None
        opened(store, t, n)
    assert store.person_remove(alice.id) is not None
    for n, t in enumerate(hers):
        assert t is not None and store.mcp_session(t.id, sid_hash(n), REOPEN_IDLE_S) is None
    assert store.mcp_session(his.id, sid_hash(2), REOPEN_IDLE_S) is not None
