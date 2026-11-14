"""Search in a real browser (#724, DESIGN.md §58): the box and ``/``, results grouped by room with the words
highlighted, the filters, opening a result at its message with the messages around it, Back to results and
Jump to latest, the keyboard, a phone and both themes. The seeded desktop broker; the lines are planted
straight into the store (the page reads history and search from the API, so no stand-in agent is needed).
Any console error, page error or CSP violation fails the test. Marker ``e2e``."""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import PHASE_REPORTS, sanitize_env
from playwright.sync_api import Browser, Page, expect
from test_web_ui import PHONE, UI, no_horizontal_scroll
from ui_world import UIWorld

pytestmark = pytest.mark.e2e

BUILD, DESIGN = "e2e-search-build", "e2e-search-design"


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
    shutil.rmtree(u.artifacts_dir(request.node.nodeid), ignore_errors=True)
    yield u
    reports = request.node.stash.get(PHASE_REPORTS, {})
    failed = any(r.failed for r in reports.values()) or bool(u.problems)
    u.finish(failed, request.node.nodeid)
    if u.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(u.problems), pytrace=False)


def seed(world: UIWorld) -> dict[str, int]:
    """Two rooms with lines about a migration: people, agents and a call line, with a quiet stretch of
    other lines around the one the tests open. Planted once; returns the ids by name."""
    if getattr(world, "_search_ids", None):
        return world._search_ids  # type: ignore[attr-defined,no-any-return]
    world.create_room("#" + BUILD)
    world.create_room("#" + DESIGN)
    ids: dict[str, int] = {}

    def plant() -> None:
        store = world.broker.state.store
        for room_name in ("#" + BUILD, "#" + DESIGN):
            assert store.get_room(room_name) is not None

        def add(
            room: str, key: str, name: str, kind: str, text: str, ts: float, harness: str | None = None
        ) -> None:
            r = store.get_room("#" + room)
            assert r is not None
            m = store.insert_message(
                r.id,
                sender_name=name,
                sender_kind=kind,
                via={"human": "web", "agent": "mcp"}.get(kind, "meeting"),
                text=text,
                sender_harness=harness,
            )
            store.con.execute("UPDATE messages SET ts=? WHERE id=?", (ts, m.id))
            ids[key] = m.id

        base = 1_790_000_000.0
        for i in range(30):
            add(BUILD, f"pre{i}", "alice", "human", f"quiet line {i} about lunch", base + i)
        add(
            BUILD,
            "q",
            "alice",
            "human",
            "@codex-1 what is the rollback story for the schema 49 migration?",
            base + 100,
        )
        add(
            BUILD,
            "hit",
            "codex-1",
            "agent",
            "If the migration fails halfway we need a rollback path: keep the old table until it is verified",
            base + 200,
            "codex",
        )
        for i in range(30):
            add(BUILD, f"post{i}", "alice", "human", f"later line {i} about lunch", base + 300 + i)
        add(
            DESIGN,
            "call",
            "bob",
            "meeting",
            "so the rollback is just restoring last night's backup, and the migration runs again",
            base + 400,
        )
        add(
            DESIGN,
            "design",
            "claude-1",
            "agent",
            "Done: the migration dry-run passed on a copy",
            base + 500,
            "claude",
        )

    world.broker.on_loop(plant)
    world._search_ids = ids  # type: ignore[attr-defined]
    return ids


def box(page: Page) -> Any:
    width = page.viewport_size["width"]  # type: ignore[index]
    return page.locator("#search-page-input" if width <= 760 else "#search-input")


def results(page: Page) -> Any:
    return page.locator("#search-results .search-hit")


def search_for(page: Page, q: str) -> None:
    b = box(page)
    b.click()
    b.fill(q)


