"""A room's hands-free calls in the page (#697, DESIGN.md §52.3): Call settings and the header marker.

The settings are the person's own ``/room voice`` and ``/room listen`` commands, typed by Save. A console
error, a page error or a CSP violation fails the test, through the watcher in ``test_web_ui.UI``.
"""

from __future__ import annotations

from playwright.sync_api import Page
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


def _chips_cut(page: Page) -> list[str]:
    """The header's status chips that are shown but not wholly inside the row that holds them."""
    return page.evaluate(
        """() => {
          const row = document.getElementById('status-chips').getBoundingClientRect();
          return [...document.querySelectorAll('#status-chips .chip')]
            .filter(c => c.getBoundingClientRect().width > 0)
            .filter(c => { const b = c.getBoundingClientRect();
                           return b.left < row.left - 1 || b.right > row.right + 1; })
            .map(c => c.id);
        }"""
    )


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_headers_chips_are_all_in_view_with_both_side_panels_open(
    ui: UI,
    calls: InProcBroker,  # noqa: F811
    scheme: str,
) -> None:
    """#704: with the room list and Members both open (a 1280 px window), the header's chip row had room for
    about one and a half chips, so with hands-free on the Hands-free chip pushed Running and Budget out of
    sight (the row scrolled). Whether a room is paused and what its budget is are worth seeing at a glance,
    so below a width the chips take a line of their own under the title, and none is cut. Fails if a chip is
    cut by its row at 1280 px or at 1180 px, or if the room's name or its buttons give way for
    it; at a wide window they stay on the title's line."""
    room = f"chips-{scheme}"
    assert _post(calls, "/api/rooms", {"name": "#" + room}).status_code == 200
    assert _post(calls, f"/api/rooms/{room}/command", {"text": "/room voice on"}).status_code == 200
    page = _open(ui, calls, room=room, color_scheme=scheme, viewport={"width": 1280, "height": 800})
    expect(page.locator("#st-handsfree")).to_be_visible()
    expect(page.locator("#pane-toggle")).to_have_attribute("aria-pressed", "true")  # Members is open
    for width in (1280, 1180):
        page.set_viewport_size({"width": width, "height": 800})
        for chip in ("#st-handsfree", "#st-state", "#st-budget", "#st-hops"):
            expect(page.locator(chip)).to_be_visible()
        assert _chips_cut(page) == [], f"cut at {width} px"
        assert page.locator("#status-chips").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        assert page.locator("#room-title-button").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        head, tools = page.locator(".conv-head").bounding_box(), page.locator("#room-rules").bounding_box()
        assert head and tools and tools["x"] + tools["width"] <= head["x"] + head["width"], width
    # a window with room to spare keeps them on the title's line, so a wide header stays one row
    page.locator("#pane-toggle").click()
    page.set_viewport_size({"width": 1700, "height": 800})
    head, chips = page.locator(".conv-head").bounding_box(), page.locator("#status-chips").bounding_box()
    assert head and chips and head["height"] < 60 and chips["y"] < head["y"] + 52
    phone = _open(ui, calls, room=room, color_scheme=scheme, **PHONE)
    # the phone keeps its own swipeable row of chips, with Hands-free first in it
    expect(phone.locator("#st-handsfree")).to_be_in_viewport()
