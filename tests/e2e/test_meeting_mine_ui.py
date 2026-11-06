"""The room's private host handoff through the real composer (DESIGN.md §54)."""

from __future__ import annotations

import base64
import hashlib

import httpx
import pytest
from conftest import InProcBroker
from playwright.sync_api import expect
from test_meetings_ui import _open
from test_meetings_ui import calls as calls
from test_meetings_ui import ui as ui
from test_web_ui import PHONE, UI, no_horizontal_scroll

from switchboard.broker import agent_tokens
from switchboard.meeting import keys
from switchboard.models import session_key

pytestmark = pytest.mark.e2e
LINK = "https://example.com.zoom.us/j/123456789?pwd=example-only"


def test_private_handoff_from_composer_and_help_in_light_dark_and_phone(ui: UI, calls: InProcBroker) -> None:
    """The palette explains mine; submitting it leaves only link-free notices/instructions, no server
    call, then the authenticated host takes it once and its relay controls the visible bar. A real page
    catches regressions in command submission, rendering, and secret exposure in all three layouts."""
    b = calls
    for theme, options in (("light", {}), ("dark", {"color_scheme": "dark"}), ("phone", PHONE)):
        room_name = f"#mine-{theme}"

        def plant(room_name: str = room_name, theme: str = theme) -> str:
            state, store = b.state, b.state.store
            room = state.service.create_room(room_name)
            token = agent_tokens.make(state, None, f"alice-{theme}", "alice")["token"]
            p = store.upsert_participant(
                "claude", session_key("claude", f"alice-{theme}", theme), host=f"alice-{theme}"
            )
            store.create_membership(room.id, p.id, "claude-1")
            return token

        token = b.on_loop(plant)
        page = _open(ui, b, room=room_name.lstrip("#"), **options)
        box = page.locator("#input")
        box.fill("/meeting")
        expect(page.locator("#pal-meeting .pal-usage")).to_contain_text("mine")
        box.fill(f"/meeting {LINK} mine")
        box.press("Enter")
        expect(box).to_have_value("")
        expect(page.locator("#log")).to_contain_text("asked their own agent to host the call")
        expect(page.locator("#log")).to_contain_text("--from-room")
        expect(page.locator("#banner-meeting")).to_be_hidden()
        assert LINK not in page.locator("body").inner_text()
        key = "sbc_" + hashlib.sha256(theme.encode()).hexdigest()
        with httpx.Client(base_url=b.base) as host:
            got = host.post(
                "/mcp/meeting/host",
                json={"room": room_name, "key_hash": keys.key_hash(key), "from_room": True},
                headers={"authorization": f"Bearer {token}"},
            )
            assert got.status_code == 200
            assert base64.b64decode(got.json()["link_b64"]).decode() == LINK
            assert (
                host.post(
                    "/mcp/meeting/push", json={"segments": []}, headers={"authorization": f"Bearer {key}"}
                ).status_code
                == 200
            )
        expect(page.locator("#banner-meeting")).to_contain_text("Transcribing a call")
        no_horizontal_scroll(page)
        page.locator("#meeting-leave").click()
        expect(page.locator("#banner-meeting")).to_be_hidden()
        assert LINK not in page.locator("body").inner_text()
        page.close()
