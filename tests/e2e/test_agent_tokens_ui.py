"""Agent tokens in a real browser (#382, DESIGN.md §49.1): Settings → Agents on a hosted broker.
Making a token asks the admin to confirm it's them once their check ran out, shows the token once
with Copy, and lists the device; Revoke asks on its own row; closing Settings forgets the token;
the phone gets the same panel. A hosted broker of its own, with its admin signed in through the
API. Marker ``e2e``; the ``UI`` fixture class of tests/e2e/test_web_ui.py watches every page for
console errors, page errors and CSP violations."""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from conftest import InProcBroker, make_tmp_home, sanitize_env
from playwright.sync_api import Browser, Page, expect
from test_web_ui import PHASE_REPORTS, PHONE, UI, open_settings

from switchboard.broker import agent_tokens
from switchboard.broker.auth import WebOrigin, sha256_hex

pytestmark = pytest.mark.e2e

ADMIN_PW = "correct horse battery"
TOKEN = re.compile(r"^sbat_[A-Za-z0-9_-]{43}$")


@pytest.fixture(scope="module")
def hosted(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[InProcBroker, str]]:
    """A hosted broker with its admin signed in through the API: the broker and the session cookie."""
    with pytest.MonkeyPatch.context() as mp:
        sanitize_env(mp, tmp_path_factory)
        home = make_tmp_home()
        b = InProcBroker(home, web_origin=lambda port: WebOrigin.parse(f"http://sb.localhost:{port}")).start()
        origin = b.state.web_origin.origin
        one_time = b.paths.test_claim_link.read_text().strip().split("#t=", 1)[1]
        h = {"Origin": origin, "X-Switchboard": "1"}
        with httpx.Client(base_url=origin, headers=h) as c:
            assert (
                c.post("/api/signin/password", json={"name": "admin", "password": one_time}).status_code
                == 200
            )
            assert c.post("/api/setup/password", json={"password": ADMIN_PW}).status_code == 200
            cookie = c.cookies.get("switchboard_session")
        assert cookie
        try:
            yield b, cookie
        finally:
            b.stop()
            shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def ui(browser: Browser, request: pytest.FixtureRequest) -> Iterator[UI]:
    u = UI(browser, None)  # type: ignore[arg-type]  # no seeded world: the pages are opened by hand
    shutil.rmtree(u.artifacts_dir(request.node.nodeid), ignore_errors=True)
    yield u
    reports = request.node.stash.get(PHASE_REPORTS, {})
    failed = any(r.failed for r in reports.values()) or bool(u.problems)
    u.finish(failed, request.node.nodeid)
    if u.problems:
        pytest.fail("the browser reported problems:\n  " + "\n  ".join(u.problems), pytrace=False)


def open_page(ui: UI, hosted: tuple[InProcBroker, str], **options: Any) -> Page:
    b, cookie = hosted
    origin = b.state.web_origin.origin
    ctx = ui.context(**options)
    ctx.add_cookies([{"name": "switchboard_session", "value": cookie, "url": origin}])
    page = ctx.new_page()
    page.goto(origin + "/")
    expect(page.locator("#st-conn")).to_have_text("Connected")
    return page


def checked_now(hosted: tuple[InProcBroker, str]) -> None:
    """The admin's session passed its password check just now (so no Confirm it's you)."""
    b, cookie = hosted
    b.on_loop(lambda: b.state.passkey_checks.__setitem__(sha256_hex(cookie), b.state.clock.now()))


def revoke_all(hosted: tuple[InProcBroker, str]) -> None:
    b, _ = hosted
    for t in b.on_loop(lambda: b.state.store.agent_tokens_of(None)):
        b.on_loop(lambda t=t: b.state.store.agent_token_revoke(t.id, None))


