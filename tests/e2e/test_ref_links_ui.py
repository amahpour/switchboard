"""Reference links in the page (#790, DESIGN.md §60): the admin's Settings → Links form, and a message that
then shows `!42` as a link.

A real page against a seeded broker, on a desktop and a phone, in light and dark: the form starts empty,
a rule is added and saved, an agent's message with `!42` renders as the ordinary external link to the
template with 42 in it (and still does after a reload, from `/api/me`), and a rule the server refuses shows
its reason and is not kept. Any console error or CSP violation fails the test.

Marker ``e2e``.
"""

from __future__ import annotations

import itertools
import shutil
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import PHASE_REPORTS, sanitize_env
from playwright.sync_api import Browser, Page, expect
from test_web_ui import PHONE, UI, no_horizontal_scroll
from ui_world import UIWorld

from switchboard import ref_links

pytestmark = pytest.mark.e2e

ROOMS = itertools.count(1)
MR_URL = "https://gitlab.example.com/group/project/-/merge_requests/"


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
    world.broker.on_loop(
        world.broker.state.store.settings_replace_prefix, ref_links.PREFIX, {}
    )  # no leftovers
    reports = request.node.stash.get(PHASE_REPORTS, {})
    failed = any(r.failed for r in reports.values()) or bool(u.problems)
    u.finish(failed, request.node.nodeid)
    if u.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(u.problems), pytrace=False)


def room_with_agent(ui: UI) -> str:
    """A room of its own (the broker is shared by this module's tests), with a scripted agent in it."""
    room = f"e2e-ref-links-{next(ROOMS)}"
    ui.world.create_room("#" + room)
    ui.world.add_agents("#" + room, ("claude-1",))
    return room


def open_links_settings(page: Page, phone: bool) -> None:
    if phone:
        page.click("#rooms-toggle")  # a phone keeps the sidebar, and Settings in it, behind this
    page.click("#me-settings")
    tab = page.locator("#settings-tab-links")
    if tab.is_visible():
        tab.click()
    else:
        page.locator("#settings-row-links").click()
    expect(page.locator("#settings-links-add")).to_be_visible()


def add_rule(page: Page, n: int, pattern: str, url: str) -> None:
    page.click("#settings-links-add")
    rows = page.locator("#settings-links-list .settings-link-row")
    expect(rows).to_have_count(n)
    rows.nth(n - 1).locator(".settings-link-pattern").fill(pattern)
    rows.nth(n - 1).locator(".settings-link-url").fill(url)


@pytest.mark.parametrize("phone", [False, True], ids=["desktop", "phone"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_a_saved_rule_makes_a_reference_in_a_message_a_link(ui: UI, scheme: str, phone: bool) -> None:
    """The form starts with no rule, saves one, and a message with `!42` then renders as an external link
    to the template's address with 42 in it, shown with its real URL; it is still a link after a reload,
    because the page is given the rules at load. The page doesn't scroll sideways on the phone. Fails if
    the form is missing for the admin, the save is lost, or the reference stays plain text."""
    args: dict[str, Any] = {"color_scheme": scheme, **(PHONE if phone else {})}
    room = room_with_agent(ui)
    page = ui.open(room=room, **args)
    open_links_settings(page, phone)
    expect(page.locator(".settings-links-none")).to_be_visible()
    add_rule(page, 1, r"!(\d+)", MR_URL + "$1")
    if phone:
        no_horizontal_scroll(page)
    page.click("#settings-links-save")
    expect(page.locator("#settings-links-status")).to_have_text("Saved.")
    page.keyboard.press("Escape")
    ui.world.agent_call("#" + room, "claude-1", "say", text=f"please look at !42 ({scheme} {phone})")
    link = page.locator(f'#log a.md-link[href="{MR_URL}42"]').last
    expect(link).to_be_visible()
    expect(link).to_have_text("!42")
    expect(link.locator("xpath=following-sibling::span[@class='md-url']").first).to_have_text(MR_URL + "42")
    page.reload()
    expect(page.locator("#st-conn")).to_have_text("Connected")
    expect(page.locator(f'#log a.md-link[href="{MR_URL}42"]').last).to_be_visible()


def test_a_rule_the_server_refuses_shows_why_and_is_not_kept(ui: UI) -> None:
    """A pattern that could backtrack is refused with its reason, in the form, and nothing is stored:
    after a reload the form is empty again. Fails if the refusal is silent or the rule is kept."""
    room = room_with_agent(ui)
    page = ui.open(room=room)
    open_links_settings(page, False)
    add_rule(page, 1, "(a+)+", MR_URL + "$1")
    page.click("#settings-links-save")
    status = page.locator("#settings-links-status")
    expect(status).to_contain_text("rule 1")
    expect(status).to_contain_text("can't be repeated")
    expect(status).to_have_class("fine bad")
    # the browser logs the refused request itself, once, and nothing else is wrong
    assert (
        len(ui.problems) == 1
        and "status of 400" in ui.problems[0]
        and "/api/settings/links" in ui.problems[0]
    )
    ui.problems.clear()
    page.reload()
    expect(page.locator("#st-conn")).to_have_text("Connected")
    open_links_settings(page, False)
    expect(page.locator(".settings-links-none")).to_be_visible()
    assert ui.world.broker.on_loop(ui.world.broker.state.store.settings_with_prefix, ref_links.PREFIX) == []


def test_a_rule_can_be_removed_and_the_reference_is_text_again(ui: UI) -> None:
    """Removing the only rule and saving puts the form back to empty, and a later message's `!7` is plain
    text. Fails if a removed rule keeps linking."""
    room = room_with_agent(ui)
    page = ui.open(room=room)
    open_links_settings(page, False)
    add_rule(page, 1, r"!(\d+)", MR_URL + "$1")
    page.click("#settings-links-save")
    expect(page.locator("#settings-links-status")).to_have_text("Saved.")
    page.click('#settings-links-list button[aria-label="Remove rule 1"]')
    page.click("#settings-links-save")
    expect(page.locator("#settings-links-status")).to_have_text("Saved.")
    expect(page.locator(".settings-links-none")).to_be_visible()
    page.keyboard.press("Escape")
    ui.world.agent_call("#" + room, "claude-1", "say", text="no link for !7 now")
    expect(page.locator("#log .line", has_text="no link for !7 now").last).to_be_visible()
    expect(page.locator(f'#log a.md-link[href="{MR_URL}7"]')).to_have_count(0)
