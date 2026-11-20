"""A secret on the whiteboard is cut out where its text is read (DESIGN.md §11 item 10, §56, #893).

``board._text`` reads every string that can reach a stored shape: ``text``, ``originalText`` and a frame's
``name``. Each test below draws a secret through one way text gets onto the board and checks the stored
shapes. What these catch: a field stored as written (the secret then reaches the database, the page and
every reader), a wrapped copy of a label that hides the secret from the patterns, a marker that pushes a
value past the limit and is stored cut in half, and a refusal that names the value. Every secret is built
from pieces, so no scanner reads this file as a leak."""

from __future__ import annotations

import json
from typing import Any

import pytest

from switchboard import board, board_flow

TOKEN = "ghp" + "_" + "aB3" * 12
PASSWORD = "password=" + "hunter2" * 2
GH = "[redacted: github token]"
PW = "password=[redacted: password or token]"


def stored_text(elements: list[dict[str, Any]]) -> str:
    """Every string of every stored shape, as one blob: a secret anywhere in it fails the test."""
    return json.dumps(elements)


def shape(**over: Any) -> dict[str, Any]:
    el: dict[str, Any] = {
        "id": "a",
        "type": "rectangle",
        "x": 0,
        "y": 0,
        "width": 100,
        "height": 50,
        "version": 1,
        "versionNonce": 1,
    }
    el.update(over)
    return el


def act(elements: list[dict[str, Any]], action: str, **params: Any) -> list[dict[str, Any]]:
    """One agent action, merged as the broker merges it; the stored elements."""
    got = board.apply_action(elements, action, params, now=10)
    assert not got.error, got.error
    merged = board.merge(elements, list(got.incoming), agent=True)
    assert not merged.rejected, merged.rejected
    return list(merged.elements)


def assert_clean(elements: list[dict[str, Any]]) -> None:
    blob = stored_text(elements)
    assert TOKEN not in blob and "hunter2" not in blob
    assert GH in blob or PW in blob


@pytest.mark.parametrize("action", ["note", "box", "text"])
def test_the_text_of_a_drawn_shape_or_loose_text_is_redacted(action: str) -> None:
    """``text`` and ``originalText`` of a note, a box's label and a loose text element. Fails if an action's
    words are stored as written."""
    els = act([], action, text=f"deploy {TOKEN} now")
    texts = [e for e in els if e["type"] == "text"]
    assert texts and all(e["text"] == f"deploy {GH} now" and e["originalText"] == e["text"] for e in texts)
    assert_clean(els)


def test_a_frames_name_is_redacted() -> None:
    """A frame's title is its ``name``, a third text field. Fails if it is stored as written."""
    els = act([], "frame", text=f"{PASSWORD} frame")
    assert next(e for e in els if e["type"] == "frame")["name"] == f"{PW} frame"
    assert_clean(els)


def test_an_arrows_label_is_redacted() -> None:
    """``connect`` puts its label on the arrow. Fails if the label is stored as written."""
    els = act([], "note", text="one")
    els = act(els, "note", text="two", x=300, y=0)
    a, b = [e["id"] for e in els if e["type"] == "rectangle"]
    els = act(els, "connect", id=a, to=b, text=f"uses {TOKEN}")
    label = next(e for e in els if e["type"] == "text" and e["containerId"] not in (a, b))
    assert label["text"] == f"uses {GH}" == label["originalText"]
    assert_clean(els)


def test_an_edit_redacts_a_shapes_label_a_loose_text_and_a_frame() -> None:
    """``edit`` writes new words into all three. Fails if an edit is stored as written."""
    els = act([], "note", text="plain")
    els = act(els, "text", text="loose", x=0, y=300)
    els = act(els, "frame", text="frame", x=0, y=600)
    for el in [e for e in els if e["type"] in ("rectangle", "text", "frame") and not e.get("containerId")]:
        els = act(els, "edit", id=el["id"], text=f"now {TOKEN}")
    assert_clean(els)
    assert stored_text(els).count(GH) == 5  # a label and a loose text, each in two fields, and the frame