def test_make_a_token_after_confirming_see_it_once_and_revoke_it(
    ui: UI, hosted: tuple[InProcBroker, str]
) -> None:
    """The whole round on a desktop-sized page. Fails if a bad name is sent, if making a token
    skips Confirm it's you once the check ran out, if the token shown isn't the one stored (by
    hash), if Revoke acts on the first click, or if focus is lost along the way."""
    b, _ = hosted
    revoke_all(hosted)
    page = open_page(ui, hosted)
    open_settings(page, "agents")
    panel = page.locator("#settings-panel-agents")
    expect(panel.locator(".passkey-count")).to_have_text("none yet")

    page.fill("#agent-token-device", "1 bad")
    page.click("#agent-token-make")
    expect(page.locator("#agent-token-result")).to_contain_text("looks like work-laptop")
    expect(page.locator("#agent-token-device")).to_be_focused()

    b.on_loop(lambda: b.state.passkey_checks.clear())  # the admin's check ran out
    page.fill("#agent-token-device", "Work-Laptop")
    page.click("#agent-token-make")
    expect(page.locator("#confirm-dialog")).to_be_visible()
    page.fill("#confirm-password", ADMIN_PW)
    page.click("#confirm-ok")
    expect(page.locator("#confirm-dialog")).to_be_hidden()

    shown = page.locator("#agent-token-value")
    expect(shown).to_have_text(TOKEN)
    expect(page.locator("#agent-token-copy")).to_be_focused()
    rows = page.locator("#agent-tokens .agent-token")
    expect(rows).to_have_count(1)
    expect(rows.first.locator(".agent-token-device")).to_have_text("work-laptop")
    expect(rows.first.locator(".agent-token-meta")).to_contain_text("never used")
    expect(panel.locator(".passkey-count")).to_have_text("1 device")
    token = shown.inner_text()
    row = b.on_loop(lambda: b.state.store.agent_token_for_device("work-laptop"))
    assert row is not None and row.token_hash == agent_tokens.token_hash(token)

    page.click(f"#agent-token-ask-{row.id}")
    expect(page.locator(f"#agent-token-keep-{row.id}")).to_be_focused()
    expect(page.locator(f"#agent-token-revoke-{row.id}")).to_be_visible()
    page.click(f"#agent-token-keep-{row.id}")
    expect(page.locator(f"#agent-token-ask-{row.id}")).to_be_focused()
    assert b.on_loop(lambda: b.state.store.agent_token_by_hash(row.token_hash)) is not None  # kept

    page.click(f"#agent-token-ask-{row.id}")
    page.click(f"#agent-token-revoke-{row.id}")
    expect(page.locator("#agent-token-result")).to_have_text("Revoked the token for work-laptop.")
    expect(rows).to_have_count(0)
    expect(page.locator("#agent-token-new")).to_have_count(0)  # the token it showed goes with it
    expect(page.locator("#agent-token-device")).to_be_focused()
    assert b.on_loop(lambda: b.state.store.agent_token_by_hash(row.token_hash)) is None


def test_closing_settings_forgets_the_token_it_showed(ui: UI, hosted: tuple[InProcBroker, str]) -> None:
    """A token is shown once: reopened, Settings lists the device and nothing else of it, and the
    page holds the token nowhere. Fails if the token survives a close."""
    revoke_all(hosted)
    checked_now(hosted)
    page = open_page(ui, hosted)
    open_settings(page, "agents")
    page.fill("#agent-token-device", "desk")
    page.click("#agent-token-make")
    expect(page.locator("#agent-token-value")).to_have_text(TOKEN)
    token = page.locator("#agent-token-value").inner_text()
    page.keyboard.press("Escape")
    expect(page.locator("#app-dialog")).to_be_hidden()
    open_settings(page, "agents")
    expect(page.locator("#agent-tokens .agent-token-device")).to_have_text(["desk"])
    expect(page.locator("#agent-token-new")).to_have_count(0)
    assert token not in page.content()
    revoke_all(hosted)


def test_the_phone_gets_the_same_panel_without_a_sideways_scroll(
    ui: UI, hosted: tuple[InProcBroker, str]
) -> None:
    """On a phone, Agents is a row of the Settings list with a summary, and its panel (with a
    48-character token showing) fits the screen's width. Fails if the row is missing, or the token
    pushes the page sideways."""
    revoke_all(hosted)
    checked_now(hosted)
    page = open_page(ui, hosted, **PHONE)
    page.click("#rooms-toggle")
    page.click("#me-settings")
    row = page.locator("#settings-row-agents")
    expect(row).to_be_visible()
    expect(row.locator(".settings-row-sub")).to_have_text("Tokens for your agents")
    row.click()
    page.fill("#agent-token-device", "phone-box")
    page.click("#agent-token-make")
    expect(page.locator("#agent-token-value")).to_have_text(TOKEN)
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
    box = page.locator("#agent-token-value").bounding_box()
    assert box is not None and box["x"] + box["width"] <= PHONE["viewport"]["width"]
    revoke_all(hosted)


def test_the_new_token_comes_with_the_steps_to_connect_an_agent(
    ui: UI, hosted: tuple[InProcBroker, str]
) -> None:
    """Under the token shown once, Connect an agent gives the line to run in Claude Code (the plugin,
    from this repository's marketplace) and this switchboard's own address, each with Copy (#384).
    Fails if the steps are missing, name another address, or go when the token does."""
    b, _ = hosted
    revoke_all(hosted)
    checked_now(hosted)
    page = open_page(ui, hosted)
    open_settings(page, "agents")
    page.fill("#agent-token-device", "studio")
    page.click("#agent-token-make")
    connect = page.locator("#agent-connect")
    expect(connect).to_contain_text("Connect an agent on studio")
    expect(page.locator("#agent-connect-claude")).to_have_text(
        "/plugin install switchboard --marketplace amahpour/switchboard"
    )
    expect(page.locator("#agent-connect-server")).to_have_text(b.state.web_origin.origin)
    expect(page.locator("#agent-connect-claude-copy")).to_be_visible()
    expect(page.locator("#agent-connect-server-copy")).to_be_visible()
    page.keyboard.press("Escape")
    open_settings(page, "agents")
    expect(page.locator("#agent-connect")).to_have_count(0)  # it goes with the token it was for
    revoke_all(hosted)


