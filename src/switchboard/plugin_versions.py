"""Which plugin version a session runs, and whether its major matches this server's (DESIGN.md §49.13).

Claude Code's and Codex's plugins send their own version in ``X-Switchboard-Plugin-Version``. It is a
claim from outside: cut to ``HEADER_MAX`` characters and parsed strictly (``parse``), and anything that
isn't ``X.Y.Z`` of digits is no version at all. Nothing here decides what a session may do; it only
lets ``/who``, the Inspector and ``join`` say that a plugin's major differs, with the command that
updates it when the plugin is the older major.

A plugin's version is the release's: ``release.py`` sets every plugin manifest to the release's own version at
each release (#841), so the plugin of switchboard X.Y.Z is X.Y.Z, and a session's plugin is compared with this
server's own version, ``PINNED`` below. Any plugin works with any server of the same major version (#850).
Nothing in this file is rewritten at a release.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from switchboard import __version__

HEADER = b"x-switchboard-plugin-version"
HEADER_MAX = 32  # a longer value is no version: nothing past it is looked at
VERSION_RE = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")

# the version of each plugin this server expects: its own (derived, so a release cannot leave it behind)
PINNED = {"claude": __version__, "codex": __version__}

# how a person updates each plugin (DESIGN.md §49.5), the same words everywhere
UPDATE = {
    "claude": (
        "claude plugin marketplace update switchboard, then claude plugin update switchboard@switchboard,"
        " then restart Claude Code"
    ),
    "codex": "codex plugin marketplace upgrade, then restart Codex",
}


def parse(raw: object) -> str | None:
    """``X.Y.Z`` for a header value that is exactly that, else None. A claim: anything else (empty,
    a prefix, a suffix, a negative, a huge number, a non-string) is not a version."""
    if not isinstance(raw, str) or len(raw) > HEADER_MAX:
        return None
    got = VERSION_RE.fullmatch(raw.strip())
    return got.group(0) if got is not None else None


def _major(version: str) -> int:
    """The major component, as a number. ``parse`` has already kept it to digits without a leading zero."""
    return int(version.split(".", 1)[0])


# Claude Code's plugin always reports (its helper prints the version even when it can make no key), so a
# Claude Code session that sent none runs a plugin from before the header. Codex's helper is the person's
# own opt-in (browser sign-in), so a Codex session that sent none is the usual case and not worth a note.
UNKNOWN_IS_STALE = frozenset({"claude"})


@dataclass(frozen=True)
class PluginCheck:
    """``state``: ``current`` (the same major as this server), ``major`` (a different major), or
    ``unknown`` (nothing parseable was sent). Never ``current`` unless it was parsed and the major
    matches. ``update`` is the harness's command when the plugin's major is the older one, else empty:
    a newer major is not told to update, because that command installs a plugin and would leave it there."""

    harness: str
    state: str
    version: str | None
    pinned: str
    update: str

    @property
    def nags(self) -> bool:
        """Whether a person or the agent should be told: a different major, or (Claude Code only)
        never said."""
        return self.state == "major" or (self.state == "unknown" and self.harness in UNKNOWN_IS_STALE)

    @property
    def note(self) -> str:
        """One plain sentence for a person or an agent, empty when there is nothing to tell."""
        if not self.nags:
            return ""
        if self.state == "major":
            said = (
                f"its plugin is from release {self.version}, a different major version than {self.pinned},"
                " this server's release"
            )
            return f"{said}: {self.update}" if self.update else said
        return f"its plugin's version is unknown (older than {self.pinned}, or not the plugin): {self.update}"


def check(harness: str, reported: str | None) -> PluginCheck | None:
    """The check for a session of ``harness`` whose plugin said ``reported`` (a parsed version, or None
    for a call that carried no usable one). None for a harness whose plugin isn't versioned this way.
    The same major is current in either direction (#850); only a different major is said."""
    pinned = PINNED.get(harness)
    if pinned is None:
        return None
    if reported is None:
        return PluginCheck(harness, "unknown", None, pinned, UPDATE[harness])
    if _major(reported) != _major(pinned):
        update = UPDATE[harness] if _major(reported) < _major(pinned) else ""
        return PluginCheck(harness, "major", reported, pinned, update)
    return PluginCheck(harness, "current", reported, pinned, "")
