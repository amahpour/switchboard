"""Who is an admin (#569, DESIGN.md §51).

``people.summary`` reports each person's own ``admin`` flag, and ``owner`` only on the
account that claimed the broker. ``Who.is_admin`` is that flag or the owner. Fails if
the list still hardcodes every person but the owner as not an admin, or if the owner
is the only one ``is_admin`` accepts.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from conftest import FakeClock

from switchboard import db
from switchboard.broker.people import Who, summary
from switchboard.config import Config
from switchboard.store import Store


def test_is_admin_is_the_owner_or_the_row_flag() -> None:
    """The owner is an admin with no people row. A person is one only when the row says so."""
    owner = Who("h", None, "alice")
    assert owner.owner is True and owner.is_admin is True and owner.admin is False
    bob = Who("h", 2, "bob", admin=True)
    assert bob.owner is False and bob.is_admin is True
    carol = Who("h", 3, "carol")
    assert carol.is_admin is False


def test_summary_reports_admin_and_owner(tmp_path: Path, clock: FakeClock) -> None:
    """The owner's entry is owner and admin. Bob's admin is the column, not a hardcoded
    False, and he is not the owner. Fails if summary still forces every person to
    admin False, or omits owner."""
    store = Store(db.open_db(tmp_path / "y.db"), clock)
    store.claim_owner(b"0123456789abcdef")
    bob = store.person_add("bob", b"bob-handle-16b!!", "hash", 60.0)
    store.person_set_admin(bob.id, True)
    store.person_add("carol", b"carol-handle-16b", "hash", 60.0)
    state = SimpleNamespace(store=store, clock=clock, cfg=Config(human_name="alice"), oidc=None)
    got = {(p["name"], p["admin"], p["owner"]) for p in summary(state)}
    assert got == {("alice", True, True), ("bob", True, False), ("carol", False, False)}
