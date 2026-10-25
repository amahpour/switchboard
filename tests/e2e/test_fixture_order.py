"""The broker outlives the pages (#527): a function-scoped broker fixture must be set up before the
``ui`` fixture that closes the pages, so pytest tears it down after them.

Marker ``e2e``. Pytest tears fixtures down in the reverse of the order it set them up in, and sets
them up in the order a test names them unless one needs another. When ``ui`` did not need the broker
and a test named ``ui`` first, the broker stopped while the page was still open; the page's closed
socket made it ask ``/api/me`` 500 ms later, the port was dead, and the browser logged a console
error that ``ui`` then reported as a failure, depending on whether the pages were closed within
those 500 ms. A module-scoped broker is torn down after every function-scoped fixture, so it is
safe whatever the order. This checks the dependency for every collected test, so it fails for the
next ``ui`` fixture written without its broker as a parameter, without waiting for the race."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

BROKER_FIXTURES = ("hosted", "google", "world")
UI_FIXTURES = ("ui", "hosted_ui")


def test_a_function_scoped_broker_is_set_up_before_the_pages_so_it_stops_after_them(
    request: pytest.FixtureRequest,
) -> None:
    """Fails if a collected test has a ``ui`` fixture that doesn't need the function-scoped broker
    the test also uses, so the broker could stop while the pages are still open."""
    wrong = set()
    for item in request.session.items:
        defs = item._fixtureinfo.name2fixturedefs  # type: ignore[attr-defined]
        for ui_name in UI_FIXTURES:
            if ui_name not in defs:
                continue
            ui_needs = defs[ui_name][-1].argnames
            for broker in BROKER_FIXTURES:
                if broker in defs and defs[broker][-1].scope == "function" and broker not in ui_needs:
                    wrong.add(f"{item.nodeid}: {ui_name} doesn't take {broker}")
    assert not wrong, "a broker could stop before its pages close:\n  " + "\n  ".join(sorted(wrong))
