"""A room's whiteboard: the merge, what may be stored, and the rows (DESIGN.md §56).

The merge is Excalidraw's: a higher version wins, a tie goes to the lower nonce, and a delete stays
as a tombstone. Shapes from outside are a claim. These tests fail if a link, an image, a script
field or an oversized shape is kept, if a newer drawing loses to an older one, or if a change
forgets who made it and when."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock

from switchboard import board, db
from switchboard.store import Conflict, Store


def el(eid: str, kind: str = "rectangle", version: int = 1, nonce: int = 1, **kw: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "id": eid,
        "type": kind,
        "x": 0,
        "y": 0,
        "width": 80,
        "height": 40,
        "version": version,
        "versionNonce": nonce,
        "isDeleted": False,
    }
    if kind in ("arrow", "line", "freedraw"):
        raw["points"] = [[0, 0], [20, 0]]
    raw.update(kw)
    return raw


def ids() -> Any:
    n = {"n": 0, "t": 0, "b": 0, "f": 0, "a": 0}

    def new(prefix: str) -> str:
        n[prefix] = n.get(prefix, 0) + 1
        return f"{prefix}{n[prefix]}"

    return new


def one() -> int:
    return 1


def test_the_caps_are_the_spikes() -> None:
    """A quiet change to a cap would let a board grow past what an agent can read back."""
    assert board.MAX_TEXT == 200
    assert board.MAX_CALL == 40
    assert board.MAX_ELEMENTS == 1500
    assert board.MAX_BYTES == 1_000_000
    assert board.MAX_CHANGES == 200


def test_a_higher_version_wins_and_a_tie_goes_to_the_lower_nonce() -> None:
    """The spike's rule. An equal nonce keeps what is stored, so sending the same element twice
    changes nothing. A delete is a tombstone that stays in the list and leaves the summary."""
    stored = el("a", version=2, nonce=10, text="stored")
    newer = merge_one(stored, el("a", version=3, nonce=1, text="newer"))
    assert newer["text"] == "newer"
    lower = merge_one(stored, el("a", version=2, nonce=4, text="lower"))
    assert lower["text"] == "lower"
    assert merge_of([stored], [el("a", version=2, nonce=10, text="same")]).taken == ()
    assert merge_of([stored], [el("a", version=2, nonce=11, text="higher")]).taken == ()
    gone = merge_one(stored, el("a", version=3, nonce=1, isDeleted=True))
    assert gone["isDeleted"] is True
    assert "id=a" not in board.summary([gone], 4)


def test_a_link_an_image_a_script_and_an_oversized_shape_are_not_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shapes are text. A link is forced null, an image or an embed is refused, a field the
    allowlist doesn't name (a script) is dropped, and text is cut. An unknown type fails closed."""
    kept = merge_one(
        None,
        el(
            "a",
            text="see https://example.com/x " + ("z" * 250),
            link="https://example.com/x",
            script="alert(1)",
            customData={"pwn": True},
        ),
    )
    assert kept["link"] is None
    assert "script" not in kept and "customData" not in kept
    assert kept["text"].endswith("z") and len(kept["text"]) == 200
    assert "https://example.com/x" in kept["text"]
    assert merge_of([], [el("img", type="image")]).rejected == ("image",)
    assert merge_of([], [el("e", type="iframe")]).rejected == ("image",)
    assert merge_of([], [el("pen", type="freedraw")], agent=True).rejected == ("agent type",)
    assert merge_of([], [el("nope", type="widget")]).rejected == ("agent type",)
    assert merge_of([], [el("nope", type="widget")], agent=False).rejected == ("unknown type",)
    pen = el("pen", type="freedraw", points=[[0, 0]] * 501)
    assert merge_of([], [pen], agent=False).rejected == ("oversized",)
    monkeypatch.setattr(board, "MAX_ELEMENTS", 1)
    assert merge_of([el("a")], [el("b")]).rejected == ("board full",)
    monkeypatch.setattr(board, "MAX_BYTES", 1)
    assert "board full" in merge_of([], [el("c", text="hi")]).rejected


def test_one_agent_call_may_change_forty_shapes() -> None:
    """The forty-first shape in one call is refused, and the first forty are kept."""
    got = board.merge([], [el(f"s{i}") for i in range(41)], agent=True)
    assert len(got.taken) == 40 and got.rejected == ("too many",)


