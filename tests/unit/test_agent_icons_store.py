"""The avatars an admin uploaded, in the store (#498, DESIGN.md §29.10): one image per harness and scheme,
a replacement keeps the table to one row, a reset takes both schemes of one harness away, and the list
never carries the bytes (a stylesheet and a settings page need only who has one, and its cache key)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from conftest import FakeClock

from switchboard import db
from switchboard.store import Store


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> Store:
    return Store(db.open_db(tmp_path / "t.db"), clock)


def put(store: Store, harness: str, scheme: str, data: bytes, *, mime: str = "image/png") -> None:
    store.agent_icon_set(harness, scheme, mime, data, 64, 48, hashlib.sha256(data).hexdigest())


def test_an_icon_is_stored_and_read_back_byte_for_byte(store: Store, clock: FakeClock) -> None:
    """What comes out is what went in: the bytes, the type, the size, the hash and the time. Fails if
    the bytes are returned as text, truncated, or the metadata isn't the row's own."""
    data = bytes(range(256)) * 3
    put(store, "codex", "light", data)
    got = store.agent_icon("codex", "light")
    assert got is not None
    meta, body = got
    assert body == data and isinstance(body, bytes)
    assert (meta.harness, meta.scheme, meta.mime) == ("codex", "light", "image/png")
    assert (meta.width, meta.height, meta.size) == (64, 48, len(data))
    assert meta.sha256 == hashlib.sha256(data).hexdigest() and meta.version == meta.sha256[:12]
    assert meta.updated_at == clock.now()
    assert store.agent_icon("codex", "dark") is None and store.agent_icon("devin", "light") is None


def test_a_second_upload_replaces_the_first_and_changes_the_version(store: Store, clock: FakeClock) -> None:
    put(store, "claude", "dark", b"first")
    first = store.agent_icons()
    clock.advance(5)
    put(store, "claude", "dark", b"second, longer")
    [now] = store.agent_icons()
    assert len(first) == 1 and now.size == len(b"second, longer")
    assert now.version != first[0].version and now.updated_at == clock.now()
    assert store.agent_icon("claude", "dark")[1] == b"second, longer"  # type: ignore[index]


def test_the_list_has_no_bytes_and_is_in_a_fixed_order(store: Store) -> None:
    """The list is for the settings page and the stylesheet: each row's size and hash, never the image,
    and always harness by harness, light before dark, whatever order they were stored in."""
    for harness, scheme in (("devin", "dark"), ("claude", "dark"), ("devin", "light"), ("claude", "light")):
        put(store, harness, scheme, f"{harness}-{scheme}".encode())
    rows = store.agent_icons()
    assert [(r.harness, r.scheme) for r in rows] == [
        ("claude", "light"),
        ("claude", "dark"),
        ("devin", "light"),
        ("devin", "dark"),
    ]
    assert not any(hasattr(r, "data") for r in rows)


def test_a_reset_takes_both_schemes_of_one_harness_and_leaves_the_others(store: Store) -> None:
    """Reset is per harness (the built-in avatar comes back in light and dark together) and says how many
    images it removed; a harness with none removes none."""
    for harness in ("codex", "cursor"):
        for scheme in ("light", "dark"):
            put(store, harness, scheme, f"{harness}-{scheme}".encode())
    assert store.agent_icon_reset("codex") == 2
    assert [(r.harness, r.scheme) for r in store.agent_icons()] == [("cursor", "light"), ("cursor", "dark")]
    assert store.agent_icon_reset("codex") == 0
    assert store.agent_icon_reset("devin") == 0


def test_the_store_refuses_a_harness_the_schema_does_not_know(store: Store) -> None:
    """A name outside the four harnesses is no row: SQLite's own CHECK is the last line, and the store
    turns it into a ValueError so the caller never sees a database error for a bad argument."""
    for harness, scheme in (("test", "light"), ("codex", "sepia"), ("../x", "light"), ("", "dark")):
        with pytest.raises(ValueError):
            put(store, harness, scheme, b"x")
    assert store.agent_icons() == []
    with pytest.raises(ValueError):
        store.agent_icon_reset("test")


def test_a_refused_write_keeps_the_icon_that_was_there(store: Store) -> None:
    """A write the store refuses (an SVG type, more bytes than the cap) leaves the earlier icon as it was:
    the old row isn't removed first and lost."""
    put(store, "codex", "light", b"keep me")
    with pytest.raises(ValueError):
        store.agent_icon_set("codex", "light", "image/svg+xml", b"<svg/>", 1, 1, "0" * 64)
    with pytest.raises(ValueError):
        put(store, "codex", "light", b"x" * (256 * 1024 + 1))
    assert store.agent_icon("codex", "light")[1] == b"keep me"  # type: ignore[index]


def test_icons_survive_closing_and_opening_the_database(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "t.db"
    store = Store(db.open_db(path), clock)
    put(store, "cursor", "dark", b"kept")
    store.con.close()
    again = Store(db.open_db(path), clock)
    assert again.agent_icon("cursor", "dark")[1] == b"kept"  # type: ignore[index]