def test_typing_in_the_box_shows_results_grouped_by_room_with_the_words_marked(ui: UI) -> None:
    """``/`` focuses the box (and not the composer, where ``/`` is still a command). Typing shows a page of
    results in place of the log, a status line with the count, one group per room, a call's own group, and the
    matched words in <mark> elements. Esc clears the box and returns to the room. Fails if the page
    does not replace the log, the words are not marked, or Esc leaves the box filled."""
    ids = seed(ui.world)
    page = ui.open(BUILD, color_scheme="light")
    page.locator("#input").fill("")
    page.locator("#input").press("/")
    expect(page.locator("#input")).to_have_value("/")  # a field keeps its own slash
    page.locator("#input").fill("")
    page.locator("#log").click(position={"x": 5, "y": 5})
    page.keyboard.press("/")
    expect(page.locator("#search-input")).to_be_focused()
    page.keyboard.type("migration rollback")
    expect(page.locator("#app")).to_have_class(re.compile("search-open"))
    expect(results(page)).to_have_count(3)
    expect(page.locator("#room-title")).to_have_text("Search")
    expect(page.locator("#room-sub")).to_have_text("3 messages match “migration rollback”")
    heads = page.locator("#search-results .search-group-h")
    expect(heads).to_have_count(2)
    texts = [t.replace("\n", " ") for t in heads.all_inner_texts()]
    assert any(t.endswith("call") for t in texts) and any(f"{BUILD}" in t for t in texts), texts
    marks = page.locator("#search-results mark").all_inner_texts()
    assert {m.lower() for m in marks} >= {"migration", "rollback"}
    expect(page.locator("#log")).to_be_hidden()
    expect(page.locator("#composer")).to_be_hidden()
    # a hostile query is words, not syntax: no error, and nothing found
    page.locator("#search-input").fill('NEAR(a b) OR "x -y:z ^')
    expect(page.locator("#search-results .search-none")).to_be_visible()
    page.locator("#search-input").fill("migration rollback")
    expect(results(page)).to_have_count(3)
    page.keyboard.press("Escape")
    expect(page.locator("#search-input")).to_have_value("")
    expect(page.locator("#log")).to_be_visible()
    expect(page.locator("#app")).not_to_have_class(re.compile("search-open"))
    expect(page.locator("#room-title")).to_have_text(BUILD)
    assert ids["hit"]


def test_a_result_opens_its_room_around_the_message_with_a_way_back(ui: UI) -> None:
    """Clicking a result shows the room at that message: about twenty lines either side, the message
    highlighted, and a banner with Back to results and Jump to latest. Back restores the query and the
    results; Jump to latest shows the room's newest line and the composer posts to the live log. Fails if
    the room shows only its latest, the target is not marked, or Back loses the results."""
    ids = seed(ui.world)
    page = ui.open(DESIGN)
    search_for(page, "rollback path")
    expect(results(page)).to_have_count(1)
    results(page).first.click()
    expect(page.locator("#banner-around")).to_be_visible()
    expect(page.locator("#banner-around")).to_contain_text("Viewing around a search result")
    expect(page.locator("#room-title")).to_have_text(BUILD)
    target = page.locator(f'#log .line[data-id="{ids["hit"]}"]')
    expect(target).to_have_class(re.compile("search-target"))
    expect(target).to_be_in_viewport()
    rows = page.locator("#log .line.k-chat")
    assert 35 <= rows.count() <= 42, rows.count()
    expect(page.locator(f'#log .line[data-id="{ids["post29"]}"]')).to_have_count(0)  # the latest is not shown
    expect(page.locator("#search-input")).to_have_value("")
    page.click("#around-back")
    expect(page.locator("#banner-around")).to_be_hidden()
    expect(page.locator("#search-input")).to_have_value("rollback path")
    expect(results(page)).to_have_count(1)
    results(page).first.click()
    expect(page.locator("#banner-around")).to_be_visible()
    page.click("#around-latest")
    expect(page.locator("#banner-around")).to_be_hidden()
    expect(page.locator(f'#log .line[data-id="{ids["post29"]}"]')).to_have_count(1)
    expect(page.locator("#log .search-target")).to_have_count(0)
    expect(page.locator("#input")).to_be_focused()
    # leaving the window by posting: the line lands in the live log
    search_for(page, "rollback path")
    results(page).first.click()
    expect(page.locator("#banner-around")).to_be_visible()
    page.locator("#input").fill("thanks, noted")
    page.locator("#input").press("Enter")
    expect(page.locator("#banner-around")).to_be_hidden()
    expect(page.locator("#log .line.k-chat").last).to_contain_text("thanks, noted")


