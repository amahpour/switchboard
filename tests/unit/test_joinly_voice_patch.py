"""The joinly voice patch is one pair of files in four places that must not drift (DESIGN.md §52.5, #713).

``deploy/joinly/`` holds them with joinly's licence and a NOTICE. Each plugin ships its own copy (a hosted
relay mounts it from the plugin with no download), and the self-test, DEPLOY.md and the plugins'
``fixes.pins`` name their sha256, because a mount makes them run inside the bot.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FILES = ("server.py", "kokoro.py")
FOLDERS = (
    ROOT / "deploy" / "joinly",
    ROOT / "plugins" / "claude" / "meeting" / "joinly",
    ROOT / "plugins" / "codex" / "meeting" / "joinly",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("name", FILES)
def test_the_plugins_copies_are_the_deploy_files_byte_for_byte(name: str) -> None:
    """A copy that differs is code the bot would run that no one reviewed as the patch. Fails when one of the
    three is edited without the others."""
    assert len({sha(folder / name) for folder in FOLDERS}) == 1


@pytest.mark.parametrize("name", FILES)
def test_each_file_carries_joinlys_licence_and_says_it_was_changed(name: str) -> None:
    """MIT requires the notice with copies. Fails if a file loses its header, or the NOTICE stops naming
    joinly's release and the files."""
    head = (FOLDERS[0] / name).read_text().split("\n\n", 1)[0]
    assert "MIT License" in head and "Dominik Brockmann" in head and "changed by switchboard" in head
    notice = (FOLDERS[0] / "NOTICE").read_text()
    assert "v0.5.4" in notice and name in notice and "MIT" in notice


@pytest.mark.parametrize("name", FILES)
def test_each_file_is_valid_python(name: str) -> None:
    ast.parse((FOLDERS[0] / name).read_text())


def test_the_pins_the_self_test_and_deploy_md_name_are_the_files_hashes() -> None:
    """One hash per file everywhere: ``fixes.pins`` of both plugins (``plugin:joinly/<name>``), the self-test
    that mounts them in the broker's own bot, and DEPLOY.md, where an operator checks them. Fails if the files
    change and one of them is not updated."""
    selftest = (ROOT / "tests" / "live" / "test_meeting_selftest.py").read_text()
    deploy = (ROOT / "docs" / "DEPLOY.md").read_text()
    for name in FILES:
        digest = sha(FOLDERS[0] / name)
        for plugin in ("claude", "codex"):
            pins = (ROOT / "plugins" / plugin / "meeting" / "fixes.pins").read_text().splitlines()
            assert f"{name} {digest} plugin:joinly/{name}" in pins, (plugin, name)
        assert digest in selftest, name
        assert digest in deploy, name


def test_the_downloads_docs_name_are_files_of_this_folder() -> None:
    """DEPLOY.md and the Kubernetes patch download ``deploy/joinly/<name>`` at a release tag. Fails if a file
    is named that this folder does not hold."""
    deploy = (ROOT / "docs" / "DEPLOY.md").read_text()
    for name in FILES:
        assert f"/deploy/joinly/{name}" in deploy
        assert (FOLDERS[0] / name).is_file()


