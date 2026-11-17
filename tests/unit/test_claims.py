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


def test_a_note_is_cleaned_collapsed_and_cut_to_the_status_line_cap() -> None:
    """A note is the claim's state or a link. It is cleaned the way ``sanitize`` cleans
    (controls dropped, ``yk:`` defanged), whitespace collapsed, and cut at 160 characters.
    Empty stays empty. A non-string is refused, so a number cannot become a note.
    Fails if a newline, a ``yk:`` token or a 500-character note is stored as given."""
    assert claims.normalize_note("  queued \n now  ") == "queued now"
    assert claims.normalize_note("x" * 500) == "x" * claims.NOTE_MAX == "x" * 160
    assert claims.normalize_note("see yk:secret") == "see yk_:secret"
    assert claims.normalize_note("a\x00b") == "ab"
    assert claims.normalize_note("") == "" and claims.normalize_note(None) == ""
    for bad in (1, True, 1.5):
        with pytest.raises(claims.ClaimError):
            claims.normalize_note(bad)


def test_a_renewal_keeps_an_empty_note_and_a_new_one_replaces_it() -> None:
    """Only the holder writes the note. An empty note on renewal keeps the one stored;
    a non-empty note replaces it. Release drops the claim, and another member's note is
    refused with the claim. Fails if a renewal wipes the note, or anyone can overwrite it."""
    clock = FakeClock()
    held = (claims.Claim("build", 1, "claude-1", clock.now() + 30, "queued"),)
    kept = claims.decide(held, 1, "build", 60, clock.now(), "")
    assert kept.action == "renewed" and kept.note == "queued" and kept.expires_at == clock.now() + 60
    replaced = claims.decide(held, 1, "build", 60, clock.now(), "review https://example.com/pr/56")
    assert replaced.action == "renewed" and replaced.note == "review https://example.com/pr/56"
    fresh = claims.decide((), 1, "build", 60, clock.now(), "queued")
    assert fresh.action == "acquired" and fresh.note == "queued"
    released = claims.decide(held, 1, "build", 0, clock.now(), "nope")
    assert released.action == "released"
    blocked = claims.decide(held, 2, "build", 60, clock.now(), "mine")
    assert blocked.action == "held" and blocked.holder == "claude-1" and blocked.note == ""


def test_a_stored_note_survives_a_renewal_and_leaves_with_the_claim(tmp_path: Path) -> None:
    """The row keeps the note. A renewal with no note leaves it; a renewal with one
    replaces it; release deletes the row. Fails if the column is missing or a blank
    renewal clears what the holder wrote."""
    clock = FakeClock()
    store = Store(db.open_db(tmp_path / "claims.db"), clock)
    room = store.create_room("#build", "alice", 60, 30)
    first = store.upsert_participant("test", session_key("test", "", "first"))
    a = store.create_membership(room.id, first.id, "claude-1", "cred-a")
    got = store.move_claim(room.id, a.id, "build", 30, "queued")
    assert got.action == "acquired" and got.note == "queued"
    assert store.claims_for_room(room.id)[0].note == "queued"
    kept = store.move_claim(room.id, a.id, "build", 60, "")
    assert kept.action == "renewed" and kept.note == "queued"
    assert store.claims_for_room(room.id)[0].note == "queued"
    replaced = store.move_claim(room.id, a.id, "build", 60, "done")
    assert replaced.note == "done" and store.claims_for_room(room.id)[0].note == "done"
    assert store.move_claim(room.id, a.id, "build", 0, "ignored").action == "released"
    assert store.claims_for_room(room.id) == []


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


def test_a_key_held_in_another_room_refuses_a_new_claim_and_names_where() -> None:
    """#817: the second session claims the same key from another room. The pure rule is given that room's
    claims on the key (``elsewhere``) and refuses with the holder, its room and its device; at the exact
    deadline the key is free again, and the case is the same in upper case. Fails if a claim in room B
    succeeds over room A's, loses where the holder is, or holds past its deadline."""
    clock = FakeClock()
    there = claims.Claim("SYS-645", 9, "claude-1", clock.now() + 30, "editing", room_id=4, host="laptop")
    refused = claims.decide((), 2, "sys-645", 60, clock.now(), "", (there,))
    assert refused.action == "held" and refused.holder == "claude-1"
    assert refused.room_id == 4 and refused.host == "laptop" and refused.expires_at == there.expires_at
    clock.advance(30)
    assert claims.decide((), 2, "sys-645", 60, clock.now(), "", (there,)).action == "acquired"


