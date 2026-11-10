"""An admin confirms identity, keeps working and downloads a ready archive (DESIGN.md §57.1)."""

from __future__ import annotations

import shutil
import sqlite3
import tarfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect
from test_web_ui import PHASE_REPORTS, PHONE, UI, chat_row, no_horizontal_scroll, open_settings
from ui_world import HostedWorld

from switchboard import backup

pytestmark = pytest.mark.e2e
ADMIN_PW = "correct horse battery"


@pytest.fixture
def world() -> Iterator[Any]:
    hosted = HostedWorld().start()
    try:
        with httpx.Client(
            base_url=hosted.origin, headers={"Origin": hosted.origin, "X-Switchboard": "1"}
        ) as client:
            one_time = hosted.claim_link().split("#t=", 1)[1]
            assert (
                client.post("/api/signin/password", json={"name": "admin", "password": one_time}).status_code
                == 200
            )
            assert client.post("/api/setup/password", json={"password": ADMIN_PW}).status_code == 200
            hosted.admin_cookie = client.cookies.get("switchboard_session")
            added = client.post("/api/people", json={"name": "bob", "email": "bob@example.com"})
            assert added.status_code == 200
            client.cookies.clear()
            signed = client.post(
                "/api/signin/password", json={"name": "bob@example.com", "password": added.json()["password"]}
            )
            assert signed.status_code == 200
            assert client.post("/api/me/password", json={"password": "bob's own secret 1"}).status_code == 200
            hosted.bob_cookie = client.cookies.get("switchboard_session")
        yield hosted
    finally:
        hosted.stop()


@pytest.fixture
def ui(browser: Browser, world: Any, request: pytest.FixtureRequest) -> Iterator[UI]:
    # Browser contexts must close before the function-scoped broker stops.
    del world
    watched = UI(browser, None)
    shutil.rmtree(watched.artifacts_dir(request.node.nodeid), ignore_errors=True)
    yield watched
    reports = request.node.stash.get(PHASE_REPORTS, {})
    failed = any(r.failed for r in reports.values()) or bool(watched.problems)
    watched.finish(failed, request.node.nodeid)
    if watched.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(watched.problems), pytrace=False)


def open_page(ui: UI, world: Any, cookie: str, *, phone: bool = False) -> Page:
    context = ui.context(**PHONE if phone else {})
    context.add_cookies([{"name": "switchboard_session", "value": cookie, "url": world.origin}])
    page = context.new_page()
    page.goto(world.origin + "/")
    expect(page.locator("#st-conn")).to_have_text("Connected")
    return page


def settings(page: Page, group: str | None = None) -> None:
    if not page.locator("#me-settings").is_visible():
        page.click("#rooms-toggle")
    open_settings(page, group)


@pytest.mark.parametrize("phone", [False, True])
def test_confirm_work_and_download_the_ready_notice(
    ui: UI, world: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phone: bool
) -> None:
    """The fresh-check dialog uses the existing password flow; a held disk worker cannot freeze the page."""
    entered, release = threading.Event(), threading.Event()
    original = backup.create

    def held(*args: Any, **kwargs: Any) -> Path:
        entered.set()
        assert release.wait(60)
        return original(*args, **kwargs)

    monkeypatch.setattr(backup, "create", held)
    world.broker.on_loop(lambda: world.broker.state.passkey_checks.clear())
    try:
        page = open_page(ui, world, world.admin_cookie, phone=phone)
        settings(page, "backups")
        expect(page.locator("#backup-start")).to_be_enabled()
        page.click("#backup-start")
        expect(page.locator("#confirm-dialog")).to_be_visible()
        page.fill("#confirm-password", ADMIN_PW)
        page.click("#confirm-ok")
        assert entered.wait(10)
        expect(page.locator("#backup-status")).to_contain_text("Backup running")
        expect(page.locator("#backup-start")).to_be_disabled()
        page.get_by_role("button", name="Close settings", exact=True).click()
        expect(page.locator("#banner-backup")).to_be_visible()  # Home keeps the global notice too
        room = page.locator('#tabs .room[data-room="#build"]')
        if phone:
            # A closing drawer can still be visible. Its expanded state is authoritative.
            toggle = page.locator("#rooms-toggle")
            if toggle.get_attribute("aria-expanded") != "true":
                toggle.click()
            expect(toggle).to_have_attribute("aria-expanded", "true")
        room.click()
        expect(page.locator("#room-title")).to_have_text("build")
        page.fill("#input", "Work continues during backup")
        page.locator("#input").press("Control+Enter")
        expect(chat_row(page, "Work continues during backup")).to_be_visible()
        release.set()
        link = page.locator("#banner-backup").get_by_role("button", name="Download archive")
        expect(link).to_be_visible()
        with page.expect_download() as received:
            link.click()
        saved = tmp_path / "download.tar"
        received.value.save_as(saved)
        with tarfile.open(saved) as archive:
            assert "switchboard.db" in archive.getnames() and "manifest.json" in archive.getnames()
            restored_db = tmp_path / "download.db"
            restored_db.write_bytes(archive.extractfile("switchboard.db").read())
        with sqlite3.connect(restored_db) as con:
            assert (
                con.execute(
                    "SELECT COUNT(*) FROM messages WHERE text=?", ("Work continues during backup",)
                ).fetchone()[0]
                == 1
            )
        expect(page.locator("#st-conn")).to_have_text("Connected")
        no_horizontal_scroll(page)
    finally:
        release.set()


def test_only_admins_get_the_backup_settings(ui: UI, world: Any) -> None:
    """The control exists for the owner and is absent for an ordinary teammate."""
    admin = open_page(ui, world, world.admin_cookie)
    settings(admin, "backups")
    expect(admin.locator("#backup-start")).to_be_visible()
    bob = open_page(ui, world, world.bob_cookie)
    settings(bob)
    expect(bob.locator("#settings-tab-backups")).to_have_count(0)
    expect(bob.locator("#settings-row-backups")).to_have_count(0)
    expect(bob.locator("#backup-start")).to_have_count(0)


def test_dismissing_the_notice_on_phone_home_keeps_focus_visible(ui: UI, world: Any) -> None:
    """Home has no composer; dismissing its notice must focus the visible Rooms control."""
    page = open_page(ui, world, world.admin_cookie, phone=True)
    settings(page, "backups")
    expect(page.locator("#backup-start")).to_be_enabled()
    page.click("#backup-start")
    expect(page.locator("#backup-download").get_by_role("button", name="Download archive")).to_be_visible()
    page.get_by_role("button", name="Close settings", exact=True).click()
    expect(page.locator("#room-title")).to_have_text("Home")
    page.get_by_role("button", name="Dismiss backup notice").click()
    expect(page.locator("#banner-backup")).to_be_hidden()
    expect(page.locator("#rooms-toggle")).to_be_focused()
