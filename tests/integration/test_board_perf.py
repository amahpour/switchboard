"""How long the layout checks take on a full board (§56.5).

Every write runs them inside its database transaction on the broker's event loop, so a slow check
stalls every page and agent. This fills a board to its 1,500 element cap and fails if checking one
touched shape costs more than a share of the time to serialize the same board, which every write
already does for the byte cap. The share is relative to the machine, as in ``test_home_perf``: a slow
runner does not fail it, and a check that visits every pair again does (about 20 times over)."""

from __future__ import annotations

import json
import statistics
import time
from typing import Any

from switchboard import board, board_checks

BOXES = 370  # a shape and its label each, and as many arrows with a label each: 1,480 elements
MAX_SHARE = 2.0


def full_board() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    n = {"n": 0}

    def new(prefix: str) -> str:
        n["n"] += 1
        return f"{prefix}{n['n']}"

    def nonce() -> int:
        return 1

    def add(action: str, **params: Any) -> list[str]:
        got = board.apply_action(out, action, params, now=1, new_id=new, new_nonce=nonce)
        assert not got.error, got.error
        out.extend(got.incoming)
        return list(got.added)

    boxes = [add("box", text=f"step {i}", x=(i % 20) * 300, y=(i // 20) * 200)[0] for i in range(BOXES)]
    for i in range(BOXES - 1):
        add("connect", id=boxes[i], to=boxes[i + 1], text="next")
    add("connect", id=boxes[0], to=boxes[-1], text="loop")
    return out


def test_checking_one_touched_shape_on_a_full_board_stays_cheap() -> None:
    """A write on a 1,480-element board hears only about what it touched, in a fraction of the time
    that serializing the board takes."""
    elements = full_board()
    assert 1400 <= len(elements) <= board.MAX_ELEMENTS
    touched = next(e["id"] for e in elements if e["type"] == "rectangle")
    narrow = board_checks.warnings(elements, only={touched})
    assert all(touched in w for w in narrow), narrow  # only what names the touched shape
    reference, checks = [], []
    for _ in range(7):
        t = time.perf_counter()
        json.dumps(elements, separators=(",", ":"), ensure_ascii=False)
        reference.append(time.perf_counter() - t)
        t = time.perf_counter()
        board_checks.warnings(elements, only={touched})
        checks.append(time.perf_counter() - t)
    share = statistics.median(checks) / statistics.median(reference)
    assert share < MAX_SHARE, f"a check took {share:.1f}x the time to serialize the board"
