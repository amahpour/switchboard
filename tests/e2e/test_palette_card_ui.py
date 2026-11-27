"""The command palette in small groups, and a review card whose controls line up (#995, step 4).

Fails if a palette group passes eight commands (UI.md: a menu past eight items is grouped), if typing stops
filtering across groups or leaves an empty heading, if the arrow keys land on a heading, if "web only"
disappears from a command a person can't run from anywhere, if an argument hint is cut, or if the four
controls of a conceded finding's card stop sharing a column. Marker ``e2e``."""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, expect
from test_layout_sweep import set_size
from test_web_ui import PHONE, UI, open_room, review_room, ui, world  # noqa: F401

pytestmark = pytest.mark.e2e

GROUPS = """() => Array.from(document.querySelectorAll('#palette [role=group]')).map(g => ({
  name: g.getAttribute('aria-label'),
  heading: g.querySelector('.group-label').textContent,
  options: Array.from(g.querySelectorAll('[role=option]')).map(o => o.id),
}))"""


def palette(ui: UI, text: str = "/", **open_args: Any) -> Page:  # noqa: F811
    page = ui.open(**open_args)
    page.locator("#input").focus()
    page.keyboard.type(text)
    expect(page.locator("#palette")).to_be_visible()
    return page


def test_no_palette_group_has_more_than_eight_commands_and_each_is_a_labelled_group(
    ui: UI,  # noqa: F811
) -> None:
    """21 commands in Room (9) and Agents (9) were two long lists. Every group is now eight or fewer, every
    one is a ``role=group`` named by its visible heading, and no command is lost."""
    page = palette(ui)
    groups = page.evaluate(GROUPS)
    assert [g["name"] for g in groups] == ["Wake-ups", "Room", "Delivery", "Work", "Info"]
    assert all(g["name"] == g["heading"] for g in groups)
    assert all(0 < len(g["options"]) <= 8 for g in groups), groups
    assert sum(len(g["options"]) for g in groups) == 21


def test_typing_filters_across_groups_and_an_empty_group_has_no_heading(
    ui: UI,  # noqa: F811
) -> None:
    """Typing "/re" matches commands in four groups, "/pa" only /pause: only its group is left. Fails if a
    heading stays above nothing, or the filter is cut to one group."""
    page = palette(ui, "/re")
    groups = page.evaluate(GROUPS)
    assert {g["name"]: g["options"] for g in groups} == {
        "Wake-ups": ["pal-resume"],
        "Room": ["pal-rename"],
        "Delivery": ["pal-release"],
        "Work": ["pal-review"],
    }
    page.keyboard.press("Backspace")
    page.keyboard.press("Backspace")
    page.keyboard.type("pa")
    assert [g["name"] for g in page.evaluate(GROUPS)] == ["Wake-ups"]
    assert page.locator("#palette .group-label").count() == 1


def test_the_arrow_keys_move_between_commands_and_skip_headings(
    ui: UI,  # noqa: F811
) -> None:
    """Down from the last command of a group lands on the first of the next one, never on its heading, and
    up goes back. Fails if a heading takes the selection or a group boundary stops the keys."""
    page = palette(ui)
    expect(page.locator("#pal-pause")).to_have_attribute("aria-selected", "true")
    for cmd in ("resume", "budget", "hops", "rename"):  # hops ends Wake-ups; rename starts Room
        page.keyboard.press("ArrowDown")
        expect(page.locator(f"#pal-{cmd}")).to_have_attribute("aria-selected", "true")
    page.keyboard.press("ArrowUp")
    expect(page.locator("#pal-hops")).to_have_attribute("aria-selected", "true")
    assert page.locator("#palette [aria-selected=true]").count() == 1


