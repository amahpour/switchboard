"""The Inspector says when an agent's plugin is old (#789, DESIGN.md §49.13).

A real page: an agent whose plugin reported a version behind the release's has a Plugin row in its
Inspector with the sentence the server wrote, update command included; one that is current has no such
row. Desktop and phone, light and dark (the row is plain text in the existing facts list).

Marker ``e2e``.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import PHASE_REPORTS, sanitize_env
from playwright.sync_api import Browser, expect
from test_web_ui import PHONE, UI, no_horizontal_scroll
from ui_world import UIWorld

from switchboard import plugin_versions

pytestmark = pytest.mark.e2e


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


def reported(ui: UI, name: str, version: str | None) -> None:
    """What a session's last call said of its plugin (the broker keeps it in memory, §49.13)."""
    b = ui.world.broker

    def put() -> None:
        st = b.state
        room = st.store.get_room("#build")
        assert room is not None
        member = next(m for m in st.store.members(room.id) if m.name == name)
        st.service.plugin_seen[member.participant_id] = version
        st.hub.members_changed("#build")

    b.on_loop(put)


@pytest.mark.parametrize("phone", [False, True], ids=["desktop", "phone"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_inspector_has_a_plugin_row_only_for_an_old_plugin(ui: UI, scheme: str, phone: bool) -> None:
    """An old plugin (and Claude Code with no version at all) gets a Plugin row with the update command;
    a current plugin gets none. The page doesn't scroll sideways on the phone. Fails if the row is missing
    for an old one, shows for a current one, or loses the command."""
    reported(ui, "claude-1", "0.1.0")
    reported(ui, "codex-1", plugin_versions.PINNED["codex"])
    args: dict[str, Any] = {"color_scheme": scheme, **(PHONE if phone else {})}
    page = ui.open(**args)
    if phone:
        page.click("#buddy-toggle")
    with page.expect_response(lambda r: "/members/claude-1" in r.url and r.request.method == "GET"):
        page.click('#buddy-list .member[data-name="claude-1"]')
    facts = page.locator("#insp-body .insp-facts")
    expect(facts).to_contain_text("Plugin")
    expect(facts).to_contain_text("older than " + plugin_versions.PINNED["claude"])
    expect(facts).to_contain_text("claude plugin update switchboard@switchboard")
    if phone:
        no_horizontal_scroll(page)
    page.click("#insp-back")
    with page.expect_response(lambda r: "/members/codex-1" in r.url and r.request.method == "GET"):
        page.click('#buddy-list .member[data-name="codex-1"]')
    expect(page.locator("#insp-body .insp-facts")).not_to_contain_text("Plugin")
    reported(ui, "claude-1", None)  # leave nothing behind for the next test
    reported(ui, "codex-1", None)
