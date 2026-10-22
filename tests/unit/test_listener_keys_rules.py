"""Listener keys' rules (#488, DESIGN.md §49.11) over a store and a clock, with no broker: what a claim
does, which session a key is for, when it dies. The routes and the headers are in
``tests/integration/test_listener_keys.py``."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from conftest import FakeClock

from switchboard import db
from switchboard.broker import listener_keys
from switchboard.broker.listener_keys import IDLE_S, MAX_PER_TOKEN, TOUCH_S, ListenerKeys
from switchboard.models import AgentTokenRow
from switchboard.store import Store

SESSION_A = "00000000-0000-4000-8000-0000000000a1"
SESSION_B = "00000000-0000-4000-8000-0000000000a2"


def key(n: int) -> str:
    return "sbl_" + hashlib.sha256(str(n).encode()).hexdigest()


def tok(n: int) -> str:
    return hashlib.sha256(f"token{n}".encode()).hexdigest()


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> Store:
    return Store(db.open_db(tmp_path / "t.db"), clock)


@pytest.fixture
def keys(store: Store, clock: FakeClock) -> ListenerKeys:
    return ListenerKeys(store, clock)


def token(store: Store, n: int = 1) -> AgentTokenRow:
    row = store.agent_token_add(None, f"laptop{n}", tok(n))
    assert row is not None
    return row


def test_a_claimed_key_is_the_claiming_tokens_and_has_no_session_yet(
    store: Store, keys: ListenerKeys
) -> None:
    """Claiming records the hash against the token and nothing else. Fails if the key is stored or has a
    session before anything named one."""
    t = token(store)
    keys.claim(t, listener_keys.key_hash(key(1)))
    row, owner = store.listener_key(listener_keys.key_hash(key(1)))  # type: ignore[misc]
    assert (owner.id, row.session_id) == (t.id, None)
    assert key(1) not in repr(row)


def test_the_first_session_a_key_presents_is_pinned_and_no_other_is_taken(
    store: Store, keys: ListenerKeys
) -> None:
    """A resumed session's listener comes before any hook has said which session the key is for, so the key's
    own first request pins it; after that another session is refused, and asking again for the pinned one is
    fine. Fails if the pin isn't made, moves on a request of the key's, or lets a second session in."""
    t = token(store)
    keys.claim(t, listener_keys.key_hash(key(1)))
    assert keys.check(key(1), SESSION_A).token == t
    assert keys.check(key(1), SESSION_A).token == t
    assert keys.check(key(1), SESSION_B) == listener_keys.Checked(None, "wrong_session")
    assert keys.check(key(1), SESSION_A).token == t  # the refusal changed nothing


def test_a_hook_of_the_connection_moves_the_pin_and_another_tokens_hook_does_not(
    store: Store, keys: ListenerKeys
) -> None:
    """Only a hook of the connection that claimed the key (its token and its hash) says which session it is
    for. Fails if the pin doesn't follow a /clear, or another token's hook moves it."""
    t, other = token(store, 1), token(store, 2)
    h = listener_keys.key_hash(key(1))
    keys.claim(t, h)
    keys.name_session(t, h, SESSION_A)
    assert keys.check(key(1), SESSION_A).token == t
    keys.name_session(other, h, SESSION_B)  # not the claiming token's
    assert keys.check(key(1), SESSION_B).why == "wrong_session"
    keys.name_session(t, h, SESSION_B)
    assert keys.check(key(1), SESSION_B).token == t
    assert keys.check(key(1), SESSION_A).why == "wrong_session"


def test_a_key_is_refused_when_unknown_malformed_or_its_token_was_revoked(
    store: Store, keys: ListenerKeys
) -> None:
    """Three answers, and the key that was never claimed and the one that isn't a key look the same. Fails if
    a revoked token's key still works, or the wrong word is given."""
    t = token(store)
    keys.claim(t, listener_keys.key_hash(key(1)))
    assert keys.check(key(2), SESSION_A).why == "unknown_key"
    for bad in ("", "sbl_", "sbl_" + "0" * 63, "sbl_" + "G" * 64, listener_keys.key_hash(key(1)), tok(1)):
        assert keys.check(bad, SESSION_A).why == "unknown_key", bad
    assert keys.check(key(1), SESSION_A).token is not None
    store.agent_token_revoke(t.id, None)
    assert keys.check(key(1), SESSION_A) == listener_keys.Checked(None, "revoked")


def test_removing_a_person_ends_their_keys_with_their_tokens(store: Store, keys: ListenerKeys) -> None:
    """A person's removal revokes their agent tokens, and a key is only as live as its token. Fails if a key
    outlives the person it belonged to."""
    bob = store.person_add("bob", b"b" * 16, "x" * 64, 3600.0).id
    row = store.agent_token_add(bob, "desk", tok(7))
    assert row is not None
    keys.claim(row, listener_keys.key_hash(key(7)))
    assert keys.check(key(7), SESSION_A).token is not None
    assert store.person_remove(bob) is not None
    assert keys.check(key(7), SESSION_A).why == "revoked"


