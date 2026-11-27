"""Spacing, type and corners come from style.css's tokens (src/switchboard/web/UI.md, #995).

When #995 measured the stylesheet, its rules had picked 28 paddings, 22 margins, 15 gaps, 19 corner radii
and 18 font sizes of their own. The tokens (``--sp-*``, ``--fs-*``, ``--r-*``, ``--ctl-h``, ``--target``)
now sit in its first section. This test counts the raw pixel values left in the properties the tokens cover,
outside that section, and holds the count where it is. A rule that adds one fails. A change that converts
rules to tokens lowers ``RAW_PX`` to the new count, the way ``test_tool_list_fits_the_review_budget`` moves
the tool list's cap down. A value that has to stay raw (a hairline, an icon's own box) says why in a
comment, and is still counted.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[2] / "src" / "switchboard" / "web" / "static" / "style.css"
TOKENS = (
    "--sp-1", "--sp-2", "--sp-3", "--sp-4", "--sp-5", "--sp-6",
    "--fs-xs", "--fs-sm", "--fs-md", "--fs-lg", "--fs-xl", "--fs-2xl",
    "--r-sm", "--r-md", "--r-lg", "--r-pill", "--ctl-h", "--target",
)  # fmt: skip
COVERED = re.compile(
    r"(?<![-\w])(padding(?:-[a-z]+)*|margin(?:-[a-z]+)*|gap|row-gap|column-gap|font-size|font"
    r"|border(?:-[a-z]+)*-radius)\s*:\s*([^;{}]+)"
)
PX = re.compile(r"(?<![\w.])(\d*\.?\d+)px\b")

# The raw pixel values left in padding, margin, gap, font size and corner radius. Only goes down (UI.md).
RAW_PX = 1212


def _rules(css: str) -> str:
    """The stylesheet without its comments, and without the tokens' own ``:root`` block."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    start = css.index(":root {")
    return css[:start] + css[css.index("}", start) + 1 :]


def raw_px(css: str) -> int:
    """How many non-zero pixel values the covered properties still hold."""
    return sum(1 for m in COVERED.finditer(_rules(css)) for v in PX.findall(m.group(2)) if float(v) != 0)


def test_the_tokens_are_in_the_first_root_block() -> None:
    """Every token UI.md names is defined where the stylesheet's tokens live. Fails if one is renamed or
    dropped, which would leave the rules that use it with no value at all."""
    css = CSS.read_text(encoding="utf-8")
    start = css.index(":root {")
    root = css[start : css.index("}", start)]
    assert [t for t in TOKENS if t + ":" not in root] == []


def test_raw_pixel_values_only_go_down() -> None:
    """The count of raw pixel values is exactly ``RAW_PX``. More means a new rule skipped the tokens; fewer
    means a change converted some and should lower ``RAW_PX``, so the next one can't spend the room."""
    n = raw_px(CSS.read_text(encoding="utf-8"))
    assert n <= RAW_PX, f"{n - RAW_PX} new raw pixel values: use the tokens (src/switchboard/web/UI.md)"
    assert n == RAW_PX, f"{RAW_PX - n} fewer raw pixel values than RAW_PX: lower RAW_PX to {n}"


def test_the_count_sees_what_it_should() -> None:
    """The counter itself: raw values in each covered property count, tokens and zero don't, and the tokens'
    own block is left out. Fails if the pattern stops seeing a property, as a regex easily does."""
    root = ":root { --sp-1: 4px; --fs-md: calc(13px * var(--text-scale)); }\n"
    assert raw_px(root) == 0
    assert raw_px(root + "b { padding: 6px 10px; }") == 2
    assert raw_px(root + "b { padding: var(--sp-2) var(--sp-3); gap: var(--sp-1); }") == 0
    assert raw_px(root + "b { font: calc(13px * var(--text-scale))/1.45 var(--font); }") == 1
    assert raw_px(root + "b { font-size: var(--fs-md); font-weight: 600; }") == 0
    assert raw_px(root + "b { margin-inline-start: -4px; margin: 0px; }") == 1
    assert raw_px(root + "b { border-top-left-radius: 6px; border-radius: var(--r-md); }") == 1
    assert raw_px(root + "b { width: 120px; top: 4px; }") == 0  # not covered: sizes and positions
    assert raw_px(root + "/* padding: 4px */ b { color: red; }") == 0
