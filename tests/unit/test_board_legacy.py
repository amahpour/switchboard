"""What an earlier release stored is cleaned once, by the upgrade, and the live code carries no exception
for it
(DESIGN.md §56.5, schema 56, #893).

After ``board.clean_stored`` (what the upgrade runs, ``tests/unit/test_db_migrate_v56.py``) no stored id
or text
holds a secret, so every id and reference is checked again on every write, and an agent's move or delete
writes
no secret into a change record. Every secret is built from pieces, so no scanner reads this file as a leak."""

from __future__ import annotations

from typing import Any

from switchboard import board
from switchboard.redact import redact

TOKEN = "ghp" + "_" + "aB3" * 12


def base(**over: Any) -> dict[str, Any]:
    el: dict[str, Any] = {
        "id": "a", "type": "rectangle", "x": 0, "y": 0, "width": 120, "height": 60,
        "version": 1, "versionNonce": 1, "isDeleted": False, "link": None,
    }  # fmt: skip
    el.update(over)
    return el


def legacy_board() -> list[dict[str, Any]]:
    """A box with a secret-shaped id and its multi-line label, as an earlier release could have stored
    them."""
    box = base(id=TOKEN, boundElements=[{"id": "lab1", "type": "text"}])
    label = base(
        id="lab1", type="text", containerId=TOKEN, text="two\nlines", fontSize=16, boundElements=None
    )
    return [box, label]


def written_ids(elements: list[dict[str, Any]], action: str, **params: Any) -> list[str]:
    """The ids the broker puts in the change record of an agent's action (``RoomBoard._commit``)."""
    got = board.apply_action(elements, action, params, now=10)
    assert not got.error, got.error
    merged = board.merge(elements, list(got.incoming), agent=True)
    return [e["id"] for e in merged.taken]


def test_a_move_or_delete_writes_no_secret_id_into_a_change_record_once_the_board_is_cleaned() -> None:
    """Moving or deleting a legacy shape copies its id into the new change record. The upgrade drops a shape
    whose id is a secret, so nothing is left to copy. Fails if the cleaning leaves the shape (the secret id is
    then written again, or the action is refused)."""
    cleaned, dropped = board.clean_stored(legacy_board())
    assert TOKEN in dropped and all(not redact(e["id"])[1] for e in cleaned)
    for e in cleaned:
        assert not redact(str(e))[1]
    assert cleaned == []  # the label held the secret id as its container: dropped with its box
    for action, params in (("move", {"x": 3, "y": 3}), ("delete", {})):
        target = base(id="box2")
        ids = written_ids([*cleaned, target], action, id="box2", **params)
        assert ids and not any(redact(i)[1] for i in ids)


def test_every_id_and_reference_is_checked_on_every_write_with_no_exception_for_a_stored_one() -> None:
    """After the upgrade no stored id holds a secret, so a write to a stored shape is checked like any
    other: a
    new revision that carries a secret in a reference, or a new shape that refers to one, is refused. Fails
    if a stored shape's own references are let through."""
    stored = [base(id="s1", groupIds=[TOKEN])]  # an id a pre-upgrade board could hold
    carrying = base(id="s1", version=2, groupIds=[TOKEN])
    assert board.merge(stored, [carrying], agent=False).rejected
    new = base(id="n1", containerId=TOKEN)
    assert board.merge(stored, [new], agent=False).rejected
    assert board.merge(stored, [base(id="n2", groupIds=["g1"])], agent=False).elements[-1]["id"] == "n2"


def test_a_tombstone_keeps_no_text_so_a_delete_copies_nothing_to_check() -> None:
    """A delete copies the old fields into a tombstone; the tombstone keeps no text, so nothing in it is read
    or kept. Fails if the text is carried into the tombstone."""
    label = base(
        id="lab1", type="text", text="two\nlines", originalText="two lines", isDeleted=True, version=2
    )
    (out,) = board.merge([], [label], agent=False).elements
    assert out["text"] == "" and out["originalText"] == ""


def test_a_new_text_with_line_breaks_and_no_original_is_still_refused() -> None:
    """The check on newly supplied text stays."""
    got = board.merge([], [base(id="n1", type="text", text="two\nlines")], agent=False)
    assert got.elements == () and got.rejected