def test_a_claim_the_member_already_holds_is_never_asked_about_other_rooms() -> None:
    """A renewal and a release of the member's own claim ignore ``elsewhere`` (a duplicate that existed
    before the key spanned rooms must not lock its own holder out), and a different key is
    untouched. Fails if renewing a held key is refused because another room also holds it, or an
    unrelated key is."""
    clock = FakeClock()
    mine = claims.Claim("a", 1, "claude-1", clock.now() + 60)
    other = claims.Claim("a", 7, "codex-1", clock.now() + 60, room_id=3, host="box")
    assert claims.decide((mine,), 1, "a", 120, clock.now(), "", (other,)).action == "renewed"
    assert claims.decide((mine,), 1, "a", 0, clock.now(), "", (other,)).action == "released"
    assert claims.decide((), 1, "b", 60, clock.now(), "", (other,)).action == "acquired"
    assert (
        claims.decide((), 1, "a", 0, clock.now(), "", (other,)).action == "released"
    )  # releasing nothing is fine


def test_the_refusal_names_the_room_only_when_the_asker_can_see_it() -> None:
    """A private room's name and holder go only to someone in it (§44): in the same room the sentence is
    the one it always was, in another room it has the holder, device and room, and a room the asker
    can't see is only
    'held elsewhere'. Fails if the hidden room's name or holder leaks into the refusal."""
    assert claims.refusal_text("k", "claude-1", "", None, True) == "k is claimed by claude-1"
    named = claims.refusal_text("k", "claude-1", "laptop", "#build", False)
    assert named == "k is claimed by claude-1@laptop in #build"
    assert claims.refusal_text("k", "claude-1", "", "#build", False) == "k is claimed by claude-1 in #build"
    hidden = claims.refusal_text("k", "claude-1", "laptop", None, False)
    assert hidden == "k is held elsewhere on this broker"
    assert "claude-1" not in hidden and "laptop" not in hidden


def test_the_store_asks_other_rooms_but_not_the_same_session(tmp_path: Path) -> None:
    """``Store.move_claim`` makes the key one name across rooms: a second session's claim is refused with the
    holder's room and device, the same session in a second room is one holder and may claim there too, an
    expired or ended claim blocks nobody, and a release frees it. Fails if a claim in room B succeeds beside
    room A's, a session blocks itself, or a stale row blocks."""
    clock = FakeClock()
    st = Store(db.open_db(tmp_path / "c.db"), clock)
    a = st.create_room("#a", "alice", 60, 10)
    b = st.create_room("#b", "alice", 60, 10)

    def member(room_id: int, harness: str, host: str, name: str) -> tuple[int, int]:
        key = session_key(harness, host, name)
        p = st.upsert_participant(harness, key, status="idle", tier="mcp-only", host=host)
        return p.id, st.create_membership(room_id, p.id, name).id

    p1, m1 = member(a.id, "claude", "laptop", "claude-1")
    p2, m2 = member(b.id, "codex", "box", "codex-1")
    assert st.move_claim(a.id, m1, "SYS-645", 120).action == "acquired"
    refused = st.move_claim(b.id, m2, "sys-645", 120)
    assert refused.action == "held" and refused.holder == "claude-1"
    assert refused.room_id == a.id and refused.host == "laptop"
    # the same session in another room is one holder
    twin = st.create_membership(b.id, p1, "claude-1")
    assert st.move_claim(b.id, twin.id, "SYS-645", 120).action == "acquired"
    assert st.move_claim(a.id, m1, "SYS-645", 0).action == "released"
    assert st.move_claim(b.id, twin.id, "SYS-645", 0).action == "released"
    # expiry, then the end of the holder's membership, free it
    assert st.move_claim(a.id, m1, "k", 30).action == "acquired"
    clock.advance(30)
    assert st.move_claim(b.id, m2, "k", 30).action == "acquired"
    assert st.move_claim(a.id, m1, "k", 30).action == "held"
    st.end_membership(m2, "leave")
    assert st.move_claim(a.id, m1, "k", 30).action == "acquired"
    assert p2
