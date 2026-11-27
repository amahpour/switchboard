"""The usage page in a real browser (#818, #995, DESIGN.md §61): the tiles, the chart of the last 7 days,
the rooms, the agents and how reviews went, on a phone in the dark theme and on a desktop in the light one.
The switch changes the numbers. A console error, a page error or a CSP violation fails
the test."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import FakeClock, InProcBroker, make_tmp_home, sanitize_env
from playwright.sync_api import Browser, Page, expect
from test_web_ui import DESKTOP, PHASE_REPORTS, PHONE, UI

from switchboard.models import session_key
from switchboard.usage import window_starts

pytestmark = pytest.mark.e2e


def no_sideways_scroll(page: Page) -> None:
    """The page does not scroll sideways, and nothing on the usage page sticks out past the right edge."""
    wide = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert wide <= 0, f"the page scrolls sideways by {wide}px"
    out = page.evaluate(
        """() => Array.from(document.querySelectorAll(
            '#usage button, #usage .usage-tile, #usage .usage-chart, #usage .usage-table')).filter(
            e => e.getBoundingClientRect().right > window.innerWidth + 0.5).map(e => e.className)"""
    )
    assert out == [], f"cut off on the right: {out}"


@pytest.fixture(scope="module")
def meter(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[InProcBroker]:
    """A broker of its own, so planting batches here cannot change another test's rooms."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        broker = InProcBroker(home).start()
        try:

            def plant() -> None:
                store = broker.state.store
                room = store.create_room("#meter", "alice", 40, 0)
                person = store.upsert_participant("claude", session_key("claude", "", "meter-1"))
                joined = store.create_membership(room.id, person.id, "meter-1")
                store.set_held(joined.id, True)
                store.set_status(person.id, "offline", "test")
                for text in ("one", "two"):
                    msg = store.insert_message(
                        room.id, sender_name="alice", sender_kind="human", via="web", text=text
                    )
                    store.create_batch(joined.id, path="wait", kind="wake", counted=True, items=[(msg.id, 1)])
                store.insert_message(
                    room.id,
                    sender_name="meter-1",
                    sender_kind="agent",
                    via="mcp",
                    text="posted",
                    sender_membership_id=joined.id,
                    sender_harness="claude",
                )

            broker.on_loop(plant)
            yield broker
        finally:
            broker.stop()
            shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def ui(browser: Browser, meter: InProcBroker, request: pytest.FixtureRequest) -> Iterator[UI]:
    u = UI(browser, None)  # type: ignore[arg-type]
    shutil.rmtree(u.artifacts_dir(request.node.nodeid), ignore_errors=True)
    yield u
    reports = request.node.stash.get(PHASE_REPORTS, {})
    failed = any(r.failed for r in reports.values()) or bool(u.problems)
    u.finish(failed, request.node.nodeid)
    if u.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(u.problems), pytrace=False)


def open_usage(page: Page, broker: InProcBroker, phone: bool = False) -> None:
    page.goto(broker.login_url())
    expect(page.locator("#st-conn")).to_have_text("Connected")
    if phone:
        page.locator("#rooms-toggle").click()
    page.locator("#usage-nav").click()
    expect(page.locator("#room-title")).to_have_text("Usage")


