"""No browser test or preview script waits with ``page.wait_for_function``: Playwright compiles its
predicate with ``eval`` in the page, which switchboard's CSP refuses at random (a flaky
``EvalError``). ``ui_world.wait_js`` polls through ``page.evaluate`` instead."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_nothing_waits_with_wait_for_function() -> None:
    found = []
    for base in (ROOT / "tests", ROOT / "docs" / "media"):
        for path in sorted(base.rglob("*.py")):
            if path.name in ("test_no_eval_waits.py", "ui_world.py"):
                continue  # this guard, and wait_js's docstring, name it on purpose
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "wait_for_function(" in line:
                    found.append(f"{path.relative_to(ROOT)}:{n}")
    assert found == [], f"use ui_world.wait_js, not page.wait_for_function: {found}"
