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


class _Button:
    def __init__(self, shown: bool, fails: bool = False) -> None:
        self.shown, self.fails, self.clicked, self.first = shown, fails, 0, self

    async def is_visible(self) -> bool:
        return self.shown

    async def click(self, timeout: int = 0) -> None:
        if self.fails:
            raise RuntimeError("detached")
        self.clicked += 1


class _Page:
    def __init__(self, button: _Button) -> None:
        self.button, self.names = button, []

    def get_by_role(self, role: str, name: object) -> _Button:
        self.names.append(name)
        return self.button


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


def test_a_join_then_clicks_the_camera_once_and_a_second_install_does_not_double_it() -> None:
    """The camera click comes after joinly's own join, keeps its result, and installing twice wraps once. The
    button regex takes the start-video names and not the stop ones. Fails if the join result is lost, the
    click comes first, or it fires twice."""
    import asyncio

    install, zoom, load = _zoom_world(logo_namespace())
    install(load, {})  # type: ignore[operator]
    install(load, {})  # type: ignore[operator]
    page = _Page(_Button(True))
    assert asyncio.run(zoom().join(page, "https://zoom.us/j/1")) == "joined"
    assert page.button.clicked == 1
    assert page.names[0].match("Start Video") and page.names[0].match("Turn on camera")
    assert not page.names[0].match("Stop Video")


@pytest.mark.parametrize("button", [_Button(False), _Button(True, fails=True)])
def test_a_missing_or_broken_camera_button_never_fails_the_join(button: _Button) -> None:
    """No button, or a click that raises, is a grey tile, not a failed join, and the failure is logged once
    per process. Fails if the join raises."""
    import asyncio

    ns = logo_namespace()
    install, zoom, load = _zoom_world(ns)
    install(load, {})  # type: ignore[operator]
    assert asyncio.run(zoom().join(_Page(button), "u")) == "joined"
    assert button.clicked == 0
    assert asyncio.run(zoom().join(_Page(button), "u")) == "joined"
