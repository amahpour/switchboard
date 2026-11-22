"""Settings → Profile → Webhook (#723, DESIGN.md §62), on the real page.

The receiver is a local HTTPS server (``tests/fakes/tls_hook.py``) the broker is told to trust and to resolve
to the loopback, with the operator's "allow private addresses" on only inside the tests that need it. Checks
what a person sees and what the page never holds: the address is never shown back, a refused address and a
failed post say so in the person's own settings, and the layout fits in light, dark and on a phone. Marker
``e2e``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from conftest import PHASE_REPORTS, sanitize_env
from fakes.tls_hook import HOST, TlsHook
from playwright.sync_api import Browser, Page, expect
from test_web_ui import PHONE, UI
from ui_world import UIWorld

from switchboard import notify
from switchboard.config import NotifyCfg

pytestmark = pytest.mark.e2e


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
def hook(world: UIWorld) -> Iterator[TlsHook]:
    h = TlsHook()
    st = world.broker.state

    def arm(private: bool) -> None:
        st.cfg = st.cfg.replace(notify=NotifyCfg(allow_private_webhooks=private))
        st.notifier.resolve = h.resolve
        st.notifier.context = h.context

    world.broker.on_loop(arm, True)
    h.arm = arm  # type: ignore[attr-defined]
    try:
        yield h
    finally:
        h.stop()

        def reset() -> None:
            st.notifier._policy.clear()
            st.notifier._limit.pop(0, None)  # the next test starts with the hour's posts unspent

        world.broker.on_loop(reset)
        web = world.broker.web_client()
        try:
            web.delete("/api/me/webhook", headers=world.broker.write_headers())
        finally:
            web.close()
        world.broker.on_loop(lambda: setattr(st, "cfg", st.cfg.replace(notify=NotifyCfg())))


@pytest.fixture
def ui(browser: Browser, world: UIWorld, request: pytest.FixtureRequest) -> Iterator[UI]:
    import shutil

    u = UI(browser, world)
    shutil.rmtree(u.artifacts_dir(request.node.nodeid), ignore_errors=True)
    yield u
    reports = request.node.stash.get(PHASE_REPORTS, {})
    failed = any(r.failed for r in reports.values()) or bool(u.problems)
    u.finish(failed, request.node.nodeid)
    if u.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(u.problems), pytrace=False)


def open_settings(ui: UI, **args: Any) -> Page:
    page = ui.open(room=None, **args)
    if not page.locator("#me-settings").is_visible():
        page.click("#rooms-toggle")
    page.click("#me-settings")
    row = page.locator("#settings-row-profile")
    if row.is_visible():
        row.click()
    field = page.locator("#settings-webhook-url")
    field.scroll_into_view_if_needed()
    expect(field).to_be_visible()
    return page


def test_save_test_and_remove_never_shows_the_address_back(ui: UI, hook: TlsHook) -> None:
    """Saving shows the host and clears the field; the secret path is nowhere on the page; "Send a test" posts
    and says so; Remove clears it. Fails if the address comes back into the page, or nothing is posted."""
    page = open_settings(ui)
    expect(page.locator("#settings-webhook-test")).to_be_disabled()
    page.fill("#settings-webhook-url", hook.url)
    page.fill("#settings-webhook-wait", "3")
    page.click("#settings-webhook-save")
    expect(page.locator("#settings-webhook-status")).to_have_text("Saved.")
    expect(page.locator("#settings-webhook-note")).to_contain_text(HOST)
    expect(page.locator("#settings-webhook-url")).to_have_value("")
    assert "secretsecret" not in page.content() and "s3cr3t" not in page.content()
    page.click("#settings-webhook-test")
    expect(page.locator("#settings-webhook-status")).to_have_text("The test went through.")
    assert len(hook.posts) == 1 and "test" in hook.posts[0]["body"]["text"]
    page.reload()
    page = open_settings(ui)
    expect(page.locator("#settings-webhook-wait")).to_have_value("3")
    assert "secretsecret" not in page.content()
    page.click("#settings-webhook-remove")
    expect(page.locator("#settings-webhook-status")).to_have_text("Removed.")
    expect(page.locator("#settings-webhook-test")).to_be_disabled()


def test_a_failed_post_and_a_refused_address_are_said_in_settings(ui: UI, hook: TlsHook) -> None:
    """A receiver that errors shows "The test failed" in red with a fixed sentence and no address. An address
    on the loopback is refused when private addresses are off. Fails if either is silent or leaks it."""
    page = open_settings(ui)
    page.fill("#settings-webhook-url", hook.url)
    page.click("#settings-webhook-save")
    expect(page.locator("#settings-webhook-status")).to_have_text("Saved.")
    hook.status = 500
    page.click("#settings-webhook-test")
    status = page.locator("#settings-webhook-status")
    expect(status).to_have_text("The test failed: The webhook answered with an error.")
    expect(status).to_have_class("fine bad")
    assert "secretsecret" not in page.content()
    hook.arm(False)  # type: ignore[attr-defined]
    page.fill("#settings-webhook-url", f"https://127.0.0.1:{hook.port}/x")
    page.click("#settings-webhook-save")
    expect(status).to_contain_text("may not post to")
    expect(status).to_have_class("fine bad")
    page.fill("#settings-webhook-url", "http://hooks.example.com/x")
    page.click("#settings-webhook-save")
    expect(status).to_contain_text("https")
    # the two refusals are the point of this test; the browser logs each 400 as a console error
    refused = [p for p in ui.problems if "400 (Bad Request)" in p and "/api/me/webhook" in p]
    assert len(refused) == 2, ui.problems
    ui.problems[:] = [p for p in ui.problems if p not in refused]


def test_the_form_fits_in_light_dark_and_on_a_phone(ui: UI, hook: TlsHook) -> None:
    """The Webhook section is reachable and nothing overflows sideways in light, dark and on a phone. Fails if
    the field or its buttons spill off the screen."""
    for args in (
        {"color_scheme": "light"},
        {"color_scheme": "dark"},
        {**PHONE, "color_scheme": "light"},
    ):
        page = open_settings(ui, **args)
        save, remove = page.locator("#settings-webhook-save"), page.locator("#settings-webhook-remove")
        expect(save).to_be_visible()
        expect(remove).to_be_visible()
        width = page.evaluate("window.innerWidth")
        for sel in (
            "#settings-webhook-url",
            "#settings-webhook-save",
            "#settings-webhook-test",
            "#settings-webhook-remove",
            "#settings-webhook-second-url",
            "#settings-webhook-second-save",
            "#settings-webhook-second-remove",
            "#settings-webhook-quiet-from",
            "#settings-webhook-quiet-to",
            "#settings-webhook-quiet-tz",
            "#settings-webhook-quiet-save",
            "#settings-webhook-quiet-remove",
        ):
            box = page.locator(sel).bounding_box()
            assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= width + 1, (
                sel,
                box,
                width,
            )


def second_url(hook: TlsHook) -> str:
    return hook.url.replace("secretsecret", "loudloud")


def test_the_second_alert_and_quiet_hours_save_show_and_remove(ui: UI, hook: TlsHook) -> None:
    """#923: with a webhook saved, a second address and quiet hours save, show only the host and the hours
    (the zone proposed from the browser's own), last through a reload, and remove. Fails if either part does
    not reach the server, the second address shows back, or the browser's zone is not proposed."""
    page = open_settings(ui, timezone_id="America/New_York")
    expect(page.locator("#settings-webhook-second-save")).to_be_disabled()
    expect(page.locator("#settings-webhook-quiet-save")).to_be_disabled()
    page.fill("#settings-webhook-url", hook.url)
    page.click("#settings-webhook-save")
    expect(page.locator("#settings-webhook-status")).to_have_text("Saved.")
    expect(page.locator("#settings-webhook-quiet-tz")).to_have_value("America/New_York")
    expect(page.locator("#settings-webhook-quiet-always-approval")).to_be_checked()
    expect(page.locator("#settings-webhook-quiet-always-question")).not_to_be_checked()
    page.fill("#settings-webhook-second-url", second_url(hook))
    page.fill("#settings-webhook-second-after", "30")
    page.click("#settings-webhook-second-save")
    expect(page.locator("#settings-webhook-second-status")).to_have_text("Second alert saved.")
    expect(page.locator("#settings-webhook-second-note")).to_contain_text(HOST)
    expect(page.locator("#settings-webhook-second-url")).to_have_value("")
    page.fill("#settings-webhook-quiet-from", "21:30")
    page.fill("#settings-webhook-quiet-to", "06:45")
    page.check("#settings-webhook-quiet-always-board")
    page.click("#settings-webhook-quiet-save")
    expect(page.locator("#settings-webhook-quiet-status")).to_have_text("Quiet hours saved.")
    expect(page.locator("#settings-webhook-quiet-note")).to_have_text(
        "Quiet from 21:30 to 06:45, America/New_York."
    )
    assert "loudloud" not in page.content() and "s3cr3t" not in page.content()
    page.click("#settings-webhook-test")
    expect(page.locator("#settings-webhook-status")).to_have_text(
        "The test went through to the first address."
    )
    expect(page.locator("#settings-webhook-second-status")).to_have_text(
        "The test went through to the second address."
    )
    assert [p["body"].get("alert") for p in hook.posts] == [None, "second"]
    page.reload()
    page = open_settings(ui)
    expect(page.locator("#settings-webhook-second-after")).to_have_value("30")
    expect(page.locator("#settings-webhook-quiet-from")).to_have_value("21:30")
    expect(page.locator("#settings-webhook-quiet-always-board")).to_be_checked()
    page.click("#settings-webhook-second-remove")
    expect(page.locator("#settings-webhook-second-status")).to_have_text("Second alert removed.")
    expect(page.locator("#settings-webhook-second-note")).to_have_text("No second alert.")
    expect(page.locator("#settings-webhook-second-save")).to_be_focused()
    page.click("#settings-webhook-quiet-remove")
    expect(page.locator("#settings-webhook-quiet-status")).to_have_text("Quiet hours turned off.")
    expect(page.locator("#settings-webhook-quiet-note")).to_have_text("No quiet hours.")
    expect(page.locator("#settings-webhook-quiet-remove")).to_be_disabled()


def test_an_unknown_zone_and_an_internal_second_address_are_refused_in_settings(
    ui: UI, hook: TlsHook
) -> None:
    """#923: a time zone the server does not know and a second address on the loopback (private addresses
    off) are refused, each saying why in its own part. Fails if either is saved or silent."""
    page = open_settings(ui)
    page.fill("#settings-webhook-url", hook.url)
    page.click("#settings-webhook-save")
    expect(page.locator("#settings-webhook-status")).to_have_text("Saved.")
    page.fill("#settings-webhook-quiet-tz", "Mars/Olympus_Mons")
    page.click("#settings-webhook-quiet-save")
    status = page.locator("#settings-webhook-quiet-status")
    expect(status).to_have_text("That time zone is not one this switchboard knows.")
    expect(status).to_have_class("fine bad")
    hook.arm(False)  # type: ignore[attr-defined]
    page.fill("#settings-webhook-second-url", f"https://127.0.0.1:{hook.port}/x")
    page.click("#settings-webhook-second-save")
    expect(page.locator("#settings-webhook-second-status")).to_contain_text("The second address")
    expect(page.locator("#settings-webhook-second-note")).to_have_text("No second alert.")
    refused = [p for p in ui.problems if "400 (Bad Request)" in p and "/api/me/webhook" in p]
    assert len(refused) == 2, ui.problems
    ui.problems[:] = [p for p in ui.problems if p not in refused]


def save_both(page: Page, hook: TlsHook) -> None:
    page.fill("#settings-webhook-url", hook.url)
    page.click("#settings-webhook-save")
    expect(page.locator("#settings-webhook-status")).to_have_text("Saved.")
    page.fill("#settings-webhook-second-url", second_url(hook))
    page.click("#settings-webhook-second-save")
    expect(page.locator("#settings-webhook-second-status")).to_have_text("Second alert saved.")


def test_send_a_test_shows_each_addresses_own_result_from_the_answer(ui: UI, hook: TlsHook) -> None:
    """#923 (F7): with the first address failing and the second working, Send a test says the first failed in
    its section and the second went through in its own; later, with the hour's posts spent, the second section
    says it was capped, not the stored "went through" of the last try. Fails if the first failure hides the
    second result, or the second section shows a stored status instead of this test's."""
    page = open_settings(ui)
    save_both(page, hook)
    first, second = page.locator("#settings-webhook-status"), page.locator("#settings-webhook-second-status")
    hook.status_of["/services/T000/B000/secretsecret?token=s3cr3t"] = 500
    page.click("#settings-webhook-test")
    expect(first).to_have_text("The test failed at the first address: The webhook answered with an error.")
    expect(first).to_have_class("fine bad")
    expect(second).to_have_text("The test went through to the second address.")
    expect(second).to_have_class("fine")
    hook.status_of.clear()
    for _ in range(notify.MAX_PER_HOUR // 2 - 1):  # the rest of the hour's posts
        page.click("#settings-webhook-test")
        expect(second).to_have_text("The test went through to the second address.")
    page.click("#settings-webhook-test")
    capped = "The test failed at the second address: " + notify.fail_reason("limit")
    expect(second).to_have_text(capped)
    expect(second).to_have_class("fine bad")
    expect(first).to_have_text("The test failed at the first address: " + notify.fail_reason("limit"))
    assert len(hook.posts) == notify.MAX_PER_HOUR


def test_a_save_keeps_what_is_typed_in_the_other_sections(ui: UI, hook: TlsHook) -> None:
    """#923 (F8): a save fills again only its own section; what is typed, unsaved, in the others stays, and
    their labels follow (the second and quiet parts open up once there is an address). Fails if any save
    redraws all three forms and clears a draft."""
    page = open_settings(ui)
    page.fill("#settings-webhook-url", hook.url)
    page.click("#settings-webhook-save")
    expect(page.locator("#settings-webhook-status")).to_have_text("Saved.")
    expect(page.locator("#settings-webhook-second-save")).to_be_enabled()
    expect(page.locator("#settings-webhook-second-note")).to_have_text("No second alert.")
    page.fill("#settings-webhook-second-url", second_url(hook))
    page.fill("#settings-webhook-second-after", "45")
    page.fill("#settings-webhook-quiet-from", "23:30")
    page.fill("#settings-webhook-wait", "7")
    page.click("#settings-webhook-save")
    expect(page.locator("#settings-webhook-status")).to_have_text("Saved.")
    expect(page.locator("#settings-webhook-wait")).to_have_value("7")
    expect(page.locator("#settings-webhook-second-url")).to_have_value(second_url(hook))
    expect(page.locator("#settings-webhook-second-after")).to_have_value("45")
    expect(page.locator("#settings-webhook-quiet-from")).to_have_value("23:30")
    page.fill("#settings-webhook-url", "https://draft.example.com/x")
    page.click("#settings-webhook-quiet-save")
    expect(page.locator("#settings-webhook-quiet-status")).to_have_text("Quiet hours saved.")
    expect(page.locator("#settings-webhook-url")).to_have_value("https://draft.example.com/x")
    expect(page.locator("#settings-webhook-second-url")).to_have_value(second_url(hook))
    expect(page.locator("#settings-webhook-second-after")).to_have_value("45")
    page.click("#settings-webhook-second-save")
    expect(page.locator("#settings-webhook-second-status")).to_have_text("Second alert saved.")
    expect(page.locator("#settings-webhook-second-url")).to_have_value("")
    expect(page.locator("#settings-webhook-second-note")).to_contain_text(HOST)
    expect(page.locator("#settings-webhook-quiet-status")).to_have_text("Quiet hours saved.")
    expect(page.locator("#settings-webhook-quiet-from")).to_have_value("23:30")
    expect(page.locator("#settings-webhook-url")).to_have_value("https://draft.example.com/x")
    assert "loudloud" not in page.content()


def test_a_saved_webhook_that_is_not_used_says_why_and_can_be_fixed_or_removed(ui: UI, hook: TlsHook) -> None:
    """#923 (F12): a saved webhook whose quiet hours' zone the server no longer knows is not used. The page
    says so in one line, naming the zone and what to do; Send a test is off, Remove and the quiet-hours
    controls stay on. Turning quiet hours off brings Send a test back; made unusable again, Remove removes it.
    Fails if such a webhook shows as none (nothing to fix or remove), or the line or a control is missing."""
    st = ui.world.broker.state
    page = open_settings(ui)
    page.fill("#settings-webhook-url", hook.url)
    page.click("#settings-webhook-save")
    expect(page.locator("#settings-webhook-status")).to_have_text("Saved.")
    page.fill("#settings-webhook-quiet-tz", "UTC")
    page.click("#settings-webhook-quiet-save")
    expect(page.locator("#settings-webhook-quiet-status")).to_have_text("Quiet hours saved.")
    problem = page.locator("#settings-webhook-problem")
    expect(problem).to_be_hidden()

    def lose_zone() -> None:
        st.store.con.execute("UPDATE webhooks SET quiet_tz='Mars/Olympus_Mons' WHERE person=0")

    ui.world.broker.on_loop(lose_zone)
    page = open_settings(ui)
    problem = page.locator("#settings-webhook-problem")
    expect(problem).to_be_visible()
    expect(problem).to_have_text(
        "This webhook is not used: its quiet hours are in a time zone this switchboard does not know"
        " (Mars/Olympus_Mons). Turn quiet hours off, or save a time zone it knows, below."
    )
    expect(page.locator("#settings-webhook-note")).to_have_text(
        f"Saved for {HOST}, and not used until it is fixed."
    )
    expect(page.locator("#settings-webhook-test")).to_be_disabled()
    expect(page.locator("#settings-webhook-remove")).to_be_enabled()
    expect(page.locator("#settings-webhook-quiet-remove")).to_be_enabled()
    expect(page.locator("#settings-webhook-quiet-save")).to_be_enabled()
    expect(page.locator("#settings-webhook-quiet-tz")).to_have_value("Mars/Olympus_Mons")
    page.click("#settings-webhook-quiet-remove")
    expect(page.locator("#settings-webhook-quiet-status")).to_have_text("Quiet hours turned off.")
    expect(problem).to_be_hidden()
    expect(page.locator("#settings-webhook-test")).to_be_enabled()
    expect(page.locator("#settings-webhook-note")).to_contain_text("Posting to " + HOST)
    page.fill("#settings-webhook-quiet-tz", "UTC")
    page.click("#settings-webhook-quiet-save")
    expect(page.locator("#settings-webhook-quiet-status")).to_have_text("Quiet hours saved.")
    ui.world.broker.on_loop(lose_zone)
    page = open_settings(ui)
    problem = page.locator("#settings-webhook-problem")
    expect(problem).to_be_visible()
    page.click("#settings-webhook-remove")
    expect(page.locator("#settings-webhook-status")).to_have_text("Removed.")
    expect(problem).to_be_hidden()
    expect(page.locator("#settings-webhook-remove")).to_be_disabled()
    assert ui.world.broker.on_loop(lambda: st.store.webhook_get(0)) is None
