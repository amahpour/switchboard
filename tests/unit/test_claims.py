"""Advisory outside-action claims: bounded inputs and decisions against a FakeClock (#293)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock

from switchboard import claims, db
from switchboard.models import session_key
from switchboard.store import Store


def test_a_claim_refuses_another_member_until_its_deadline() -> None:
    """A second member sees the holder; at the exact deadline the key can be taken."""
    clock = FakeClock()
    held = claims.Claim("build", 1, "claude-1", clock.now() + 30)
    refused = claims.decide((held,), 2, "build", 60, clock.now())
    assert refused.action == "held" and refused.holder == "claude-1"
    clock.advance(30)
    taken = claims.decide((held,), 2, "build", 60, clock.now())
    assert taken.action == "acquired" and taken.expires_at == clock.now() + 60


def test_only_the_holder_can_renew_or_release_and_each_member_has_a_cap() -> None:
    clock = FakeClock()
    held = tuple(claims.Claim(f"key-{n}", 1, "claude-1", clock.now() + 60) for n in range(8))
    assert claims.decide(held, 1, "key-0", 120, clock.now()).action == "renewed"
    assert claims.decide(held, 2, "key-0", 0, clock.now()).action == "held"
    assert claims.decide(held, 1, "key-0", 0, clock.now()).action == "released"
    assert claims.decide(held, 1, "key-8", 60, clock.now()).action == "limit"


def test_claim_keys_are_cut_and_time_to_live_is_clamped() -> None:
    assert claims.normalize_key("  edit   TICKET-123\n") == "edit TICKET-123"
    assert claims.normalize_key("x" * 1000) == "x" * 120
    assert claims.normalize_ttl(1000000) == 86400
    assert claims.normalize_ttl(0) == 0
    with pytest.raises(claims.ClaimError):
        claims.normalize_key(" \t ")
    for bad in (-1, True, "60", 1.5):
        with pytest.raises(claims.ClaimError):
            claims.normalize_ttl(bad)


def test_stored_claim_expires_and_ends_with_its_membership(tmp_path: Path) -> None:
    """The persisted key can be taken at its deadline and is removed on session end."""
    clock = FakeClock()
    store = Store(db.open_db(tmp_path / "claims.db"), clock)
    room = store.create_room("#build", "alice", 60, 30)
    first = store.upsert_participant("test", session_key("test", "", "first"))
    second = store.upsert_participant("test", session_key("test", "", "second"))
    a = store.create_membership(room.id, first.id, "claude-1", "cred-a")
    b = store.create_membership(room.id, second.id, "codex-1", "cred-b")
    assert store.move_claim(room.id, a.id, "build", 30).action == "acquired"
    assert store.move_claim(room.id, b.id, "BUILD", 30).action == "held"
    clock.advance(30)
    assert store.claims_for_room(room.id) == []
    assert store.move_claim(room.id, b.id, "build", 60).action == "acquired"
    assert store.claims_for_room(room.id)[0].holder == "codex-1"
    store.end_membership(b.id, "session_end")
    assert store.claims_for_room(room.id) == []
    assert store.move_claim(room.id, a.id, "build", 60).action == "acquired"