@pytest.mark.parametrize("phone", [False, True])
def test_usage_shows_the_wakeups_the_messages_and_the_limit(ui: UI, meter: InProcBroker, phone: bool) -> None:
    """Two wake-ups and one message are what was stored: the tiles, the room row and the agent row say so,
    and the room's limit is 38 of 40 left, which is not near. The phone is dark and the desktop is light,
    and a room's name opens the room."""
    opts: dict[str, Any] = dict(PHONE if phone else DESKTOP)
    opts["color_scheme"] = "dark" if phone else "light"
    page = ui.context(**opts).new_page()
    open_usage(page, meter, phone)
    expect(page.locator("#usage-tile-wakes")).to_contain_text("2")
    expect(page.locator("#usage-tile-wakes")).to_contain_text("Each one starts a paid model turn")
    expect(page.locator("#usage-tile-posts")).to_contain_text("1")
    expect(page.locator("#usage-tile-busiest")).to_contain_text("meter-1")
    expect(page.locator("#usage-tile-busiest")).to_contain_text("2 wake-ups, 1 message")
    expect(page.locator("#usage-tile-limit")).to_contain_text("No limit near")
    row = page.locator("#usage-rooms tbody tr")
    expect(row).to_contain_text("38 of 40 left")
    expect(page.locator("#usage-agents")).to_contain_text("meter-1")
    expect(page.locator("#usage-agents")).to_contain_text("2 wake-ups")
    expect(page.locator("#usage-agents")).to_contain_text("1 message")
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg == ("rgb(28, 28, 31)" if phone else "rgb(255, 255, 255)")
    if phone:
        no_sideways_scroll(page)
        box = row.locator("button").bounding_box()
        assert box is not None and box["height"] >= 44
    opener = page.locator('#usage-rooms button[data-room="#meter"]')
    opener.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#room-title")).to_have_text("meter")