@pytest.mark.parametrize("phone", [False, True])
def test_agents_explains_devins_idle_limit_before_connecting(
    ui: UI, hosted: tuple[InProcBroker, str], phone: bool
) -> None:
    """#514: people see Devin's listening limit before making a token, and the recovery instruction
    beside its setup steps. The notice must remain readable on a phone."""
    revoke_all(hosted)
    checked_now(hosted)
    page = open_page(ui, hosted, **(PHONE if phone else {}))
    if phone:
        page.click("#rooms-toggle")
    open_settings(page, "agents")
    notice = page.locator("#agent-devin-idle-limit")
    expect(notice).to_be_visible()
    expect(notice).to_contain_text("only while wait() is open")
    expect(notice).to_contain_text("2 per prompt and 12 per hour per session by default")
    expect(notice).to_contain_text("type in Devin")
    page.fill("#agent-token-device", "devin-box")
    page.click("#agent-token-make")
    step = page.locator("#agent-connect-devin")
    expect(step).to_contain_text("plugins/devin/")
    expect(step).to_contain_text("read #build and go back to wait()")
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
    page.keyboard.press("Escape")
    revoke_all(hosted)


def test_connect_gives_codex_its_plugin_its_server_and_its_token(
    ui: UI, hosted: tuple[InProcBroker, str]
) -> None:
    """Codex's steps: the plugin (the hooks), this switchboard in config.toml (a Codex plugin can't
    hold an address), and in the environment the token that config names and the address, which the
    plugin's approval hook (a command) reads. Fails if any line names another address or another
    variable, or misses the token just made."""
    b, _ = hosted
    revoke_all(hosted)
    checked_now(hosted)
    page = open_page(ui, hosted)
    open_settings(page, "agents")
    page.fill("#agent-token-device", "build-box")
    page.click("#agent-token-make")
    token = page.locator("#agent-token-value").inner_text()
    expect(page.locator("#agent-connect-codex")).to_have_text(
        "codex plugin marketplace add amahpour/switchboard && codex plugin add switchboard@switchboard"
    )
    origin = b.state.web_origin.origin
    config = page.locator("#agent-connect-codex-config").inner_text()
    assert config.splitlines() == [
        "[mcp_servers.switchboard]",
        f'url = "{origin}/mcp"',
        'bearer_token_env_var = "SWITCHBOARD_AGENT_TOKEN"',
        "tool_timeout_sec = 86400",
    ]
    # the address is for the plugin's approval hook, a command that reads it from the environment (#472)
    assert page.locator("#agent-connect-codex-env").inner_text().splitlines() == [
        f"export SWITCHBOARD_URL={origin}",
        f"export SWITCHBOARD_AGENT_TOKEN={token}",
    ]
    page.keyboard.press("Escape")
    revoke_all(hosted)


def test_connect_gives_opencode_v2_its_plugin_and_environment_token(
    ui: UI, hosted: tuple[InProcBroker, str]
) -> None:
    """OpenCode's copyable config has this broker and the local V2 plugin; the credential stays in the
    environment."""
    import json

    b, _ = hosted
    revoke_all(hosted)
    checked_now(hosted)
    page = open_page(ui, hosted)
    open_settings(page, "agents")
    page.fill("#agent-token-device", "opencode-box")
    page.click("#agent-token-make")
    token = page.locator("#agent-token-value").inner_text()
    config = json.loads(page.locator("#agent-connect-opencode-config").inner_text())
    assert config["plugins"] == ["./.opencode-switchboard"]
    assert config["mcp"]["servers"]["switchboard"] == {
        "type": "remote",
        "url": b.state.web_origin.origin + "/mcp",
        "oauth": False,
        "headers": {"Authorization": "Bearer {env:SWITCHBOARD_AGENT_TOKEN}"},
    }
    assert token not in json.dumps(config)
    expect(page.locator("#agent-connect-opencode-config-copy")).to_be_visible()
    assert page.locator("#agent-connect-opencode-env").inner_text().splitlines() == [
        f"export SWITCHBOARD_URL={b.state.web_origin.origin}",
        f"export SWITCHBOARD_AGENT_TOKEN={token}",
    ]
    page.keyboard.press("Escape")
    open_settings(page, "agents")
    expect(page.locator("#agent-connect-opencode-config")).to_have_count(0)
    revoke_all(hosted)
