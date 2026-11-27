"""One footer on every Settings panel whose fields are saved together (#995, finding 5).

A real page against a seeded broker, at 1440x900 and at a 390x844 phone with Larger text. Profile, New rooms,
Files and Links each have Discard and Save in one footer under the panel's scrolling body: Save is inside
the window (and stays put when the body scrolls), Discard puts back what was there, and Save keeps the edit
across a reload. On a desktop the dialog is the same size on every tab. Fails if a panel keeps its Save
inline in the body (where a long panel scrolls it away), or if Discard leaves the edit in the field.

Marker ``e2e``.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from conftest import PHASE_REPORTS, sanitize_env
from playwright.sync_api import Browser, Page, expect
from test_web_ui import PHONE, UI, no_horizontal_scroll
from ui_world import UIWorld

from switchboard import ref_links

pytestmark = pytest.mark.e2e

DESKTOP: dict[str, Any] = {"viewport": {"width": 1440, "height": 900}}
DEVICES = [pytest.param(DESKTOP, False, id="desktop"), pytest.param(PHONE, True, id="phone-larger")]
RULES_URL = "https://gitlab.example.com/group/project/-/merge_requests/"


@pytest.fixture(scope="module")
def world(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[UIWorld]:
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        w = UIWorld().start()
        try:
            yield w
        finally:
            w.stop()


@pytest.fixture
def ui(browser: Browser, world: UIWorld, request: pytest.FixtureRequest) -> Iterator[UI]:
    u = UI(browser, world)
    # not the field's own 30, so seeing 45 proves the stored value has loaded
    world.broker.on_loop(world.broker.state.attachments.set_retention, 45)
    shutil.rmtree(u.artifacts_dir(request.node.nodeid), ignore_errors=True)
    yield u
    world.broker.on_loop(world.broker.state.store.settings_replace_prefix, ref_links.PREFIX, {})
    world.broker.on_loop(world.broker.state.attachments.set_retention, 30)  # the default, for other modules
    reports = request.node.stash.get(PHASE_REPORTS, {})
    failed = any(r.failed for r in reports.values()) or bool(u.problems)
    u.finish(failed, request.node.nodeid)
    if u.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(u.problems), pytrace=False)


def open_panel(ui: UI, args: dict[str, Any], phone: bool, group: str) -> Page:
    """A fresh page with Settings open at ``group``, at Larger text on a phone."""
    page = ui.open(room=None, **args)
    if phone:
        page.evaluate("() => { document.documentElement.dataset.textSize = 'larger'; }")
        page.click("#rooms-toggle")
    page.click("#me-settings")
    expect(page.locator("#app-dialog")).to_be_visible()
    page.click(f"#settings-row-{group}" if phone else f"#settings-tab-{group}")
    expect(page.locator(f"#settings-panel-{group}")).to_be_visible()
    return page


def box(page: Page, selector: str) -> dict[str, float]:
    found = page.locator(selector).bounding_box()
    assert found is not None, selector
    return found


def assert_footer_in_view(page: Page, group: str, save: str, phone: bool) -> None:
    """Save sits in the panel's footer, wholly inside the window, and does not move when the body scrolls."""
    footer = f"#settings-panel-{group} .settings-savebar"
    expect(page.locator(f"{footer} {save}")).to_be_visible()
    view = page.viewport_size or {}
    first = box(page, f"{footer} {save}")
    assert 0 <= first["y"] and first["y"] + first["height"] <= view["height"], first
    assert first["x"] >= 0 and first["x"] + first["width"] <= view["width"], first
    page.locator(f"#settings-panel-{group} .settings-panel-content").evaluate(
        "e => { e.scrollTop = e.scrollHeight; }"
    )
    assert box(page, f"{footer} {save}") == first
    if phone:
        no_horizontal_scroll(page)


Edit = Callable[[Page], None]


def edit_profile(page: Page) -> None:
    page.fill("#settings-first", "Alice")


def edit_rooms(page: Page) -> None:
    page.fill("#settings-room-rules", "be kind")


def edit_files(page: Page) -> None:
    """A longer time than the stored 45 asks nothing, where a shorter one asks first."""
    page.fill("#settings-files-days", "60")


def edit_links(page: Page) -> None:
    page.click("#settings-links-add")
    page.locator("#settings-links-list .settings-link-pattern").last.fill(r"!(\d+)")
    page.locator("#settings-links-list .settings-link-url").last.fill(RULES_URL + "$1")


# group: (edit, the field that shows it, what Save leaves in that field, what Discard puts back)
PANELS: dict[str, tuple[Edit, str, str, str]] = {
    "profile": (edit_profile, "#settings-first", "Alice", ""),
    "rooms": (edit_rooms, "#settings-room-rules", "be kind", ""),
    "files": (edit_files, "#settings-files-days", "60", "45"),
    "links": (edit_links, "#settings-links-list .settings-link-pattern", r"!(\d+)", ""),
}


