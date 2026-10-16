"""Agent tokens in the store (#382, DESIGN.md §49.1): one person's tokens, one live token per
device, revoking (by its person, or by their removal), and the token's own shape."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from conftest import FakeClock

from switchboard import db
from switchboard.broker import agent_tokens
from switchboard.store import Store


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> Store:
    return Store(db.open_db(tmp_path / "t.db"), clock)


def bob(store: Store) -> int:
    return store.person_add("bob", b"b" * 16, "x" * 64, 3600.0).id


def h(n: int) -> str:
    return hashlib.sha256(str(n).encode()).hexdigest()


def test_a_token_is_found_by_its_hash_listed_for_its_person_and_gone_once_revoked(store: Store) -> None:
    """The owner's token (person None) and bob's are each their own: listed only for them, found
    by hash while live, revoked only by their own person. Fails if a list leaks another person's
    tokens, if a revoked token still authenticates, or if someone else can revoke one."""
    b = bob(store)
    mine = store.agent_token_add(None, "laptop", h(1))
    theirs = store.agent_token_add(b, "desk", h(2))
    assert mine is not None and theirs is not None
    assert [t.device for t in store.agent_tokens_of(None)] == ["laptop"]
    assert [t.device for t in store.agent_tokens_of(b)] == ["desk"]
    assert store.agent_token_by_hash(h(1)) == mine
    assert store.agent_token_revoke(mine.id, b) is None  # not bob's
    assert store.agent_token_revoke(theirs.id, None) is None  # not the owner's either
    gone = store.agent_token_revoke(mine.id, None)
    assert gone is not None and gone.revoked_at is not None
    assert store.agent_token_by_hash(h(1)) is None
    assert store.agent_tokens_of(None) == []
    assert store.agent_token_revoke(mine.id, None) is None  # once
    assert store.agent_token_by_hash(h(2)) == theirs


def test_one_live_token_per_device_name_whoever_has_it(store: Store) -> None:
    """A device name is a host name, so it is one live token's at a time, across people; revoking
    frees it. Fails if two live tokens share a device (two people's agents on one host)."""
    b = bob(store)
    first = store.agent_token_add(None, "laptop", h(1))
    assert first is not None
    assert store.agent_token_add(b, "laptop", h(2)) is None
    assert store.agent_token_add(None, "laptop", h(3)) is None
    assert store.agent_token_for_device("laptop") == first
    store.agent_token_revoke(first.id, None)
    assert store.agent_token_for_device("laptop") is None
    again = store.agent_token_add(b, "laptop", h(2))
    assert again is not None and again.person_id == b


def test_removing_a_person_revokes_their_tokens_and_no_one_elses(store: Store) -> None:
    """Removing someone takes their agents' access with it, in the same transaction as their
    passkeys and sessions. Fails if a removed person's token still authenticates, or if anyone
    else's is revoked with it."""
    b = bob(store)
    store.agent_token_add(b, "desk", h(1))
    store.agent_token_add(b, "pi", h(2))
    store.agent_token_add(None, "laptop", h(3))
    assert store.person_remove(b) is not None
    assert store.agent_token_by_hash(h(1)) is None and store.agent_token_by_hash(h(2)) is None
    assert store.agent_tokens_of(b) == []
    assert [t.device for t in store.agent_tokens_of(None)] == ["laptop"]


def test_last_use_is_the_clock_and_only_for_a_live_token(store: Store, clock: FakeClock) -> None:
    """The list shows when each token was last used. Fails if the time isn't the store's clock, or
    a revoked token's row still changes."""
    t = store.agent_token_add(None, "laptop", h(1))
    assert t is not None and t.last_used_at is None
    clock.advance(90)
    store.agent_token_used(t.id)
    got = store.agent_token_by_hash(h(1))
    assert got is not None and got.last_used_at == clock.now()
    store.agent_token_revoke(t.id, None)
    clock.advance(90)
    store.agent_token_used(t.id)
    row = store.con.execute("SELECT last_used_at FROM agent_tokens WHERE id=?", (t.id,)).fetchone()
    assert row[0] == got.last_used_at


def test_the_store_refuses_a_bad_device_name_or_hash(store: Store) -> None:
    """The store holds the column's rules itself: a host name, and a sha256. Fails if anything
    else is saved."""
    for device, digest in (("Laptop", h(1)), ("1laptop", h(1)), ("x" * 25, h(1)), ("laptop", "abc")):
        with pytest.raises(ValueError):
            store.agent_token_add(None, device, digest)


def test_a_token_is_32_random_bytes_behind_a_prefix_and_kept_as_its_sha256() -> None:
    """Tokens are long, random, recognizable (``sbat_``, so a scanner can spot a leaked one), and
    the store keeps only their hash. Fails if two tokens repeat, the shape changes, or the hash
    isn't sha256 of the token."""
    tokens = {agent_tokens.new_token() for _ in range(50)}
    assert len(tokens) == 50
    for t in tokens:
        assert agent_tokens.TOKEN_RE.fullmatch(t), t
        assert agent_tokens.token_hash(t) == hashlib.sha256(t.encode()).hexdigest()
