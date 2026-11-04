"""Which meeting links the broker will open (DESIGN.md §52).

joinly's browser would open any URL, so only https links on Zoom, Google Meet and
Teams are accepted. The full link, query string included, stays in memory only
long enough to join: nothing here is written down.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

PLATFORM_LABEL = {"zoom": "Zoom", "meet": "Meet", "teams": "Teams"}


class LinkError(ValueError):
    """The text is not an allowed meeting link. The message never echoes the text."""


@dataclass(frozen=True)
class MeetingLink:
    platform: str  # zoom, meet, teams
    url: str  # the full https link, for join_meeting only


def _host_is(host: str, root: str) -> bool:
    return host == root or host.endswith("." + root)


def parse_link(text: str) -> MeetingLink:
    """``(platform, url)`` or ``LinkError``. ``url`` is the caller's text, unchanged,
    so Zoom's ``pwd=`` still reaches joinly. A non-https link, any other host, or a
    host with no meeting path is refused."""
    raw = text.strip()
    if not raw or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in raw) or "\\" in raw:
        raise LinkError("not a meeting link")
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme != "https" or parts.username or parts.password:
        raise LinkError("not a meeting link")
    host = parts.hostname
    if not host:
        raise LinkError("not a meeting link")
    try:
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        raise LinkError("not a meeting link") from None
    if _host_is(host, "zoom.us"):
        platform = "zoom"
    elif host == "meet.google.com":
        platform = "meet"
    elif _host_is(host, "teams.microsoft.com") or _host_is(host, "teams.live.com"):
        platform = "teams"
    else:
        raise LinkError("not a meeting link")
    if parts.path in ("", "/"):
        raise LinkError("not a meeting link")
    return MeetingLink(platform, raw)