def test_a_page_shape_is_redacted_in_every_text_field_and_customdata_is_dropped() -> None:
    """A person's page sends whole elements: ``text``, ``originalText`` and ``name`` are read like an
    agent's, and a field the cleaner does not know (``customData``, a ``link``) is never stored."""
    sent = [
        shape(id="s", text=f"{TOKEN}", originalText=f"{TOKEN}", name=PASSWORD, link=f"https://x/{TOKEN}"),
        shape(id="t", type="text", text=PASSWORD, originalText=PASSWORD, customData={"note": TOKEN}),
    ]
    merged = board.merge([], sent, agent=False)
    assert not merged.rejected
    els = list(merged.elements)
    assert_clean(els)
    s, t = els
    assert (s["text"], s["originalText"], s["name"]) == (GH, GH, PW)
    assert (t["text"], t["originalText"]) == (PW, PW)
    assert s["link"] is None and "customData" not in t


def test_a_label_wrapped_across_lines_cannot_hide_a_secret_in_its_text() -> None:
    """Excalidraw keeps ``text`` as ``originalText`` wrapped to the shape's width, and a wrap breaks a long
    token or a ``Bearer`` and its token across lines, where no pattern matches. ``text`` is read unwrapped as
    well. Fails if the wrapped copy keeps the secret while ``originalText`` is clean."""
    token = "sk-" + "a1b2c3d4e5f6" * 3
    broken = token[:14] + "\n" + token[14:]
    bearer = "Bearer\n" + "a1b2c3d4e5f6" * 3
    for wrapped in (broken, bearer, f"see {PASSWORD[:12]}\n{PASSWORD[12:]}"):
        el = shape(type="text", text=wrapped, originalText=wrapped.replace("\n", ""))
        (out,) = board.merge([], [el], agent=False).elements
        assert "a1b2c3d4e5f6" not in out["text"] and "hunter2" not in out["text"], out["text"]
    plain = shape(type="text", text="two\nlines", originalText="two lines")
    assert board.merge([], [plain], agent=False).elements[0]["text"] == "two\nlines"


def test_mermaid_labels_edges_and_subgraph_titles_are_redacted() -> None:
    """A diagram's node labels, an edge's label and a subgraph's title become shapes through the same
    cleaner. Fails if one is stored as written, and shows the diagram still draws with a marker in it."""
    source = f'flowchart LR\nsubgraph G["group {TOKEN}"]\nA["a {TOKEN}"] -->|"edge {TOKEN}"| B[plain]\nend'
    flow = board_flow.convert(source)
    els = board.mermaid_elements([], flow.elements, now=10)
    assert_clean(els)
    names = [e.get("originalText") or e.get("name") for e in els]
    assert f"a {GH}" in names and f"edge {GH}" in names and f"group {GH}" in names and "plain" in names
    assert len([e for e in els if e["type"] == "arrow"]) == 1


def test_a_mermaid_secret_in_a_node_id_or_unquoted_label_is_redacted() -> None:
    """A node's id is its label when it has none, and an unquoted label is read as written. Fails if the
    id's text reaches the board."""
    els = board.mermaid_elements(
        [], board_flow.convert(f"flowchart TD\n{TOKEN} --> B[{PASSWORD}]").elements, now=10
    )
    blob = stored_text(els)
    assert TOKEN not in blob and "hunter2" not in blob


def test_a_marker_that_would_push_a_value_past_the_limit_is_refused_naming_no_value() -> None:
    """A value that fits and that redaction lengthens past ``MAX_TEXT`` is refused, saying the kind and
    never the value: stored cut it would hold half a marker. Fails if it comes back stored (cut or whole)."""
    value = "x" * (board.MAX_TEXT - len(PASSWORD) - 1) + " " + PASSWORD
    assert len(value) <= board.MAX_TEXT
    got = board.apply_action([], "note", {"text": value}, now=10)
    assert "secret" in got.error and "password or token" in got.error and "hunter2" not in got.error
    page = shape(text=value, originalText=value)
    merged = board.merge([], [page], agent=False)
    assert merged.elements == () and "password or token" in merged.rejected[0]
    assert "hunter2" not in merged.rejected[0]


def test_text_over_the_limit_is_cut_after_redaction_so_no_half_secret_is_left_at_the_cut() -> None:
    """Text already too long is cut as before, but after the secret is cut out: a secret that straddles
    character 200 is gone, not half there. Fails if the cut runs first."""
    text = "x" * (board.MAX_TEXT - 20) + " " + TOKEN + " tail"
    el = shape(type="text", text=text, originalText=text)
    (out,) = board.merge([], [el], agent=False).elements
    assert len(out["text"]) == board.MAX_TEXT and "ghp_" not in out["text"] and "aB3aB3" not in out["text"]