def test_each_action_draws_the_smallest_change_and_an_unknown_one_does_nothing() -> None:
    """note, box, frame, connect, move, edit, delete and read. The broker mints the ids. An
    action the tool doesn't know fails closed, and a move without a place is refused."""
    new = ids()
    note = board.apply_action(
        [],
        "note",
        {"text": "<script>hi</script>", "x": 12, "y": 34},
        now=10,
        new_id=new,
        new_nonce=one,
    )
    assert note.error == "" and note.added == ("n1", "t1")
    stored = merge_all(note.incoming)
    assert stored[0]["link"] is None and "<script>" in stored[1]["text"]
    assert board.apply_action([], "note", {}, now=10, new_id=new, new_nonce=one).error == "text is required"
    box = board.apply_action([], "box", {"shape": "ellipse"}, now=10, new_id=new, new_nonce=one)
    assert box.added == ("b1",) and box.incoming[0]["type"] == "ellipse"
    assert board.apply_action([], "box", {"shape": "image"}, now=10, new_id=new, new_nonce=one).error == (
        "unknown shape"
    )
    frame = board.apply_action([], "frame", {"text": "Plan"}, now=10, new_id=new, new_nonce=one)
    assert frame.incoming[0]["type"] == "frame" and frame.incoming[0]["name"] == "Plan"
    second = board.apply_action(
        [], "note", {"text": "there", "x": 200, "y": 34}, now=10, new_id=new, new_nonce=one
    )
    both = stored + merge_all(second.incoming)
    arrow = board.apply_action(
        both, "connect", {"id": "n1", "to": "n2", "text": "then"}, now=10, new_id=new, new_nonce=one
    )
    assert arrow.added[0] == "a1"
    drawn = both + merge_all(arrow.incoming)
    moved = board.apply_action(
        drawn, "move", {"id": "n1", "x": 40, "y": 50}, now=11, new_id=new, new_nonce=one
    )
    assert moved.error == "" and moved.incoming[0]["x"] == 40
    label = next(e for e in moved.incoming if e["type"] == "text")
    shape = moved.incoming[0]
    # the bound text moves with the note and stays centred in it
    assert label["x"] + label["width"] / 2 == shape["x"] + shape["width"] / 2
    assert label["y"] + label["height"] / 2 == shape["y"] + shape["height"] / 2
    assert board.apply_action(drawn, "move", {"id": "n1"}, now=11, new_id=new, new_nonce=one).error == (
        "x and y are required"
    )
    edited = board.apply_action(
        drawn, "edit", {"id": "n1", "text": "hello"}, now=12, new_id=new, new_nonce=one
    )
    assert edited.incoming[0]["text"] == "hello" and edited.added == ("n1",)
    deleted = board.apply_action(drawn, "delete", {"id": "n1"}, now=13, new_id=new, new_nonce=one)
    assert deleted.error == "" and all(e["isDeleted"] for e in deleted.incoming)
    assert board.apply_action(drawn, "image", {}, now=10, new_id=new, new_nonce=one).error == "unknown action"
    assert board.apply_action(drawn, "read", {}, now=10, new_id=new, new_nonce=one).summary.startswith(
        "board rev 0:"
    )


@pytest.mark.parametrize("origin", [(0, 0), (-400, 250)])
def test_auto_placed_notes_stay_inside_their_frame_and_avoid_siblings(origin: tuple[int, int]) -> None:
    """The enclosing frame is not an obstacle; its notes still occupy their own slots (#674)."""
    new = ids()
    result = board.apply_action(
        [], "frame", {"text": "Plan", "x": origin[0], "y": origin[1]}, now=10, new_id=new
    )
    frame = result.incoming[0]
    elements = list(result.incoming)
    siblings: list[dict[str, Any]] = []
    for text in ("first", "second", "third", "fourth"):
        result = board.apply_action(elements, "note", {"to": frame["id"], "text": text}, now=10, new_id=new)
        assert not result.error
        note, label = result.incoming
        assert note["frameId"] == label["frameId"] == frame["id"]
        assert frame["x"] <= note["x"] <= frame["x"] + frame["width"] - note["width"]
        assert frame["y"] <= note["y"] <= frame["y"] + frame["height"] - note["height"]
        for sibling in siblings:
            assert (
                note["x"] >= sibling["x"] + sibling["width"] + 10
                or sibling["x"] >= note["x"] + note["width"] + 10
                or note["y"] >= sibling["y"] + sibling["height"] + 10
                or sibling["y"] >= note["y"] + note["height"] + 10
            )
        siblings.append(note)
        elements.extend(result.incoming)