def test_a_key_unused_for_a_week_is_gone_and_use_keeps_it(
    store: Store, keys: ListenerKeys, clock: FakeClock
) -> None:
    """A key ages out after IDLE_S without use, counted from its last use (a listener round, a signed-in
    request). Fails if a key never expires, expires with use, or is counted from when it was made."""
    t = token(store)
    keys.claim(t, listener_keys.key_hash(key(1)))
    for _ in range(3):  # three listener rounds, each most of the limit apart: the first claim is far behind
        clock.advance(IDLE_S * 0.8)
        assert keys.check(key(1), SESSION_A).token == t
    clock.advance(IDLE_S + 1)
    assert keys.check(key(1), SESSION_A).why == "unknown_key"


def test_the_oldest_keys_of_a_token_make_room_for_new_ones(
    store: Store, keys: ListenerKeys, clock: FakeClock
) -> None:
    """A connection that makes a key each time it starts can't fill the table: past MAX_PER_TOKEN the one
    used longest ago goes, another token's keys stay, and a key that is used is kept over one that isn't.
    Fails if the table grows, or the wrong key goes."""
    t, other = token(store, 1), token(store, 2)
    keys.claim(other, listener_keys.key_hash(key(900)))
    for n in range(MAX_PER_TOKEN):
        clock.advance(TOUCH_S + 1)  # a use is noted at most once a minute
        keys.claim(t, listener_keys.key_hash(key(n)))
    clock.advance(TOUCH_S + 1)
    assert keys.check(key(0), SESSION_A).token == t  # used now: not the oldest any more
    clock.advance(TOUCH_S + 1)
    keys.claim(t, listener_keys.key_hash(key(100)))
    assert store.listener_key(listener_keys.key_hash(key(1))) is None  # the one used longest ago
    assert store.listener_key(listener_keys.key_hash(key(0))) is not None
    assert store.listener_key(listener_keys.key_hash(key(100))) is not None
    assert store.listener_key(listener_keys.key_hash(key(900))) is not None
    count = store.con.execute("SELECT COUNT(*) FROM listener_keys WHERE token_id=?", (t.id,)).fetchone()[0]
    assert count == MAX_PER_TOKEN


def test_a_key_idle_for_a_week_is_swept_when_a_new_one_is_made(
    store: Store, keys: ListenerKeys, clock: FakeClock
) -> None:
    """Idle keys of any token are removed as a new one is claimed, so abandoned ones don't stay. Fails if
    they stay in the table."""
    t = token(store)
    keys.claim(t, listener_keys.key_hash(key(1)))
    clock.advance(IDLE_S + 1)
    keys.claim(t, listener_keys.key_hash(key(2)))
    assert store.listener_key(listener_keys.key_hash(key(1))) is None


def test_a_hash_another_token_holds_stays_with_it(store: Store, keys: ListenerKeys) -> None:
    """A claim never moves a key to another token, and says it didn't. Fails if the second claim wins."""
    first, second = token(store, 1), token(store, 2)
    h = listener_keys.key_hash(key(1))
    assert store.listener_key_claim(first.id, h, MAX_PER_TOKEN, IDLE_S) is True
    assert store.listener_key_claim(second.id, h, MAX_PER_TOKEN, IDLE_S) is False
    assert store.listener_key_claim(first.id, h, MAX_PER_TOKEN, IDLE_S) is True  # its own again
    assert store.listener_key(h)[1].id == first.id  # type: ignore[index]


def test_a_connections_every_request_writes_at_most_once_a_minute(
    store: Store, keys: ListenerKeys, clock: FakeClock
) -> None:
    """Every request of a signed-in connection carries the hash, so the claim and the session's naming are
    remembered: a request a second later writes nothing, and one a minute later notes the use. Fails if every
    request writes."""
    t = token(store)
    h = listener_keys.key_hash(key(1))
    keys.claim(t, h)
    first = store.listener_key(h)[0].last_used_at  # type: ignore[index]
    clock.advance(TOUCH_S / 2)
    keys.claim(t, h)
    assert store.listener_key(h)[0].last_used_at == first  # type: ignore[index]
    clock.advance(TOUCH_S)
    keys.claim(t, h)
    assert store.listener_key(h)[0].last_used_at > first  # type: ignore[index]
    keys.name_session(t, h, SESSION_A)
    store.con.execute("UPDATE listener_keys SET session_id=NULL")  # a write the memory doesn't know of
    keys.name_session(t, h, SESSION_A)  # remembered: not written again
    assert store.listener_key(h)[0].session_id is None  # type: ignore[index]


def test_what_the_rules_remember_stays_bounded(
    store: Store, keys: ListenerKeys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every request of every signed-in connection carries a hash, so the two memories that spare the store a
    write each time are capped, and a hash that fell out of one is simply written again. Fails if they grow
    without end, or forgetting one loses a key."""
    monkeypatch.setattr(listener_keys, "CACHE_MAX", 3)
    t = token(store)
    for n in range(6):
        keys.claim(t, listener_keys.key_hash(key(n)))
        keys.name_session(t, listener_keys.key_hash(key(n)), SESSION_A)
    assert len(keys._claimed) == 3 and len(keys._named) == 3
    keys.claim(t, listener_keys.key_hash(key(0)))  # forgotten, and claimed again: still one row
    assert store.listener_key(listener_keys.key_hash(key(0))) is not None
    assert keys.check(key(0), SESSION_A).token == t
