"""Shapes an earlier broker stored are tolerated by the live code (DESIGN.md §56.5, #893).

A write that supplies new text or a new id is checked: a text with line breaks and no ``originalText``, an id
that is a secret. A write to a shape that is already stored (an agent's move, resize or delete copies its old
fields into the new revision) must not refuse what came from storage, or a delete fails, or commits a box and
leaves its old label behind. Every secret is built from pieces, so no scanner reads this file as a leak."""

from __future__ import annotations

from typing import Any

import pytest

from switchboard import board

TOKEN = "ghp" + "_" + "aB3" * 12


def base(**over: Any) -> dict[str, Any]:
    el: dict[str, Any] = {
        "id": "a", "type": "rectangle", "x": 0, "y": 0, "width": 120, "height": 60,
        "version": 1, "versionNonce": 1, "isDeleted": False, "link": None,
    }  # fmt: skip
    el.update(over)
    return el


def stored_board(box_id: str = "box1") -> list[dict[str, Any]]:
    """A box and its label as an earlier broker could have stored them: the label has line breaks and no
    ``originalText``, and its box carries the secret-shaped id the test passes."""
    box = base(id=box_id, boundElements=[{"id": "lab1", "type": "text"}])
    label = base(
        id="lab1", type="text", containerId=box_id, text="two\nlines", fontSize=16, fontFamily=1,
        textAlign="center", verticalAlign="middle", lineHeight=1.25, boundElements=None,
    )  # fmt: skip
    return [box, label]


def run(elements: list[dict[str, Any]], action: str, **params: Any) -> list[dict[str, Any]]:
    got = board.apply_action(elements, action, params, now=10)
    assert not got.error, got.error
    merged = board.merge(elements, list(got.incoming), agent=True)
    assert not merged.rejected, merged.rejected
    return list(merged.elements)


@pytest.mark.parametrize("box_id", ["box1", TOKEN], ids=["plain-id", "secret-id"])
def test_deleting_a_stored_legacy_box_takes_its_label_and_its_text_with_it(box_id: str) -> None:
    """The tombstone of the label copies text a write would refuse, and the box's id may be one a write would
    refuse. A tombstone keeps no text, and a stored id is accepted on a write to that stored shape. Fails if
    the delete is refused, or commits the box and leaves the label."""
    after = run(stored_board(box_id), "delete", id=box_id)
    assert all(e["isDeleted"] for e in after)
    label = next(e for e in after if e["id"] == "lab1")
    assert label["text"] == ""  # nothing is copied into a tombstone


def test_moving_and_resizing_a_stored_legacy_box_commits_it_and_keeps_its_label() -> None:
    """A move or a resize bumps the label too, with the text it already has. Fails if the label is refused
    (the box then moves and its label stays behind), or if its text is lost."""
    moved = run(stored_board(), "move", id="box1", x=300, y=40)
    box = next(e for e in moved if e["id"] == "box1")
    label = next(e for e in moved if e["id"] == "lab1")
    assert box["x"] == 300 and label["version"] == 2 and label["text"] == "two\nlines"
    resized = run(stored_board(), "resize", id="box1", w=200, h=90)
    assert next(e for e in resized if e["id"] == "box1")["width"] == 200
    assert next(e for e in resized if e["id"] == "lab1")["version"] == 2


def test_a_stored_text_with_a_secret_is_cut_when_it_is_touched_and_never_refused() -> None:
    """A stored text that holds a secret (from before the check) and that is moved is redacted, and a marker
    that makes it longer than the limit cuts it rather than refusing the move."""
    elements = stored_board()
    elements[1]["text"] = "x" * 170 + " password=" + "hunter2" * 2
    after = run(elements, "move", id="box1", x=5, y=5)
    text = next(e for e in after if e["id"] == "lab1")["text"]
    assert "hunter2" not in text and len(text) <= board.MAX_TEXT


def test_a_new_element_with_such_text_or_such_an_id_is_still_refused() -> None:
    """The checks stay for what is newly supplied. Fails if the stored-shape tolerance leaks to a new id."""
    new_text = base(id="n1", type="text", text="two\nlines")
    new_id = base(id=TOKEN)
    got = board.merge(stored_board(), [new_text, new_id], agent=False)
    assert len(got.rejected) == 2 and "n1" not in str(got.elements[2:])
    assert [e["id"] for e in got.elements] == ["box1", "lab1"]
    # an id the stored shape did not have is checked even on a write to that stored shape
    sneaky = {**stored_board()[0], "groupIds": [TOKEN], "version": 2}
    assert board.merge(stored_board(), [sneaky], agent=False).rejected