def test_the_keyboard_moves_through_results_and_enter_opens_one(ui: UI) -> None:
    """Down moves the ring to the next result, Up back, Enter opens the ringed one, and Esc in the room's
    banner flow is not needed. Fails if Enter opens the first whatever the ring is on."""
    ids = seed(ui.world)
    page = ui.open(BUILD)
    search_for(page, "migration")
    expect(results(page)).to_have_count(4)
    first = results(page).first
    expect(first).to_have_class(re.compile(r"\bsel\b"))
    page.keyboard.press("ArrowDown")
    expect(results(page).nth(1)).to_have_class(re.compile(r"\bsel\b"))
    expect(first).not_to_have_class(re.compile(r"\bsel\b"))
    page.keyboard.press("ArrowUp")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    wanted = results(page).nth(2).get_attribute("data-id")
    page.keyboard.press("Enter")
    expect(page.locator("#banner-around")).to_be_visible()
    expect(page.locator(f'#log .line[data-id="{wanted}"]')).to_have_class(re.compile("search-target"))
    assert wanted in {str(v) for v in ids.values()}


def test_the_filters_narrow_the_results(ui: UI) -> None:
    """A room, a person and calls off each narrow the page, and the time filter reaches the broker as a bound.
    Fails if a chip is only decoration."""
    seed(ui.world)
    page = ui.open(BUILD)
    search_for(page, "migration")
    expect(results(page)).to_have_count(4)
    page.select_option('#search-chips select[data-focus="search-room"]', label=DESIGN)
    expect(results(page)).to_have_count(2)
    page.click('#search-chips button[data-focus="search-calls"]')
    expect(results(page)).to_have_count(1)
    expect(page.locator("#search-chips button[data-focus='search-calls']")).to_have_text("Calls off")
    page.select_option('#search-chips select[data-focus="search-room"]', label="Any room")
    page.select_option('#search-chips select[data-focus="search-from"]', label="codex-1")
    expect(results(page)).to_have_count(1)
    expect(results(page).first).to_contain_text("codex-1")
    page.select_option('#search-chips select[data-focus="search-from"]', label="Anyone")
    page.click('#search-chips button[data-focus="search-calls"]')
    page.select_option('#search-chips select[data-focus="search-time"]', label="Today")
    expect(page.locator("#search-results .search-none")).to_be_visible()  # the lines are from 2026
    page.select_option('#search-chips select[data-focus="search-time"]', label="Any time")
    expect(results(page)).to_have_count(4)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_phone_has_one_column_big_targets_and_the_highlight_in_both_themes(ui: UI, scheme: str) -> None:
    """At 390 px the page carries its own box; the sidebar's hands over to it; nothing scrolls sideways; the
    chips, the results and the banner's buttons are at least 44 px tall; the mark has a background in both
    themes. Fails if the drawer's box types into nothing, or a target is under 44 px."""
    seed(ui.world)
    page = ui.open(None, color_scheme=scheme, **PHONE)
    page.click("#rooms-toggle")
    page.locator("#search-input").focus()  # in the drawer: hands over to the page's own box
    expect(page.locator("#search-page-input")).to_be_focused()
    page.keyboard.type("migration rollback")
    expect(results(page)).to_have_count(3)
    no_horizontal_scroll(page)
    for sel in ("#search-page-input", "#search-chips .search-chip", "#search-results .search-hit"):
        for h in page.locator(sel).evaluate_all("els => els.map(e => e.getBoundingClientRect().height)"):
            assert h >= 43.5, (sel, h)
    bg = page.locator("#search-results mark").first.evaluate("e => getComputedStyle(e).backgroundColor")
    assert bg not in ("rgba(0, 0, 0, 0)", "transparent"), bg
    results(page).first.click()
    expect(page.locator("#banner-around")).to_be_visible()
    for sel in ("#around-back", "#around-latest"):
        assert page.locator(sel).bounding_box()["height"] >= 43.5  # type: ignore[index]
    no_horizontal_scroll(page)
