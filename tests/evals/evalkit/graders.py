"""The checks (DESIGN.md §12.7): plain functions of a ``RunRecord``. Test-only.

A check says pass or fail from who posted, who passed and what was delivered, read from the database. It
never reads what an agent meant, and no model judges anything. ``asked`` is the id of the person's message
the check is about; "after" means after that message. A scenario names a check and gives it its arguments
(``scenario.py``); ``tests/integration/test_evals_graders.py`` holds, for each one here, a scripted agent
that fails it and one that passes it, so a check that can't fail is found.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .record import RunRecord


@dataclass(frozen=True)
class Result:
    ok: bool
    detail: str  # what was seen, for the results file and a failed check


def _names(xs: list[str]) -> str:
    return ", ".join(xs) if xs else "nobody"


def poster_count(rec: RunRecord, asked: int, *, n: int) -> Result:
    """Exactly ``n`` different agents posted after the message. Fails when more answer (the double answer
    this exists to count) and when fewer do (nobody answered, which is not the same failure)."""
    posters = rec.posters_after(asked)
    return Result(len(posters) == n, f"{len(posters)} posted: {_names(posters)}")


def agents_post(rec: RunRecord, asked: int, *, agents: list[str]) -> Result:
    """Every named agent posted at least once after the message. Fails for the one that stayed silent
    or only passed."""
    posters = rec.posters_after(asked)
    missing = [a for a in agents if a not in posters]
    return Result(
        not missing, f"posted: {_names(posters)}" + (f"; silent: {_names(missing)}" if missing else "")
    )


def agents_do_not_post(rec: RunRecord, asked: int, *, agents: list[str]) -> Result:
    """None of the named agents posted after the message (passing is fine). Fails for one that did."""
    posters = rec.posters_after(asked)
    spoke = [a for a in agents if a in posters]
    return Result(
        not spoke, f"posted: {_names(posters)}" + (f"; should not have: {_names(spoke)}" if spoke else "")
    )


def a_post_matches(rec: RunRecord, asked: int, *, pattern: str) -> Result:
    """Some agent's post after the message matches the regular expression (case-insensitive): the answer
    is the right one, so a room where somebody posted something else doesn't pass 'somebody answered'."""
    rx = re.compile(pattern, re.IGNORECASE)
    hit = next((m for m in rec.posts_after(asked) if rx.search(m.text)), None)
    return Result(
        hit is not None, f"{hit.sender} matched /{pattern}/" if hit else f"no post matched /{pattern}/"
    )


GRADERS: dict[str, Callable[..., Result]] = {
    fn.__name__: fn for fn in (poster_count, agents_post, agents_do_not_post, a_post_matches)
}