@pytest.mark.parametrize("args,phone", DEVICES)
@pytest.mark.parametrize("group", list(PANELS))
def test_every_saving_panel_has_discard_and_save_in_a_footer_that_stays_in_view(
    ui: UI, group: str, args: dict[str, Any], phone: bool
) -> None:
    """Edit a field: Save and Discard turn on, in the footer, in view. Discard puts the old value back and
    turns both off; the same edit saved is still there after a reload. A Save inline in the body, or a
    Discard that keeps the edit, fails this."""
    edit, field, saved_value, before = PANELS[group]
    page = open_panel(ui, args, phone, group)
    names = {"profile": ("name", "profile"), "rooms": ("rules", "rules")}
    stem, dstem = names.get(group, (group, group))
    save, discard = f"#settings-{stem}-save", f"#settings-{dstem}-discard"
    footer = f"#settings-panel-{group} .settings-savebar"
    expect(page.locator(f"{footer} {save}")).to_be_disabled()
    expect(page.locator(f"{footer} {discard}")).to_be_disabled()
    if group == "files":
        expect(page.locator("#settings-files-days")).to_have_value("45")
    if group == "links":
        expect(page.locator(".settings-links-none")).to_be_visible()
    edit(page)
    expect(page.locator(f"{footer} {save}")).to_be_enabled()
    assert_footer_in_view(page, group, save, phone)
    assert_footer_in_view(page, group, discard, phone)
    page.click(f"{footer} {discard}")
    if group == "links":
        expect(page.locator("#settings-links-list .settings-link-row")).to_have_count(0)
    else:
        expect(page.locator(field)).to_have_value(before)
    expect(page.locator(f"{footer} {save}")).to_be_disabled()
    edit(page)
    page.click(f"{footer} {save}")
    expect(page.locator(f"{footer} {save}")).to_be_disabled()  # clean again: what is shown is what is stored
    page.reload()
    expect(page.locator("#st-conn")).to_have_text("Connected")
    if phone:
        page.click("#rooms-toggle")
    page.click("#me-settings")
    page.click(f"#settings-row-{group}" if phone else f"#settings-tab-{group}")
    expect(page.locator(field)).to_have_value(saved_value)
    if group in ("profile", "rooms"):  # put back what this test changed, through the same footer
        page.fill(field, "")
        page.click(f"{footer} {save}")
        expect(page.locator(f"{footer} {save}")).to_be_disabled()


def test_the_dialog_is_the_same_size_on_every_tab(ui: UI) -> None:
    """On a desktop Settings does not jump when a tab changes: its box is identical on every one."""
    page = open_panel(ui, DESKTOP, False, "profile")
    sizes = set()
    for group in ("profile", "appearance", "rooms", "agents", "files", "links", "backups"):
        page.click(f"#settings-tab-{group}")
        expect(page.locator(f"#settings-panel-{group}")).to_be_visible()
        b = box(page, "#app-dialog")
        sizes.add((b["width"], b["height"]))
    assert len(sizes) == 1, sizes
    width, height = sizes.pop()
    assert (width, height) == (880, 760), (width, height)  # the roomier size of #995, not the old 860 x 720


def test_add_a_rule_is_a_button_not_a_banner(ui: UI) -> None:
    """UI.md: a full-width button belongs only in a phone sheet's footer. Once Save moved to the footer,
    Links' "Add a rule" stretched across the desktop panel. Fails if it takes half the panel's width."""
    page = open_panel(ui, DESKTOP, False, "links")
    add, panel = box(page, "#settings-links-add"), box(page, "#settings-panel-links")
    assert add["width"] < panel["width"] / 2, (add, panel)


@pytest.mark.parametrize("args,phone", DEVICES)
def test_no_hint_in_links_runs_past_its_panel(ui: UI, args: dict[str, Any], phone: bool) -> None:
    """Links' hint carries an example address with no spaces in it, which ran past the panel's edge on a
    phone. Fails if any line of a hint reaches beyond the panel's body."""
    page = open_panel(ui, args, phone, "links")
    over = page.evaluate(
        """() => {
          const body = document.querySelector('#settings-panel-links .settings-panel-content');
          const edge = body.getBoundingClientRect().right;
          return Array.from(body.querySelectorAll('.fine')).filter(e => {
            const r = document.createRange();
            r.selectNodeContents(e);
            return Array.from(r.getClientRects()).some(x => x.right > edge + 0.5);
          }).map(e => e.textContent.slice(0, 40));
        }"""
    )
    assert over == [], over
