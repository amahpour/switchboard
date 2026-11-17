# switchboard's logo on the meeting bot's tile (#820, DESIGN.md section 52).
#
# switchboard's own file, MIT licensed like the rest of this repository: a new file mounted into joinly v0.5.4 as
# joinly/bot_logo.py. It is NOT wired in yet: the patched server.py would call ``install()`` once at start.
#
# joinly already has a virtual camera (joinly/providers/browser/camera_feed.py): a canvas that draws a logo, in the
# place of a real camera, for any getUserMedia call of the meeting page. Nothing ever turns the camera on, so the
# tile stays grey. This file does the two small things that are missing, without copying any joinly file:
#   1. it replaces the logo that canvas draws with switchboard's (``camera_feed._LOGO_SVG`` is read when the
#      page's init script is made, so assigning it before a call starts is enough);
#   2. after a platform's ``join`` it clicks that platform's camera button, once, and never fails the join for it.
# ``SWITCHBOARD_BOT_VIDEO=0`` (or false, off, no) turns both off: the bot is a grey tile as before.
from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import quote

logger = logging.getLogger("joinly.bot_logo")

# favicon.svg of this repository (src/switchboard/web/static/), without its comment: a white mark on the blue tile
LOGO = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7.5" '
    'fill="#0a64d8"/><g transform="translate(4 4) scale(1.5)" fill="none" stroke="#fff" stroke-width="1.6" '
    'stroke-linecap="round" stroke-linejoin="round"><circle cx="4" cy="4" r="1.8"/><circle cx="12" cy="4" r="1.8"/>'
    '<circle cx="12" cy="12" r="1.8"/><path d="M4 5.8V8a4 4 0 0 0 4 4h2.2M12 5.8v4.4"/></g></svg>'
)
LOGO_URI = "data:image/svg+xml," + quote(LOGO, safe="/:=,;")

OFF = frozenset({"0", "false", "off", "no"})
# what each platform's camera button is called while the camera is off (accessible names, case-insensitive)
CAMERA_ON = re.compile(r"^(start (my )?video|turn on (my )?camera|turn camera on)", re.IGNORECASE)
PLATFORMS = (
    ("joinly.providers.browser.platforms.zoom", "ZoomBrowserPlatformController"),
    ("joinly.providers.browser.platforms.google_meet", "GoogleMeetBrowserPlatformController"),
    ("joinly.providers.browser.platforms.teams", "TeamsBrowserPlatformController"),
)


def enabled(env: dict[str, str] | None = None) -> bool:
    """On unless ``SWITCHBOARD_BOT_VIDEO`` says off. An unrecognised value is on: the setting only turns a
    thing off, and the default is the logo."""
    return (env if env is not None else os.environ).get("SWITCHBOARD_BOT_VIDEO", "1").strip().lower() not in OFF


async def camera_on(page: Any) -> bool:
    """Click the page's camera-on button once if it is there; True when it was clicked. Never raises."""
    try:
        button = page.get_by_role("button", name=CAMERA_ON).first
        if not await button.is_visible():
            return False
        await button.click(timeout=1000)
        return True
    except Exception:  # a missing button or a changed page must never fail the join
        logger.warning("bot logo: could not turn the camera on", exc_info=True)
        return False


def _after_join(original: Any) -> Any:
    async def join(self: Any, page: Any, *args: Any, **kwargs: Any) -> Any:
        result = await original(self, page, *args, **kwargs)
        await camera_on(page)
        return result

    join.__wrapped__ = original  # type: ignore[attr-defined]
    return join


def install(import_module: Any = None, env: dict[str, str] | None = None) -> bool:
    """Swap the logo and wrap each platform's join, once. False (and nothing touched) when it is off."""
    if not enabled(env):
        return False
    import importlib

    load = import_module or importlib.import_module
    load("joinly.providers.browser.camera_feed")._LOGO_SVG = LOGO_URI
    for module, name in PLATFORMS:
        try:
            cls = getattr(load(module), name)
        except (ImportError, AttributeError):
            logger.warning("bot logo: %s.%s not found", module, name)
            continue
        if not hasattr(cls.join, "__wrapped__"):
            cls.join = _after_join(cls.join)
    return True
