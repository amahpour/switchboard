"""A room's hands-free calls in the page (#697, DESIGN.md §52.3): Call settings and the header marker.

The settings are the person's own ``/room voice`` and ``/room listen`` commands, typed by Save. A console
error, a page error or a CSP violation fails the test, through the watcher in ``test_web_ui.UI``.
"""

from __future__ import annotations

from test_meetings_ui import PHONE, UI, InProcBroker, _open, _post, expect, pytest
from test_meetings_ui import calls as calls  # noqa: F401  (a fixture)
from test_meetings_ui import ui as ui  # noqa: F401  (a fixture)

pytestmark = pytest.mark.e2e


def test_the_call_settings_dialog_sets_hands_free_and_the_header_says_so(
    ui: UI,
    calls: InProcBroker,  # noqa: F811
) -> None:
    """Call settings is a button with a name, reachable by Tab, that opens a dialog of two checkboxes. Save
    sends the person's own commands, the room's header shows a Hands-free marker for everyone while
    either is on and the room's log names who changed it, and turning both off takes the marker away. A phone
    shows the marker and the dialog inside the screen. Fails if the marker stays, hides with one setting
    still on, or the dialog cuts off."""
    assert _post(calls, "/api/rooms", {"name": "#handsfree"}).status_code == 200
    page = _open(ui, calls, room="handsfree")
    marker = page.locator("#st-handsfree")
    expect(marker).to_be_hidden()

    button = page.get_by_role("button", name="Call settings")
    page.locator("#room-wake").focus()
    page.keyboard.press("Tab")
    expect(button).to_be_focused()
    assert button.evaluate("el => getComputedStyle(el).outlineStyle") == "solid"
    page.keyboard.press("Enter")
    dialog = page.locator("#app-dialog")
    expect(dialog).to_be_visible()
    voice = dialog.get_by_label("Calls start with Voice on")
    listen = dialog.get_by_label("Agents listen on calls")
    expect(voice).not_to_be_checked()
    expect(listen).not_to_be_checked()
    voice.check()
    dialog.get_by_role("button", name="Save").click()
    expect(marker).to_be_visible()
    expect(marker).to_have_text("Hands-free")
    expect(page.locator("#log")).to_contain_text("alice turned on hands-free voice")

    # the marker is for everyone in the room: a second page sees it with no action of its own
    other = _open(ui, calls, room="handsfree")
    expect(other.locator("#st-handsfree")).to_be_visible()

    button.click()
    expect(voice).to_be_checked()
    listen.check()
    voice.uncheck()
    dialog.get_by_role("button", name="Save").click()
    expect(page.locator("#log")).to_contain_text("alice turned on hands-free listening")
    expect(page.locator("#log")).to_contain_text("alice turned off hands-free voice")
    expect(marker).to_be_visible()  # listening is still on
    expect(marker).to_have_attribute(
        "title", "Calls here start with Voice off; agents listen without being named"
    )

    button.click()
    listen.uncheck()
    dialog.get_by_role("button", name="Save").click()
    expect(marker).to_be_hidden()
    expect(other.locator("#st-handsfree")).to_be_hidden()

    # a phone: the marker and the dialog are inside the screen
    phone = _open(ui, calls, room="handsfree", **PHONE)
    phone.get_by_role("button", name="Call settings").click()
    box = phone.locator("#app-dialog").bounding_box()
    assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= PHONE["viewport"]["width"]
    assert phone.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