def test_redacting_twice_changes_nothing() -> None:
    """An action's text is read in the action and again in the merge: a marker is never redacted again, so
    the second read is a no-op. Fails if a marker is mangled or refused on the second pass."""
    once = act([], "note", text=f"{TOKEN} {PASSWORD}")
    assert_clean(once)
    twice = board.merge(once, [e for e in once], agent=True)
    assert not twice.rejected and list(twice.elements) == once


def test_a_mermaid_label_is_redacted_before_it_is_cut_so_a_token_across_the_cut_is_gone() -> None:
    """The decoder cut a label at ``MAX_TEXT`` before anyone read it, so a token that crossed character 200
    kept a prefix too short to match (#893). A node label, an edge label and a subgraph title each carry one.
    Fails if the cut runs before the redaction: the first 19 characters of the token reach the board."""
    pad = "x" * (board.MAX_TEXT - 21) + " "
    source = f'flowchart LR\nsubgraph G["{pad}{TOKEN}"]\nA["{pad}{TOKEN}"] -->|"{pad}{TOKEN}"| B[b]\nend'
    els = board.mermaid_elements([], board_flow.convert(source).elements, now=10)
    blob = stored_text(els)
    assert "ghp_" not in blob and "aB3aB3" not in blob and "[redacted" in blob


def test_a_wrapped_text_whose_first_fragment_matches_alone_is_read_whole_and_unmutated() -> None:
    """``text`` is read before it is changed or cut. A break inside a long ``sk-`` token leaves a first
    fragment that matches by itself, and a line-by-line reading replaces only that and keeps the rest;
    the unwrapped reading sees the whole token. Fails if the rest of the token is stored."""
    body = "a1b2c3d4e5f6" * 4
    wrapped = f"sk-{body[:24]}\n{body[24:]}"
    el = shape(type="text", text=wrapped, originalText=wrapped.replace("\n", ""))
    (out,) = board.merge([], [el], agent=False).elements
    assert out["text"] == "[redacted: api key]" == out["originalText"]
    assert body[24:] not in stored_text([out])


def test_a_wrapped_text_that_expands_past_the_cap_is_refused_like_any_other() -> None:
    """A short wrapped value whose marker makes it longer than ``MAX_TEXT`` is refused, not silently cut
    (the same rule as an unwrapped value). Fails if the wrapped copy comes back cut at 200."""
    value = "x" * (board.MAX_TEXT - 30) + " " + "password=hunter\n2hunter2"
    assert len(value) <= board.MAX_TEXT
    merged = board.merge([], [shape(type="text", text=value, originalText="plain")], agent=False)
    assert merged.elements == () and "password or token" in merged.rejected[0]
    assert "hunter" not in merged.rejected[0]


@pytest.mark.parametrize(
    "field",
    ["id", "groupIds", "frameId", "containerId", "boundElements", "startBinding", "endBinding", "index"],
)
def test_an_id_or_reference_that_is_a_secret_is_refused_naming_the_kind(field: str) -> None:
    """An id, a group id, a reference or a fractional ``index`` is stored as written and goes into the
    change log and an agent's summary, and a complete token fits its character check. One in which
    ``redact`` finds a kind drops the element, naming the kind and never the value. Fails if the token is
    stored (or only dropped from a list, keeping the element)."""
    tok = "ghp_" + "aB3" * 12
    values: dict[str, Any] = {
        "id": tok,
        "groupIds": [tok],
        "frameId": tok,
        "containerId": tok,
        "boundElements": [{"id": tok, "type": "arrow"}],
        "startBinding": {"elementId": tok, "focus": 0, "gap": 1},
        "endBinding": {"elementId": tok, "focus": 0, "gap": 1},
        "index": "AKIA" + "ABCDEFGHIJKLMNOP",
    }
    base = shape(type="arrow", points=[[0, 0], [5, 5]])
    el = {**base, field: values[field]} if field != "id" else {**base, "id": tok}
    merged = board.merge([], [el], agent=False)
    assert merged.elements == () and "secret" in merged.rejected[0]
    assert "ghp_" not in merged.rejected[0] and "AKIA" not in merged.rejected[0]
    assert board.merge([], [base], agent=False).elements  # an ordinary id is untouched
