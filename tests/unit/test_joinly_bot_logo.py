"""``deploy/joinly/bot_logo.py`` (#820): the logo swap, the camera click after a join, and the off switch.

The file runs inside joinly, which is not installed here, so it is loaded by path and given fake modules."""

from __future__ import annotations

import asyncio
import importlib.util
import types
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load() -> Any:
    spec = importlib.util.spec_from_file_location("bot_logo", ROOT / "deploy/joinly/bot_logo.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Button:
    def __init__(self, shown: bool, fails: bool = False) -> None:
        self.shown, self.fails, self.clicked = shown, fails, 0
        self.first = self

    async def is_visible(self) -> bool:
        return self.shown

    async def click(self, timeout: int = 0) -> None:
        if self.fails:
            raise RuntimeError("detached")
        self.clicked += 1


class Page:
    def __init__(self, button: Button) -> None:
        self.button, self.names = button, []

    def get_by_role(self, role: str, name: Any) -> Button:
        self.names.append(name)
        return self.button


def loader(mods: dict[str, Any]) -> Any:
    def load_module(name: str) -> Any:
        if name not in mods:
            raise ImportError(name)  # a platform this joinly does not have
        return mods[name]

    return load_module


def fakes() -> tuple[dict[str, Any], Any]:
    class Zoom:
        async def join(self, page: Any, url: str) -> str:
            return "joined"

    mods = {
        "joinly.providers.browser.camera_feed": types.SimpleNamespace(_LOGO_SVG="joinly's"),
        "joinly.providers.browser.platforms.zoom": types.SimpleNamespace(ZoomBrowserPlatformController=Zoom),
    }
    return mods, Zoom


def test_the_logo_is_switchboards_own_mark_and_safe_inside_joinlys_template() -> None:
    """joinly puts the value inside a double-quoted JavaScript string through str.format: no quote, brace or
    raw angle bracket may be in it. Fails if the data URI could break that string."""
    uri = load().LOGO_URI
    assert uri.startswith("data:image/svg+xml,%3Csvg")
    assert not any(c in uri for c in '"{}<> \n')
    assert "0a64d8" in uri  # the favicon's blue tile


def test_install_swaps_the_logo_and_turns_the_camera_on_after_a_join_once() -> None:
    """After install, a join runs joinly's own join first, then clicks the camera button; installing twice
    does not stack a second click. Fails if the join result is lost, the click comes first, or it doubles."""
    bl = load()
    mods, zoom = fakes()
    assert bl.install(loader(mods), env={}) is True
    assert bl.install(loader(mods), env={}) is True
    assert mods["joinly.providers.browser.camera_feed"]._LOGO_SVG == bl.LOGO_URI
    page = Page(Button(True))
    assert asyncio.run(zoom().join(page, "https://zoom.us/j/1")) == "joined"
    assert page.button.clicked == 1
    assert page.names[0].match("Start Video") and page.names[0].match("Turn on camera")
    assert not page.names[0].match("Stop Video")


def test_the_off_switch_touches_nothing() -> None:
    """SWITCHBOARD_BOT_VIDEO=0 (any of the off words) leaves joinly's logo and join alone; any other
    value is on.
    Fails if off still swaps the logo or wraps the join."""
    bl = load()
    for off in ("0", "false", "OFF", " no "):
        mods, zoom = fakes()
        before = zoom.join
        assert bl.install(loader(mods), env={"SWITCHBOARD_BOT_VIDEO": off}) is False
        assert zoom.join is before and mods["joinly.providers.browser.camera_feed"]._LOGO_SVG == "joinly's"
    assert bl.enabled({"SWITCHBOARD_BOT_VIDEO": "maybe"}) is True


@pytest.mark.parametrize("button", [Button(False), Button(True, fails=True)])
def test_a_missing_or_broken_camera_button_never_fails_the_join(button: Button) -> None:
    """No button, or a click that raises, is a grey tile, not a failed join. Fails if the join raises."""
    bl = load()
    mods, zoom = fakes()
    bl.install(loader(mods), env={})
    assert asyncio.run(zoom().join(Page(button), "u")) == "joined"
    assert button.clicked == 0
