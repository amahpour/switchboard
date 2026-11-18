"""Which plugin version a session runs, and whether it is behind this release's (DESIGN.md §49.13).

Claude Code's and Codex's plugins send their own version in ``X-Switchboard-Plugin-Version``. It is a
claim from outside: cut to ``HEADER_MAX`` characters and parsed strictly (``parse``), and anything that
isn't ``X.Y.Z`` of digits is no version at all. Nothing here decides what a session may do; it only
lets ``/who``, the Inspector and ``join`` say that a plugin is old, with the command that updates it.

``PINNED`` is the manifest version of each plugin at this release (``plugins/<harness>/``). A test
holds it equal to the manifests, and ``release.py`` rewrites it when it raises one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HEADER = b"x-switchboard-plugin-version"
HEADER_MAX = 32  # a longer value is no version: nothing past it is looked at
VERSION_RE = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")

# release.py rewrites the PINNED line (and nothing else in this file) to match the plugin manifests
PINNED = {"claude": "0.16.34", "codex": "0.8.33"}

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


def _key(version: str) -> tuple[int, int, int]:
    a, b, c = version.split(".")
    return int(a), int(b), int(c)


# Claude Code's plugin always reports (its helper prints the version even when it can make no key), so a
# Claude Code session that sent none runs a plugin from before the header. Codex's helper is the person's
# own opt-in (browser sign-in), so a Codex session that sent none is the usual case and not worth a note.
UNKNOWN_IS_STALE = frozenset({"claude"})


@dataclass(frozen=True)
class PluginCheck:
    """``state``: ``current`` (as new as the release's), ``older``, or ``unknown`` (nothing parseable was
    sent). Never ``current`` unless it was parsed and is not behind. ``update`` is the harness's command,
    empty when ``current``."""

    harness: str
    state: str
    version: str | None
    pinned: str
    update: str

    @property
    def nags(self) -> bool:
        """Whether a person or the agent should be told: behind, or (Claude Code only) never said."""
        return self.state == "older" or (self.state == "unknown" and self.harness in UNKNOWN_IS_STALE)

    @property
    def note(self) -> str:
        """One plain sentence for a person or an agent, empty when there is nothing to tell."""
        if not self.nags:
            return ""
        if self.state == "older":
            return f"its plugin is {self.version}, older than {self.pinned}: {self.update}"
        return f"its plugin's version is unknown (older than {self.pinned}, or not the plugin): {self.update}"


def check(harness: str, reported: str | None) -> PluginCheck | None:
    """The check for a session of ``harness`` whose plugin said ``reported`` (a parsed version, or None
    for a call that carried no usable one). None for a harness whose plugin isn't versioned this way."""
    pinned = PINNED.get(harness)
    if pinned is None:
        return None
    if reported is None:
        return PluginCheck(harness, "unknown", None, pinned, UPDATE[harness])
    if _key(reported) < _key(pinned):
        return PluginCheck(harness, "older", reported, pinned, UPDATE[harness])
    return PluginCheck(harness, "current", reported, pinned, "")