def test_the_summary_names_frames_shapes_and_arrows() -> None:
    """The text an agent reads, in the shape the spike measured. Tombstones are left out."""
    frame = el("f1", "frame", name="Plan", x=0, y=0, width=600, height=360)
    box = el("n1", text="", x=20, y=40, boundElements=[{"id": "t1", "type": "text"}], frameId="f1")
    label = el("t1", "text", text="start", x=30, y=50, containerId="n1", frameId="f1")
    arrow = el(
        "a1",
        "arrow",
        startBinding={"elementId": "n1", "focus": 0, "gap": 1},
        endBinding={"elementId": "n2", "focus": 0, "gap": 1},
    )
    other = el("n2", x=300, y=40)
    text = board.summary([frame, box, label, other, arrow], 7)
    lines = text.splitlines()
    assert lines[0] == "board rev 7: 1 frames, 2 shapes, 1 arrows, 0 loose texts"
    assert lines[1] == 'frame 1 id=f1 "Plan" at (0,0) 600x360'
    assert 'rectangle id=n1 "start" at (20,40) 80x40 in f1' in text
    assert 'arrow id=a1 n1 -> n2 ""' in text


def merge_of(current: list[dict[str, Any]], incoming: list[Any], *, agent: bool = True) -> board.MergeResult:
    return board.merge(current, incoming, agent=agent)


def merge_one(current: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    got = merge_of([] if current is None else [current], [incoming])
    assert len(got.taken) == 1, got.rejected
    return got.taken[0]


def merge_all(incoming: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    got = merge_of([], list(incoming))
    assert not got.rejected, got.rejected
    return list(got.elements)


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> Store:
    return Store(db.open_db(tmp_path / "y.db"), clock)


def test_a_change_records_who_and_when_and_a_room_delete_takes_the_board(
    store: Store, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last 200 changes are kept: who, when, and which ids, not a copy of the drawing. Deleting
    the room deletes the board with it, or the row-count check on delete would fail."""
    room = store.create_room("#build", "alice", 60, 6)
    assert store.board_load(room.id) == (0, [])
    monkeypatch.setattr(board, "MAX_BYTES", 10)
    with pytest.raises(Conflict):
        store.board_write(room.id, 1, [{"id": "n1", "text": "too big for the patched cap"}], "alice", ["n1"])
    assert store.board_load(room.id) == (0, [])
    monkeypatch.setattr(board, "MAX_BYTES", 1_000_000)
    monkeypatch.setattr(board, "MAX_CHANGES", 2)
    store.board_write(room.id, 1, [{"id": "n1"}], "alice", ["n1"])
    clock.advance(5)
    store.board_write(room.id, 2, [{"id": "n1"}, {"id": "n2"}], "bob", ["n2"])
    clock.advance(5)
    store.board_write(room.id, 3, [{"id": "n1"}, {"id": "n2"}, {"id": "n3"}], "carol", ["n3"])
    changes = store.board_changes(room.id)
    assert [c["seq"] for c in changes] == [2, 3]
    assert changes[0]["actor"] == "bob" and changes[0]["at"] == clock.t - 5 and changes[0]["ids"] == ["n2"]
    assert changes[1]["actor"] == "carol" and changes[1]["at"] == clock.t and changes[1]["ids"] == ["n3"]
    seq, els = store.board_load(room.id)
    assert seq == 3 and [e["id"] for e in els] == ["n1", "n2", "n3"]
    counts = db.row_counts(store.con, db.TABLES)
    store.delete_room(
        room.id, name=room.name, created_at=room.created_at, expect_counts=counts, event={"by": "alice"}
    )
    assert store.board_load(room.id) == (0, [])
    assert store.board_changes(room.id) == []
