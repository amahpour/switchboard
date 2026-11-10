"""A pinned voice is stored with the member and its shape is checked (DESIGN.md §52.6, #713)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock
from engine_world import World


def test_set_call_voice_keeps_a_good_name_and_refuses_a_bad_one(tmp_path: Path) -> None:
    """The store is the second door after the command's parser. Fails if a bad name is stored."""
    w = World(tmp_path, FakeClock())
    _, m = w.agent("alpha")
    assert w.store.find_member(w.room.id, "alpha").call_voice is None
    w.store.set_call_voice(m.id, "bf_emma")
    assert w.store.find_member(w.room.id, "alpha").call_voice == "bf_emma"
    for bad in ("", "../x", "Bad", "x" * 33, "a b"):
        with pytest.raises(ValueError):
            w.store.set_call_voice(m.id, bad)
    assert w.store.find_member(w.room.id, "alpha").call_voice == "bf_emma"  # nothing changed
    w.store.set_call_voice(m.id, None)
    assert w.store.find_member(w.room.id, "alpha").call_voice is None
