"""Labels that are never cut short (#995): the member panel's buttons at every text size, on a phone and on a
desktop, and the phone's Members button. A console error, a page error or a CSP violation fails the test
(``test_web_ui.UI``). Marker ``e2e``."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect
from test_web_ui import PHONE, UI, ui, world  # noqa: F401

pytestmark = pytest.mark.e2e

SIZES = ("small", "default", "large", "larger")
CUT = """(sel) => Array.from(document.querySelectorAll(sel))
  .filter(e => e.scrollWidth > e.clientWidth)
  .map(e => (e.textContent || '').trim() + ' ' + e.scrollWidth + '>' + e.clientWidth)"""


def set_size(page: Page, size: str) -> None:
    page.evaluate("s => { document.documentElement.dataset.textSize = s; }", size)


def test_member_panel_buttons_show_their_whole_label_at_every_text_size(ui: UI) -> None:  # noqa: F811
    """No ``.insp-actions`` button has ``scrollWidth > clientWidth`` at any text size, on a phone and on a
    desktop (the pane at its narrowest width, a window just wide enough for it). Fails when a label such as
    "Catch up on…" is cut at Larger (it needs 138 px and had 126, #995)."""
    for options, narrow in (
        ({}, False),
        (PHONE, True),
        ({"viewport": {"width": 1000, "height": 800}}, False),
    ):
        page = ui.open(**options)
        try:
            if narrow:
                page.click("#buddy-toggle")
            page.click('#buddy-list .member[data-name="codex-1"]')
            expect(page.locator("#insp-catchup")).to_be_visible()
            for size in SIZES:
                set_size(page, size)
                assert page.evaluate(CUT, ".insp-actions .btn") == [], (options.get("viewport"), size)
        finally:
            set_size(page, "default")


def test_the_phone_members_button_holds_its_counts(ui: UI) -> None:  # noqa: F811
    """The Members button on a phone is wide enough for its icon and count at every text size, and its
    alert badge sits after the count, not over it. Fails when the content (57 px) spills out of the
    button (53 px, #995)."""
    page = ui.open(**PHONE)
    try:
        for size in SIZES:
            set_size(page, size)
            spill = page.evaluate(
                """() => { const b = document.getElementById('buddy-toggle');
                  const r = b.getBoundingClientRect();
                  const kids = Array.from(b.children).filter(c => getComputedStyle(c).display !== 'none')
                    .map(c => c.getBoundingClientRect());
                  const outside = kids.filter(c => c.right > r.right + 0.5 || c.left < r.left - 0.5).length;
                  const overlap = kids.slice(1).filter((c, i) => c.left < kids[i].right - 0.5).length;
                  return outside + overlap + Math.max(0, b.scrollWidth - b.clientWidth); }"""
            )
            assert spill <= 0, (size, spill)
    finally:
        set_size(page, "default")