@pytest.fixture(scope="module")
def weekly(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[InProcBroker]:
    """A broker whose wakes fall on known days: 7 today (the room's budget of 8 has 1 left), 2 yesterday,
    1 three days ago, 1 the day before the week began, and one agent message today."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        broker = InProcBroker(home).start()
        try:

            def plant() -> None:
                store = broker.state.store
                real = store.clock
                now = float(real.now())
                today, week = window_starts(now, 0)
                room = store.create_room("#tight", "alice", 8, 0)
                person = store.upsert_participant("claude", session_key("claude", "", "tight-1"))
                joined = store.create_membership(room.id, person.id, "tight-1")
                store.set_held(joined.id, True)
                store.set_status(person.id, "offline", "test")

                def wake(counted: bool) -> None:
                    msg = store.insert_message(
                        room.id, sender_name="alice", sender_kind="human", via="web", text="go"
                    )
                    store.create_batch(
                        joined.id, path="wait", kind="wake", counted=counted, items=[(msg.id, 1)]
                    )

                clock = FakeClock(now)
                store.clock = clock  # type: ignore[assignment]
                try:
                    for when, n in ((today - 3600, 2), (today - 3 * 86_400 + 3600, 1), (week - 3600, 1)):
                        for _ in range(n):
                            clock.t = when
                            wake(False)
                finally:
                    store.clock = real
                for _ in range(7):
                    wake(True)  # now, counted: the room's 8 an hour are down to 1
                store.insert_message(
                    room.id, sender_name="tight-1", sender_kind="agent", via="mcp", text="posted",
                    sender_membership_id=joined.id, sender_harness="claude",
                )  # fmt: skip

            broker.on_loop(plant)
            yield broker
        finally:
            broker.stop()
            shutil.rmtree(home, ignore_errors=True)


def weekly_page(ui: UI, broker: InProcBroker, **opts: Any) -> Page:
    """A page in UTC, so the days the fixture planted are the days the page counts."""
    page = ui.context(timezone_id="UTC", **opts).new_page()
    open_usage(page, broker, bool(opts.get("is_mobile")))
    return page


def test_the_tiles_compare_with_the_period_before_and_the_limit_is_near(ui: UI, weekly: InProcBroker) -> None:
    """Seven wake-ups today against two yesterday reads "up 5 on yesterday"; the week's ten against the one
    before it, "up 9 on the 7 days before". The room has 1 of 8 left, so the limit tile names it. The
    bug: a tile with no comparison, or a limit that is near and says nothing."""
    page = weekly_page(ui, weekly, **DESKTOP)
    expect(page.locator("#usage-tile-wakes")).to_contain_text("↑ 5 on yesterday")
    expect(page.locator("#usage-tile-posts")).to_contain_text("↑ 1 on yesterday")
    limit = page.locator("#usage-tile-limit")
    expect(limit).to_contain_text("1 room")
    expect(limit).to_contain_text("Near its wake limit")
    expect(limit).to_contain_text("#tight: 1 of 8 left this hour")
    expect(page.locator("#usage-rooms tbody tr")).to_contain_text("1 of 8 left")
    expect(page.locator("#usage-rooms .usage-meter.low")).to_have_count(1)
    page.locator('.usage-switch-btn[data-period="week"]').click()
    expect(page.locator("#usage-tile-wakes")).to_contain_text("10")
    expect(page.locator("#usage-tile-wakes")).to_contain_text("↑ 9 on the 7 days before")


def test_the_switch_changes_the_numbers_and_stays(ui: UI, weekly: InProcBroker) -> None:
    """Today shows 7 wake-ups and Last 7 days shows 10; the chart is the same either way; after a
    opening a room and coming back keeps the choice. The bug: a switch that resets when the page is
    redrawn, or one that changes the chart. (It is not kept over a reload: the lint bans localStorage.)"""
    page = weekly_page(ui, weekly, **DESKTOP)
    today = page.locator('.usage-switch-btn[data-period="today"]')
    week = page.locator('.usage-switch-btn[data-period="week"]')
    expect(today).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#usage-tile-wakes .usage-tile-value")).to_have_text("7")
    chart = page.locator("#usage-days-table").inner_text()
    week.click()
    expect(week).to_have_attribute("aria-pressed", "true")
    expect(week).to_be_focused()
    expect(page.locator("#usage-tile-wakes .usage-tile-value")).to_have_text("10")
    assert page.locator("#usage-days-table").inner_text() == chart
    page.locator('#usage-rooms button[data-room="#tight"]').click()
    expect(page.locator("#room-title")).to_have_text("tight")
    page.locator("#usage-nav").click()
    expect(page.locator('.usage-switch-btn[data-period="week"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#usage-tile-wakes .usage-tile-value")).to_have_text("10")


def test_the_chart_has_seven_bars_and_marks_today(ui: UI, weekly: InProcBroker) -> None:
    """Seven bars, oldest first, with today's the last and the darker one; each says its day and count to
    a screen reader and on hover, and a hidden table has the same numbers. The wake-ups planted are 1
    three days ago, 2 yesterday and 7 today."""
    page = weekly_page(ui, weekly, **DESKTOP)
    bars = page.locator("#usage-chart .usage-bar")
    expect(bars).to_have_count(7)
    expect(page.locator("#usage-chart .usage-bar.today")).to_have_count(1)
    labels = [bars.nth(i).get_attribute("aria-label") for i in range(7)]
    assert [(lb or "").rsplit(": ", 1)[1] for lb in labels] == [
        "0 wake-ups", "0 wake-ups", "0 wake-ups", "1 wake-up", "0 wake-ups", "2 wake-ups", "7 wake-ups",
    ]  # fmt: skip
    assert (labels[6] or "").startswith("Today: ")
    assert bars.nth(6).get_attribute("title") == labels[6]
    assert page.locator("#usage-days-table tr").count() == 8  # a header and seven days
    heights = [float(bars.nth(i).evaluate("e => e.getBoundingClientRect().height")) for i in range(7)]
    assert heights[6] > heights[5] > heights[3] > heights[0] == 0
    today_bg = bars.nth(6).evaluate("e => getComputedStyle(e).backgroundColor")
    other_bg = bars.nth(5).evaluate("e => getComputedStyle(e).backgroundColor")
    assert today_bg != other_bg


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_a_phone_at_larger_text_does_not_scroll_sideways(ui: UI, weekly: InProcBroker, scheme: str) -> None:
    """On a phone at the largest text size the tiles go two by two, the chart and the lists stack, and
    nothing is wider than the window, in both themes. The bug: a table or a bar row that forces a
    sideways scroll at Larger."""
    page = weekly_page(ui, weekly, color_scheme=scheme, **PHONE)
    page.evaluate("document.documentElement.dataset.textSize = 'larger'")
    expect(page.locator("#usage-tile-wakes")).to_be_visible()
    cols = page.evaluate(
        "getComputedStyle(document.querySelector('.usage-tiles')).gridTemplateColumns.split(' ').length"
    )
    assert cols == 2
    no_sideways_scroll(page)
    page.locator('.usage-switch-btn[data-period="week"]').click()
    no_sideways_scroll(page)


@pytest.fixture(scope="module")
def reviewed(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[InProcBroker]:
    """A broker with one settled and merged review board, planted with times a few minutes before now so
    the 7-day window holds them whatever the hour. Two findings: one confirmed, one dropped."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        broker = InProcBroker(home).start()
        try:

            def plant() -> None:
                store = broker.state.store
                real = store.clock
                t0 = float(real.now())
                clock = FakeClock(t0)
                store.clock = clock  # type: ignore[assignment]
                try:
                    room = store.create_room("#reviewed", "alice", 40, 0)
                    for who in ("rev-1", "dev-1"):
                        person = store.upsert_participant("claude", session_key("claude", "", who))
                        store.create_membership(room.id, person.id, who)

                    def at(offset: float) -> None:
                        clock.t = t0 - offset

                    def event(rv: dict[str, Any], offset: float, **data: Any) -> None:
                        at(offset)
                        store.add_event("review", room_id=room.id, data={**data, "board": rv["id"]})

                    at(300)
                    rv = store.open_review(
                        room.id, "https://github.com/example/shop/pull/7", "aaaaaaa", "rev-1"
                    )
                    event(rv, 300, open=rv["url"], head="aaaaaaa", by="rev-1", how="opened")
                    at(280)
                    one = store.add_review_item(rv["id"], "finding", "raised", title="a", raised_by="rev-1")
                    at(270)
                    two = store.add_review_item(rv["id"], "finding", "raised", title="b", raised_by="rev-1")
                    for offset, item, to, state, fields in (
                        (200, one, "conceded", "conceded", {"owner": "dev-1"}),
                        (150, two, "dropped", "dropped", {"reason": "no"}),
                        (120, one, "fixed", "fixed", {"commit_sha": "abc1234", "proof": "t"}),
                        (100, one, "confirmed", "fixed", {"confirmed_by": "rev-1"}),
                    ):
                        at(offset)
                        store.move_review_item(item.id, state, **fields)
                        event(rv, offset, item=item.label, to=to, by="x")
                    event(rv, 50, report={"pr_state": "merged"}, by="rev-1")
                    at(50)
                    store.set_review(rv["id"], pr_state="merged", reported_by="rev-1", reported_at=clock.t)
                finally:
                    store.clock = real

            broker.on_loop(plant)
            yield broker
        finally:
            broker.stop()
            shutil.rmtree(home, ignore_errors=True)


@pytest.mark.parametrize("mode", ["light", "dark", "phone"])
def test_usage_shows_how_reviews_went(ui: UI, reviewed: InProcBroker, mode: str) -> None:
    """One board with one finding confirmed and one dropped: the page says "50% held up" and "1 of 2 settled"
    for the reviewer who raised them, in the light and dark themes and on a phone, with no sideways scroll.
    The agent who only fixed (dev-1) raised nothing and has no row."""
    opts: dict[str, Any] = dict(PHONE if mode == "phone" else DESKTOP)
    opts["color_scheme"] = "light" if mode == "light" else "dark"
    page = ui.context(**opts).new_page()
    page.goto(reviewed.login_url())
    expect(page.locator("#st-conn")).to_have_text("Connected")
    if mode == "phone":
        page.locator("#rooms-toggle").click()
    page.locator("#usage-nav").click()
    expect(page.locator("#room-title")).to_have_text("Usage")
    row = page.locator("#usage-reviews li")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("rev-1")
    expect(row).to_contain_text("2 findings")
    expect(row).to_contain_text("50% held up")
    expect(row).to_contain_text("1 of 2 settled")
    assert row.locator(".usage-stack").get_attribute("aria-label") == "1 held up, 1 dropped, 0 still open"
    expect(page.locator("#usage-reviews")).not_to_contain_text("dev-1")
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg == ("rgb(255, 255, 255)" if mode == "light" else "rgb(28, 28, 31)")
    if mode == "phone":
        no_sideways_scroll(page)
