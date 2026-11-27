"""The member panel's Coordinator… dialog in a real browser (#976, DESIGN.md §50.2): five unticked boxes
for a new coordinator, Save types the person's own ``/coordinator <name> <actions>``, the member list and
the panel mark it, reopening shows what was ticked, Remove types ``/coordinator off``, and the dialog fits a
phone. A console error, a page error or a CSP violation fails the test (``test_web_ui.UI``).
Marker ``e2e``."""

from __future__ import annotations

import pytest
from playwright.sync_api import Locator, Page, Request, expect
from test_web_ui import PHONE, UI, ui, world  # noqa: F401

pytestmark = pytest.mark.e2e


def is_command(r: Request) -> bool:
    return r.method == "POST" and r.url.endswith("/api/rooms/build/command")


KEYS = ["Quiet", "Boards", "Pins", "Calls", "Budget"]


def test_the_coordinator_dialog_names_changes_and_removes_it(ui: UI) -> None:  # noqa: F811
    """Coordinator… opens a dialog of five checkboxes, all unticked for a new one, each with a hint. Save
    sends ``/coordinator codex-1 quiet,pins`` and nothing else; the member row and the panel then say
    coordinator, the
    reopened dialog shows the two ticks and offers Remove; unticking all sends ``none`` (named, no actions);
    Remove sends ``/coordinator off`` and the marks go. Fails if a box starts ticked, a different command is
    sent, the marks do not follow, or Remove is offered for an agent that is not the coordinator."""
    page = ui.open()
    row = page.locator('#buddy-list .member[data-name="codex-1"]')
    page.click('#buddy-list .member[data-name="codex-1"]')
    button = page.locator("#insp-coordinator")
    expect(button).to_have_text("Coordinator…")
    dialog = page.locator("#app-dialog")
    try:
        button.click()
        expect(dialog).to_be_visible()
        expect(dialog.locator("h2")).to_have_text("Coordinator: codex-1")
        for key in KEYS:
            expect(dialog.get_by_label(key, exact=True)).not_to_be_checked()
        expect(dialog.locator("#app-dialog-coord-remove")).to_have_count(0)
        expect(dialog.locator("#app-dialog-coord-quiet-hint")).to_contain_text("/room quiet")
        dialog.get_by_label("Quiet", exact=True).check()
        dialog.get_by_label("Pins", exact=True).check()
        with page.expect_request(
            lambda r: r.method == "POST" and r.url.endswith("/api/rooms/build/command")
        ) as req:
            dialog.get_by_role("button", name="Save").click()
        assert req.value.post_data_json == {"text": "/coordinator codex-1 quiet,pins"}
        expect(page.locator("#insp-body .chip-quiet", has_text="coordinator")).to_have_count(1)
        expect(row).to_contain_text("coordinator")
        expect(page.locator("#log")).to_contain_text("named codex-1 the room's coordinator")
        expect(button).to_have_text("Coordinator ✓…")

        button.click()
        expect(dialog.get_by_label("Quiet", exact=True)).to_be_checked()
        expect(dialog.get_by_label("Pins", exact=True)).to_be_checked()
        expect(dialog.get_by_label("Boards", exact=True)).not_to_be_checked()
        dialog.get_by_label("Quiet", exact=True).uncheck()
        dialog.get_by_label("Pins", exact=True).uncheck()
        with page.expect_request(is_command) as req:
            dialog.get_by_role("button", name="Save").click()
        assert req.value.post_data_json == {"text": "/coordinator codex-1 none"}
        expect(page.locator("#insp-body .chip-quiet", has_text="coordinator")).to_have_attribute(
            "title", "Named the coordinator, with no actions ticked"
        )

        button.click()
        with page.expect_request(is_command) as req:
            dialog.get_by_role("button", name="Remove coordinator").click()
        assert req.value.post_data_json == {"text": "/coordinator off"}
        expect(page.locator("#insp-body .chip-quiet", has_text="coordinator")).to_have_count(0)
        expect(row).not_to_contain_text("coordinator")
        expect(button).to_have_text("Coordinator…")
        expect(page.locator("#log")).to_contain_text("removed codex-1 as the room's coordinator")
    finally:
        assert ui.world.web is not None
        ui.world.web.post(
            "/api/rooms/build/command",
            json={"text": "/coordinator off"},
            headers=ui.world.broker.write_headers(),
        )


def test_the_coordinator_dialog_fits_a_phone_and_cancel_sends_nothing(ui: UI) -> None:  # noqa: F811
    """On a phone the dialog and its five boxes sit inside the screen with no sideways scroll, and Cancel
    sends no command. Fails if the dialog is cut off or Cancel names a coordinator."""
    page = ui.open(**PHONE)
    page.click("#buddy-toggle")
    page.click('#buddy-list .member[data-name="codex-1"]')
    sent: list[str] = []
    page.on(
        "request", lambda r: sent.append(r.url) if r.method == "POST" and r.url.endswith("/command") else None
    )
    page.locator("#insp-coordinator").click()
    dialog = page.locator("#app-dialog")
    expect(dialog).to_be_visible()
    box = dialog.bounding_box()
    assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= PHONE["viewport"]["width"]
    assert box["y"] >= 0 and box["y"] + box["height"] <= PHONE["viewport"]["height"]
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
    dialog.get_by_label("Calls", exact=True).check()
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).not_to_be_visible()
    assert sent == []


