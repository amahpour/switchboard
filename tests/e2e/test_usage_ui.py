"""The usage page in a real browser (#818, DESIGN.md §61): the wakes, the posts and the budget
the room has left, on a phone in the dark theme and on a desktop in the light one. Opening a
row opens that room. A console error, a page error or a CSP violation fails the test."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import FakeClock, InProcBroker, make_tmp_home, sanitize_env
from playwright.sync_api import Browser, Page, expect
from test_web_ui import DESKTOP, PHASE_REPORTS, PHONE, UI

from switchboard.models import session_key

pytestmark = pytest.mark.e2e


def no_sideways_scroll(page: Page) -> None:
    """The page does not scroll sideways, and no usage control sticks out past the right edge."""
    wide = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert wide <= 0, f"the page scrolls sideways by {wide}px"
    out = page.evaluate(
        """() => Array.from(document.querySelectorAll('#usage button, #usage .usage-name')).filter(
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


@pytest.mark.parametrize("phone", [False, True])
def test_usage_shows_the_wakes_the_posts_and_the_budget(ui: UI, meter: InProcBroker, phone: bool) -> None:
    """Two wakes and one post are what was stored, and the budget is what those wakes left.
    The phone is dark and the desktop is light, and a row opens its room."""
    opts: dict[str, Any] = dict(PHONE if phone else DESKTOP)
    opts["color_scheme"] = "dark" if phone else "light"
    page = ui.context(**opts).new_page()
    page.goto(meter.login_url())
    expect(page.locator("#st-conn")).to_have_text("Connected")
    if phone:
        page.locator("#rooms-toggle").click()
    page.locator("#usage-nav").click()
    expect(page.locator("#room-title")).to_have_text("Usage")
    row = page.locator("#usage-rooms li")
    expect(row).to_contain_text("38 of 40 left this hour")
    expect(row).to_contain_text("Today: 2 wakes, 1 message")
    expect(row).to_contain_text("7 days: 2 wakes, 1 message")
    expect(page.locator("#usage-agents")).to_contain_text("meter-1")
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
    """One board settled in 200 s and merged 50 s after, with one finding confirmed and one dropped: the
    page says so by room and by agent, in the light and dark themes and on a phone, with no sideways
    scroll. The agent who only fixed (dev-1) raised nothing and has no row."""
    opts: dict[str, Any] = dict(PHONE if mode == "phone" else DESKTOP)
    opts["color_scheme"] = "light" if mode == "light" else "dark"
    page = ui.context(**opts).new_page()
    page.goto(reviewed.login_url())
    expect(page.locator("#st-conn")).to_have_text("Connected")
    if mode == "phone":
        page.locator("#rooms-toggle").click()
    page.locator("#usage-nav").click()
    expect(page.locator("#room-title")).to_have_text("Usage")
    for list_id in ("#usage-review-rooms li", "#usage-review-agents li"):
        row = page.locator(list_id)
        expect(row).to_have_count(1)
        expect(row).to_contain_text("Boards: 1 opened, 1 settled, median 3 min to settle")
        expect(row).to_contain_text(
            "Findings: 2 raised, 1 confirmed, 0 contested, 1 dropped \u00b7 50% confirmed"
        )
        expect(row).to_contain_text("Fixes: 0 reopened, median 1 commit before settling")
        expect(row).to_contain_text(
            "Time to merge: first finding 20 s \u2192 settled 3 min \u2192 merged 50 s"
        )
        expect(row).to_contain_text("Merged: 1 reported, 0 unknown")
    expect(page.locator("#usage-review-agents")).to_contain_text("rev-1")
    expect(page.locator("#usage-review-agents")).not_to_contain_text("dev-1")
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg == ("rgb(255, 255, 255)" if mode == "light" else "rgb(28, 28, 31)")
    if mode == "phone":
        no_sideways_scroll(page)
