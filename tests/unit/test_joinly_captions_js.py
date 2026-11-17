"""The captions reader's page script (``READ_JS`` in ``deploy/joinly/captions.py``), run in node against a
fake caption box shaped like Zoom's (#810). A live call on 2026-11-17 showed that for a speaker with a profile
picture the picture *is* the row's icon element (``<img class="zmu-data-selector-item__icon" src=...>``),
with no initials and no name; the first version looked for an ``<img>`` inside the icon, found none, and
returned no picture id, so the speaker stayed unnamed. These tests pin both shapes.

Skipped where node is missing, except under SWITCHBOARD_REQUIRE_NODE=1 (CI), where that fails."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from conftest import node_for_tests

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("sb_captions_js", ROOT / "deploy" / "joinly" / "captions.py")
assert spec and spec.loader
cap = importlib.util.module_from_spec(spec)
sys.modules["sb_captions_js"] = cap
spec.loader.exec_module(cap)

NODE, pytestmark = node_for_tests()

# A minimal DOM: just what READ_JS touches (querySelector(All) by class or tag, parentElement, tagName,
# textContent, getAttribute, outerHTML, currentSrc) and a getComputedStyle stub.
HARNESS = r"""
function el(tag, attrs, children, text) {
  const e = {tagName: tag.toUpperCase(), attrs: attrs || {}, children: children || [], parentElement: null,
             textContent: text || ''};
  for (const c of e.children) { c.parentElement = e; e.textContent += c.textContent; }
  e.getAttribute = (k) => (k in e.attrs ? e.attrs[k] : null);
  e.currentSrc = e.attrs.src || '';
  e.outerHTML = '<' + tag + '>';
  const all = () => e.children.flatMap((c) => [c, ...c._all()]);
  e._all = all;
  const match = (c, sel) => sel.startsWith('.') ? (c.attrs.class || '').split(' ').includes(sel.slice(1))
                                                 : c.tagName === sel.toUpperCase();
  e.querySelectorAll = (sel) => all().filter((c) => match(c, sel));
  e.querySelector = (sel) => all().find((c) => match(c, sel)) || null;
  return e;
}
const rows = JSON.parse(process.argv[1]).map((r) => {
  const icon = r.shape === 'img'
    ? el('img', {class: 'zmu-data-selector-item__icon', src: r.src, alt: '', 'aria-hidden': 'true'})
    : r.shape === 'nested'
      ? el('span', {class: 'zmu-data-selector-item__icon'}, [el('img', {src: r.src, alt: ''})])
      : el('span', {class: 'zmu-data-selector-item__icon'}, [], r.initials);
  return el('div', {}, [icon, el('span', {class: 'live-transcription-subtitle__item'}, [], r.text)]);
});
const box = el('div', {class: 'live-transcription-subtitle__box'}, rows);
globalThis.document = {querySelector: (sel) => (sel === '.live-transcription-subtitle__box' ? box : null)};
globalThis.getComputedStyle = () => ({backgroundColor: 'rgb(1, 2, 3)'});
const read = eval(process.argv[2]);
process.stdout.write(JSON.stringify(read()));
"""

AVATAR_A = "https://example.com/avatars/a.png?size=64"
AVATAR_B = "https://example.com/avatars/b.png?size=64"


def read(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    assert NODE
    out = subprocess.run(
        [NODE, "-e", HARNESS, json.dumps(rows), cap.READ_JS],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(out.stdout)


def test_a_picture_that_is_the_icon_itself_gets_a_picture_id() -> None:
    """Zoom's shape on 2026-11-17: the icon element is the <img>. Fails if READ_JS looks only inside it."""
    (item,) = read([{"shape": "img", "src": AVATAR_A, "text": "hello there"}])
    assert item["i"] == ""
    assert item["n"] == ""
    assert len(item["p"]) == 12 and int(item["p"], 16) >= 0
    assert item["t"] == "hello there"


def test_a_picture_inside_the_icon_still_gets_one() -> None:
    (item,) = read([{"shape": "nested", "src": AVATAR_A, "text": "x"}])
    assert len(item["p"]) == 12


def test_the_id_is_stable_per_picture_and_differs_between_pictures() -> None:
    a1, a2, b = read(
        [
            {"shape": "img", "src": AVATAR_A, "text": "one"},
            {"shape": "nested", "src": AVATAR_A, "text": "two"},
            {"shape": "img", "src": AVATAR_B, "text": "three"},
        ]
    )
    assert a1["p"] == a2["p"]
    assert a1["p"] != b["p"]


def test_the_picture_address_is_not_in_the_id_fields() -> None:
    (item,) = read([{"shape": "img", "src": AVATAR_A, "text": "x"}])
    assert "example.com" not in json.dumps({k: v for k, v in item.items() if k != "h"})


def test_an_initials_row_has_no_picture_id() -> None:
    (item,) = read([{"shape": "initials", "initials": "AB", "text": "hi"}])
    assert item["i"] == "AB"
    assert item["p"] == ""