def test_a_web_only_command_says_so_in_text_and_is_not_a_badge(
    ui: UI,  # noqa: F811
) -> None:
    """The note "web only" stays as muted text in the row. Fails if it goes back to a badge
    (``.pill``) or is dropped from /resume, or appears on /pause, which any session can run."""
    page = palette(ui)
    assert page.locator("#palette .pill").count() == 0
    expect(page.locator("#pal-resume .pal-web")).to_have_text("web only")
    expect(page.locator("#pal-budget .pal-web")).to_have_text("raise: web only")
    expect(page.locator("#pal-pause .pal-web")).to_have_count(0)
    # a person reads it: it is text a screen reader sees, and not a faint one
    assert page.locator("#pal-resume .pal-web").evaluate("e => getComputedStyle(e).display") != "none"


HINTS = ("meeting", "coordinator", "catchup")


@pytest.mark.parametrize("device", ["desktop", "phone"])
@pytest.mark.parametrize("size", ["default", "larger"])
def test_a_long_argument_hint_is_whole(
    ui: UI,  # noqa: F811
    device: str,
    size: str,
) -> None:
    """/meeting's, /coordinator's and /catchup's hints were cut by an ellipsis. Each wraps under its command
    now: no ellipsis, nothing wider than its box, and the whole text is on the page."""
    page = palette(ui, "/", **(PHONE if device == "phone" else {}))
    set_size(page, size)
    for cmd in HINTS:
        hint = page.locator(f"#pal-{cmd} .pal-args")
        hint.scroll_into_view_if_needed()
        cut = hint.evaluate(
            """e => ({over: getComputedStyle(e).textOverflow, wide: e.scrollWidth - e.clientWidth,
                     tall: e.scrollHeight - e.clientHeight, text: e.textContent})"""
        )
        assert cut["over"] == "clip" and cut["wide"] <= 0 and cut["tall"] <= 0, (cmd, cut)
    expect(page.locator("#pal-meeting .pal-args")).to_have_text("[<link> [mine]|leave]")
    expect(page.locator("#pal-catchup .pal-args")).to_have_text("<agent> [on …]")


CARD = ("#board-reassign-owner", "#board-ticket", "#board-reason")
BUTTONS = ("#board-reassign", "#board-mark-fixed", "#board-defer", "#board-drop")
BOX = "e => { const r = e.getBoundingClientRect(); return {left: r.left, width: r.width, right: r.right}; }"


@pytest.mark.parametrize("device", ["desktop", "phone"])
@pytest.mark.parametrize("size", ["default", "larger"])
def test_an_open_cards_inputs_share_a_column_and_its_buttons_a_width(
    ui: UI,  # noqa: F811
    device: str,
    size: str,
) -> None:
    """A conceded finding's card stacked Owner, the follow-up ticket and the reason with their buttons at
    uneven widths. The three fields now share a left edge and a width, the four buttons a width and a
    right edge, with no sideways scroll."""
    slug = f"e2e-card-{device}-{size}"
    page = review_room(ui, slug, **(PHONE if device == "phone" else {}))
    set_size(page, size)
    page.click("#board-toggle")
    page.click('#board .board-card[data-item="F4"]')  # conceded to codex-1
    for sel in (*CARD, *BUTTONS):
        expect(page.locator(sel)).to_be_visible()
    fields = [page.locator(s).evaluate(BOX) for s in CARD]
    buttons = [page.locator(s).evaluate(BOX) for s in BUTTONS]
    assert max(f["left"] for f in fields) - min(f["left"] for f in fields) < 1, fields
    assert max(f["width"] for f in fields) - min(f["width"] for f in fields) < 1, fields
    assert max(b["width"] for b in buttons) - min(b["width"] for b in buttons) < 1, buttons
    assert max(b["right"] for b in buttons) - min(b["right"] for b in buttons) < 1, buttons
    # each button sits to the right of its field and nothing is cut or past the card
    card = page.locator("#board .board-detail").evaluate(BOX)
    for f, b in zip(fields, [buttons[0], buttons[2], buttons[3]], strict=True):
        assert f["right"] <= b["left"] + 1
    assert all(b["right"] <= card["right"] + 1 for b in buttons)
    for sel in BUTTONS:
        assert page.locator(sel).evaluate("e => e.scrollWidth <= e.clientWidth") is True, sel
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