def open_coordinator(page: Page) -> Locator:
    """Open codex-1's Coordinator… dialog (the phone's member sheet first) and return the dialog."""
    if (page.viewport_size or {}).get("width", 1440) <= 760:
        page.click("#buddy-toggle")
    page.click('#buddy-list .member[data-name="codex-1"]')
    page.locator("#insp-coordinator").click()
    dialog = page.locator("#app-dialog")
    expect(dialog).to_be_visible()
    return dialog


def test_each_option_row_keeps_its_hint_tight_and_the_rows_evenly_spaced(ui: UI) -> None:  # noqa: F811
    """In the Coordinator dialog each hint sits within 6 px under its label's text, lined up with it, the
    boxes are 12 px apart, and a click on a hint ticks its box. Fails if a hint floats as far from its label
    as from the next box (#995), or a hint is not part of its row."""
    page = ui.open()
    dialog = open_coordinator(page)
    try:
        for key in ("quiet", "boards", "pins", "calls", "budget"):
            label = dialog.locator(f"#app-dialog-coord-{key}-label").bounding_box()
            hint = dialog.locator(f"#app-dialog-coord-{key}-hint").bounding_box()
            assert label is not None and hint is not None
            gap = hint["y"] - (label["y"] + label["height"])
            assert 0 <= gap <= 6, (key, gap)
            assert abs(hint["x"] - label["x"]) <= 1, (key, hint["x"], label["x"])
        rows = [
            dialog.locator(f"#app-dialog-coord-{k}").evaluate(
                "e => e.closest('label').getBoundingClientRect()"
            )
            for k in ("quiet", "boards", "pins", "calls", "budget")
        ]
        gaps = [round(b["top"] - (a["top"] + a["height"]), 1) for a, b in zip(rows, rows[1:], strict=False)]
        assert max(gaps) - min(gaps) <= 1 and 10 <= gaps[0] <= 14, gaps
        dialog.locator("#app-dialog-coord-pins-hint").click()
        expect(dialog.locator("#app-dialog-coord-pins")).to_be_checked()
    finally:
        page.keyboard.press("Escape")


def test_select_all_ticks_clears_and_goes_mixed(ui: UI) -> None:  # noqa: F811
    """All actions ticks all five boxes, clearing it clears them, ticking two of five makes it mixed, and Save
    after All actions names all five actions. Fails if it is not a real checkbox, ignores a single box, or
    Save leaves an action out."""
    page = ui.open()
    dialog = open_coordinator(page)
    all_box = dialog.locator("#app-dialog-coord-all")
    boxes = [dialog.locator(f"#app-dialog-coord-{k}") for k in ("quiet", "boards", "pins", "calls", "budget")]
    try:
        expect(all_box).not_to_be_checked()
        assert all_box.evaluate("e => e.indeterminate") is False
        all_box.check()
        for box in boxes:
            expect(box).to_be_checked()
        all_box.uncheck()
        for box in boxes:
            expect(box).not_to_be_checked()
        boxes[0].check()
        boxes[2].check()
        assert all_box.evaluate("e => e.indeterminate") is True
        all_box.press("Space")  # mixed -> ticks every box (a keyboard user reaches it too)
        for box in boxes:
            expect(box).to_be_checked()
        boxes[1].uncheck()
        assert all_box.evaluate("e => e.indeterminate") is True
        all_box.check()
        with page.expect_request(is_command) as req:
            dialog.get_by_role("button", name="Save").click()
        assert req.value.post_data_json == {"text": "/coordinator codex-1 quiet,boards,pins,calls,budget"}
    finally:
        assert ui.world.web is not None
        ui.world.web.post(
            "/api/rooms/build/command",
            json={"text": "/coordinator off"},
            headers=ui.world.broker.write_headers(),
        )


def test_the_coordinator_dialog_fits_a_phone_at_larger_text_with_save_in_view(ui: UI) -> None:  # noqa: F811
    """At 390x844 with Larger text the dialog is no taller than the window, its middle scrolls and Save is in
    the viewport without scrolling. Fails if the dialog grows past the window (957 px tall, #995)."""
    page = ui.open(**PHONE)
    page.evaluate("document.documentElement.dataset.textSize = 'larger'")
    dialog = open_coordinator(page)
    box = dialog.bounding_box()
    assert box is not None and box["y"] >= 0 and box["y"] + box["height"] <= 844
    save = dialog.get_by_role("button", name="Save").bounding_box()
    assert save is not None and save["y"] >= 0 and save["y"] + save["height"] <= 844
    assert dialog.locator(".app-dialog-form").evaluate("e => e.scrollHeight > e.clientHeight")
    page.keyboard.press("Escape")
    page.evaluate("document.documentElement.dataset.textSize = 'default'")
