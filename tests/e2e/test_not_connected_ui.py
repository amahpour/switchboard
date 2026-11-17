"""Agents that are not connected (#814, DESIGN.md §49.14), on the real page.

A session that went quiet with no wait open shows a hollow grey dot and "not connected since <time>", folds
into a "Not connected (n)" group at the bottom of Members (newest first, closed until opened), and its
Inspector offers Remove. Desktop and phone, light and dark. Marker ``e2e``.
"""

from __future__ import annotations

import secrets
import shutil
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import PHASE_REPORTS, sanitize_env
from playwright.sync_api import Browser, expect
from test_web_ui import PHONE, UI, no_horizontal_scroll
from ui_world import UIWorld

pytestmark = pytest.mark.e2e

HOUR = 3600.0


@pytest.fixture(scope="module")
def world(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[UIWorld]:
    """One seeded broker for this module. ``playwright`` starts first, under the real home."""
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
    shutil.rmtree(u.artifacts_dir(request.node.nodeid), ignore_errors=True)
    yield u
    reports = request.node.stash.get(PHASE_REPORTS, {})
    failed = any(r.failed for r in reports.values()) or bool(u.problems)
    u.finish(failed, request.node.nodeid)
    if u.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(u.problems), pytrace=False)


def two_gone(ui: UI) -> str:
    """A room of its own with ghost-1 last heard of 2 days ago, ghost-2 three hours ago (both hold no wait, so
    both are not connected) and live-1, which just joined. A joined scripted agent is busy until it waits, and
    a busy one gets 2 hours, hence three. Returns the room's name without its #."""
    room = "nc-" + secrets.token_hex(3)
    ui.world.create_room(f"#{room}")
    ui.world.add_agents(f"#{room}", ("ghost-1", "ghost-2", "live-1"))
    ui.world.set_member_heard(f"#{room}", "ghost-1", 48 * HOUR)
    ui.world.set_member_heard(f"#{room}", "ghost-2", 3 * HOUR)
    return room


@pytest.mark.parametrize("phone", [False, True], ids=["desktop", "phone"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_members_fold_the_agents_that_are_not_connected_newest_first(
    ui: UI, scheme: str, phone: bool
) -> None:
    """The two quiet agents are out of the main list and in a closed 'Not connected (2)' group; opening it
    shows ghost-2 (heard from three hours ago) above ghost-1 (two days), each with a hollow grey dot and its
    time, and a connected agent never goes in it. The page doesn't scroll sideways on a phone. Fails if a
    quiet agent stays among the live ones, the group opens by itself, or the order is oldest first."""
    room = two_gone(ui)
    args: dict[str, Any] = {"color_scheme": scheme, **(PHONE if phone else {})}
    page = ui.open(room, **args)
    if phone:
        page.click("#buddy-toggle")
    toggle = page.locator("#nc-group")
    expect(toggle).to_contain_text("Not connected (2)")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(page.locator('#buddy-list .member[data-name="ghost-1"]')).to_have_count(0)
    expect(page.locator('#buddy-list .member[data-name="live-1"]')).to_have_count(1)  # a live one stays
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    names = page.locator("#buddy-list .member.nc").evaluate_all("els => els.map(e => e.dataset.name)")
    assert names == ["ghost-2", "ghost-1"], names
    row = page.locator('#buddy-list .member[data-name="ghost-1"]')
    expect(row.locator(".m-status")).to_contain_text("Not connected since")
    assert row.locator(".dot").evaluate("e => e.classList.contains('s-notconnected')")
    if phone:
        no_horizontal_scroll(page)


def test_the_inspector_says_it_is_not_connected_shows_the_date_and_removes_it(ui: UI) -> None:
    """Opening ghost-1 shows 'Not connected since', a Last seen that carries a date (it is two days old, so
    not just 14:02), and a Remove button; Remove asks first, then the member is gone and the group is
    'Not connected (1)'. Fails if Last seen prints only the time, Remove is missing, or it skips the ask."""
    room = two_gone(ui)
    page = ui.open(room)
    page.click("#nc-group")
    with page.expect_response(lambda r: "/members/ghost-1" in r.url and r.request.method == "GET"):
        page.click('#buddy-list .member[data-name="ghost-1"]')
    expect(page.locator("#insp-body .insp-status")).to_contain_text("Not connected since")
    expect(page.locator("#insp-body .insp-attn")).to_contain_text("Not connected")
    seen = page.locator("#insp-body .insp-facts dt", has_text="Last seen")
    last = seen.locator("xpath=following-sibling::dd[1]")
    assert "," in last.inner_text(), last.inner_text()  # "Mon, Nov 15, 14:02": a date, not just a time
    page.click("#insp-remove")
    expect(page.locator("#app-dialog")).to_contain_text("Remove ghost-1?")
    page.click("#app-dialog .btn-danger, #app-dialog .danger")
    expect(page.locator('#buddy-list .member[data-name="ghost-1"]')).to_have_count(0)
    expect(page.locator("#nc-group")).to_contain_text("Not connected (1)")


def test_a_connected_agent_has_no_remove_button_and_the_admin_sets_the_days(ui: UI) -> None:
    """Remove is only for the agents that are not connected (a live one has Kick alone). The admin's setting
    is in Settings, Agents: it loads 3 days, saves 5, and an out-of-range number is refused in the page. Fails
    if a live agent can be 'removed', or the setting doesn't round-trip."""
    page = ui.open()
    with page.expect_response(lambda r: "/members/claude-1" in r.url and r.request.method == "GET"):
        page.click('#buddy-list .member[data-name="claude-1"]')
    expect(page.locator("#insp-kick")).to_be_visible()
    expect(page.locator("#insp-remove")).to_have_count(0)
    page.click("#insp-back")
    page.click("#me-settings")
    tab = page.locator("#settings-tab-agents")
    if tab.is_visible():
        tab.click()
    else:
        page.locator("#settings-row-agents").click()
    days = page.locator("#settings-nc-days")
    expect(days).to_have_value("3")
    days.fill("5")
    page.click("#settings-nc-save")
    expect(page.locator("#settings-nc-status")).to_have_text("Saved.")
    days.fill("500")
    page.click("#settings-nc-save")
    expect(page.locator("#settings-nc-status")).to_contain_text("Choose 1 to 365")
    days.fill("3")
    page.click("#settings-nc-save")
    expect(page.locator("#settings-nc-status")).to_have_text("Saved.")