def test_the_captions_file_is_switchboards_own_new_file_pinned_in_the_self_test_and_deploy_md() -> None:
    """``captions.py`` (#764) is not a copy of a joinly file: it is a new one, mounted at
    joinly/captions.py by
    the server's bot only (a hosted call reads no captions), so there is one copy and no ``fixes.pins`` line.
    A mount makes it run inside the bot, so its sha256 is in the self-test and DEPLOY.md. Fails if it changes
    and one of them is not updated, or if it grows a joinly import at its top (the rule and the mapping are
    tested without joinly)."""
    path = FOLDERS[0] / "captions.py"
    ast.parse(path.read_text())
    digest = sha(path)
    assert digest in (ROOT / "tests" / "live" / "test_meeting_selftest.py").read_text()
    assert digest in (ROOT / "docs" / "DEPLOY.md").read_text()
    assert "/deploy/joinly/captions.py" in (ROOT / "docs" / "DEPLOY.md").read_text()
    top_level = [n for n in ast.parse(path.read_text()).body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not any((getattr(n, "module", "") or "").startswith("joinly") for n in top_level)
    for plugin in ("claude", "codex"):
        assert not (ROOT / "plugins" / plugin / "meeting" / "joinly" / "captions.py").exists()


# ------------------------------------------------------------ the bot logo (#820)
START = "# --- switchboard bot logo (#820"
END = "# --- end of the bot logo"


def logo_namespace() -> dict[str, object]:
    """Run only the bot-logo block of server.py (the rest imports joinly, which is not installed here), with a
    logger and ``Any`` it expects from the file's top."""
    import logging
    import re
    from typing import Any

    text = (FOLDERS[0] / "server.py").read_text()
    block = text[text.index(START) : text.index(END)]
    ns: dict[str, object] = {"logger": logging.getLogger("joinly.test"), "Any": Any, "re": re}
    exec(compile(block.replace("\n_install_bot_logo()\n", "\n"), "server.py", "exec"), ns)  # noqa: S102
    return ns


def test_the_bot_logo_is_exactly_the_favicons_mark() -> None:
    """The tile shows the favicon's mark (white on the blue tile, made by docs/media/make_icons.py), and a
    copy in the patched server.py would drift the day the favicon changes. Fails if they differ (comment
    aside)."""
    import re

    favicon = (ROOT / "src" / "switchboard" / "web" / "static" / "favicon.svg").read_text()
    favicon = re.sub(r"<!--.*?-->", "", favicon, flags=re.S).strip()
    assert logo_namespace()["BOT_LOGO_SVG"] == favicon


def test_the_logo_is_safe_in_joinlys_format_string_and_the_off_words_leave_joinly_alone() -> None:
    """joinly puts the data URI into a double-quoted JavaScript string with str.format: no quote, brace, angle
    bracket or space may be in it. SWITCHBOARD_BOT_VIDEO=0 and its words install nothing; any other value is
    on. Fails if the URI could break that string, or off still swaps the logo."""
    import types

    ns = logo_namespace()
    install = ns["_install_bot_logo"]
    mods: dict[str, object] = {
        "joinly.providers.browser.camera_feed": types.SimpleNamespace(_LOGO_SVG="joinly's"),
    }

    def load(name: str) -> object:
        if name not in mods:
            raise ImportError(name)
        return mods[name]

    for off in ("0", "false", "OFF", " no "):
        assert install(load, {"SWITCHBOARD_BOT_VIDEO": off}) is False  # type: ignore[operator]
        feed = mods["joinly.providers.browser.camera_feed"]
        assert feed._LOGO_SVG == "joinly's"  # type: ignore[attr-defined]
    assert ns["_bot_video_enabled"]({"SWITCHBOARD_BOT_VIDEO": "maybe"}) is True  # type: ignore[operator]
    assert install(load, {}) is True  # type: ignore[operator]
    uri = mods["joinly.providers.browser.camera_feed"]._LOGO_SVG  # type: ignore[attr-defined]
    assert uri.startswith("data:image/svg+xml,%3Csvg") and not any(c in uri for c in '"{}<> \n')


class _Page:
    """A page whose one ``evaluate`` is the DOM click: it records what it was given and answers true (a button
    was clicked), false (none matched) or raises."""

    def __init__(self, found: bool = True, fails: bool = False) -> None:
        self.found, self.fails, self.calls = found, fails, []

    async def evaluate(self, js: str, arg: str) -> bool:
        self.calls.append((js, arg))
        if self.fails:
            raise RuntimeError("page closed")
        return self.found


def _zoom_world(ns: dict[str, object]) -> tuple[object, type, object]:
    import types

    class Zoom:
        async def join(self, page: object, url: str) -> str:
            return "joined"

    mods = {
        "joinly.providers.browser.camera_feed": types.SimpleNamespace(_LOGO_SVG="joinly's"),
        "joinly.providers.browser.platforms.zoom": types.SimpleNamespace(ZoomBrowserPlatformController=Zoom),
    }

    def load(name: str) -> object:
        if name not in mods:
            raise ImportError(name)  # a platform this joinly does not have
        return mods[name]

    return ns["_install_bot_logo"], Zoom, load


def test_a_join_then_clicks_the_camera_once_in_the_page_and_a_second_install_does_not_double_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The camera click comes after joinly's own join, keeps its result, installing twice wraps once, and it
    is ONE page.evaluate that clicks the first button whose label matches, visible or not (Zoom hides its bar
    after a join, so is_visible() was False in the live check). The pattern handed to the page names the
    start-video buttons and not the stop ones, and the outcome is logged once at info. Fails if the join
    result is lost, the click comes first or twice, or the old visibility check is back."""
    import asyncio
    import logging
    import re

    install, zoom, load = _zoom_world(logo_namespace())
    install(load, {})  # type: ignore[operator]
    install(load, {})  # type: ignore[operator]
    page = _Page(found=True)
    with caplog.at_level(logging.INFO, logger="joinly.test"):
        assert asyncio.run(zoom().join(page, "https://zoom.us/j/1")) == "joined"
        assert asyncio.run(zoom().join(_Page(found=True), "https://zoom.us/j/2")) == "joined"
    assert len(page.calls) == 1
    js, pattern = page.calls[0]
    assert "querySelectorAll('button')" in js and "aria-label" in js and ".click()" in js
    assert "is_visible" not in js
    rx = re.compile(pattern, re.IGNORECASE)
    assert rx.search("start my video") and rx.search("Start Video") and rx.search("Turn on camera")
    assert not rx.search("Stop Video") and not rx.search("turn off camera")
    assert "(?" not in pattern  # a Python-only group would not parse in the page's RegExp
    assert [r.getMessage() for r in caplog.records].count("bot logo: camera button clicked") == 1


def test_no_matching_button_or_an_exception_never_fails_the_join(caplog: pytest.LogCaptureFixture) -> None:
    """No button matched ("not found" at info) or the page raising (a warning once per process) is a grey
    tile, not a failed join. Fails if the join raises, or the exception is logged on every call."""
    import asyncio
    import logging

    ns = logo_namespace()
    install, zoom, load = _zoom_world(ns)
    install(load, {})  # type: ignore[operator]
    with caplog.at_level(logging.INFO, logger="joinly.test"):
        assert asyncio.run(zoom().join(_Page(found=False), "u")) == "joined"
        for _ in range(2):
            assert asyncio.run(zoom().join(_Page(fails=True), "u")) == "joined"
    messages = [r.getMessage() for r in caplog.records]
    assert "bot logo: camera button not found" in messages
    assert messages.count("bot logo: could not turn the camera on") == 1
