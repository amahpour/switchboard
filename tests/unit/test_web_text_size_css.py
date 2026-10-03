"""Every fixed type size must follow the chosen text-size step (DESIGN.md §34)."""

from __future__ import annotations

import re
from pathlib import Path

STYLE = Path(__file__).resolve().parents[2] / "src" / "switchboard" / "web" / "static" / "style.css"


def test_fixed_font_sizes_use_the_shared_scale() -> None:
    """A bare px declaration would leave some part of the UI small at the Larger step."""
    css = STYLE.read_text()
    declarations = re.findall(r"(?<![-\w])font(?:-size)?\s*:\s*([^;]+);", css)
    fixed = [value for value in declarations if re.search(r"\d+(?:\.\d+)?px", value)]
    assert fixed
    assert all("var(--text-scale)" in value for value in fixed)
