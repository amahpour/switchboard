"""Every module's docstring names the DESIGN.md section it implements (CLAUDE.md, "The shape to
keep"; #173 to #176 added the last four that didn't). A static check, so a new module without
one fails here rather than in a review."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "switchboard"


def test_every_module_docstring_names_a_design_section() -> None:
    missing = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            continue  # an empty package marker has nothing to describe
        doc = ast.get_docstring(ast.parse(text)) or ""
        if "DESIGN.md §" not in doc:
            missing.append(str(path.relative_to(SRC)))
    assert missing == [], f"name a DESIGN.md section (DESIGN.md §N) in these modules' docstrings: {missing}"
