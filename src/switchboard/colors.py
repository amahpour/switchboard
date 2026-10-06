"""Colour for the CLI's own framing (DESIGN.md §40, docs/USAGE.md "Colour").

switchboard colours what it says itself: diff markers, headings, state words,
nicks. It never colours by what relayed text contains, and never passes colour
through from it. Room text, names, remote details and config values are
cleaned first (``envelope.clean`` drops control characters, ESC included;
``install.common.safe_text`` shows them escaped), and a ``Paint`` only ever
wraps its own SGR codes around that cleaned text. So a message can't restyle
the terminal or pass itself off as a switchboard line.

When (``enabled``): ``--color always`` or ``never`` wins over everything.
``auto``, the default, is off when ``NO_COLOR`` is set and not empty
(https://no-color.org), on when ``FORCE_COLOR`` or ``CLICOLOR_FORCE`` is set
(and isn't ``0``, ``false``, ``no`` or ``off``), off for ``TERM=dumb``, and otherwise on only when
the stream is a terminal. So pipes and files stay plain. ``--json`` output is
never painted, whatever the mode.

Palette: six of the basic colours, red to cyan (their bright variants are left to
the terminal's theme) plus bold and dim, and no backgrounds, so it reads on dark
and light terminals alike.
"""

from __future__ import annotations

import os
import sys
import zlib
from collections.abc import Mapping
from typing import Any

MODES = ("auto", "always", "never")

_SGR = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
}
_RESET = "\x1b[0m"
# An agent's nick colour: stable for its name (crc32, since Python's hash() of a str changes
# from process to process). Red is kept for warnings, and the human is bold, not coloured.
_NICK_COLOURS = ("cyan", "green", "yellow", "blue", "magenta")
_OFF_VALUES = ("0", "false", "no", "off")


def enabled(mode: str = "auto", stream: Any = None, environ: Mapping[str, str] | None = None) -> bool:
    """Whether to colour what goes to ``stream`` (default stdout), by the rules above."""
    if mode == "always":
        return True
    if mode == "never":
        return False
    env = os.environ if environ is None else environ
    if env.get("NO_COLOR"):
        return False
    for name in ("FORCE_COLOR", "CLICOLOR_FORCE"):
        value = env.get(name)
        if value and value.strip().lower() not in _OFF_VALUES:
            return True
    if env.get("TERM") == "dumb":
        return False
    stream = sys.stdout if stream is None else stream
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):  # no isatty, or a closed stream
        return False


class Paint:
    """Wraps text in SGR codes when on, and hands it back untouched when off.

    Each line is wrapped on its own, so a colour never runs on into the next
    line's indent (or into ``less -R``'s next screen). The roles below are the
    only styles the CLI uses; name a role rather than a colour at call sites.
    """

    def __init__(self, on: bool) -> None:
        self.on = on

    def style(self, text: str, *names: str) -> str:
        if not self.on or not text or not names:
            return text
        start = "\x1b[" + ";".join(_SGR[n] for n in names) + "m"
        return "\n".join(start + line + _RESET if line else line for line in text.split("\n"))

    # --- roles
    def bold(self, text: str) -> str:
        return self.style(text, "bold")

    def dim(self, text: str) -> str:
        return self.style(text, "dim")

    def ok(self, text: str) -> str:  # up, online, idle, installed, wrote
        return self.style(text, "green")

    def warn(self, text: str) -> str:  # down, parked, paused, held, would change
        return self.style(text, "yellow")

    def bad(self, text: str) -> str:  # blocked, failed, a warning line, approvals off
        return self.style(text, "red")

    def link(self, text: str) -> str:  # URLs
        return self.style(text, "cyan")

    def added(self, text: str) -> str:  # a diff's + lines
        return self.style(text, "green")

    def removed(self, text: str) -> str:  # a diff's - lines
        return self.style(text, "red")

    def heading(self, text: str) -> str:
        return self.style(text, "bold")

    def prompt(self, text: str) -> str:  # Apply? [y/N]
        return self.style(text, "bold", "yellow")

    def nick(self, name: str, kind: str = "agent") -> str:
        """A sender's name: the human bold, the system dim, each agent its own colour."""
        if kind == "human":
            return self.bold(name)
        if kind == "system":
            return self.dim(name)
        return self.style(name, nick_colour(name))


def nick_colour(name: str) -> str:
    """The colour an agent's nick gets, the same in every process and on every run."""
    return _NICK_COLOURS[zlib.crc32(name.encode("utf-8")) % len(_NICK_COLOURS)]


PLAIN = Paint(False)


def for_args(args: Any, stream: Any = None) -> Paint:
    """The Paint for a CLI command: its ``--color`` mode, and never with ``--json``."""
    if getattr(args, "json", False):
        return PLAIN
    return Paint(enabled(getattr(args, "color", "auto"), stream))
