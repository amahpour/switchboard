"""Every release note in changes/ has a CHANGELOG heading (changes/README.md).

Text above the first heading opens the release's section, as its introduction. A note that
is only that text, with no heading at all, was meant as an entry: twice (#148, #160) one would
have printed as the release's introduction. Catch it in the pull request that adds it."""

from __future__ import annotations

import re
from pathlib import Path

CHANGES = Path(__file__).resolve().parents[2] / "changes"


def test_every_note_has_a_changelog_heading() -> None:
    notes = [p for p in sorted(CHANGES.glob("*.md")) if p.name != "README.md"]
    missing = [p.name for p in notes if not re.search(r"^### \S", p.read_text(), re.M)]
    assert not missing, f"no '### Added/Changed/Fixed…' heading in: {', '.join(missing)}"
