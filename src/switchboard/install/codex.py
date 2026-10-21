"""``switchboard uninstall codex`` (DESIGN.md §9.7).

``unplan`` removes what ``switchboard install codex`` used to write (retired in 0.38.0, #385): the
``[mcp_servers.switchboard]`` table in ``~/.codex/config.toml``, between ``# >>> switchboard >>>`` and
``# <<< switchboard <<<``, and switchboard's hook groups from ``~/.codex/hooks.json``.

It removes switchboard's lines from the marker block (``[mcp_servers.switchboard]`` and its sub-tables,
the markers and their comments; anything else that ended up between the markers, such as a table Codex
appended, stays where it is) and checks that the result parses to the same TOML minus
``mcp_servers.switchboard``; everything else in the file is kept byte for byte. The user's hook groups
after switchboard's move up, and since Codex keys hook trust by position (event:group:handler) the diff
flags each one that moves: those may need re-trusting in ``/hooks``. switchboard never writes trust
state, and leaves Codex's own trust records alone.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from switchboard.install.common import (
    MASK_RE,
    FileEdit,
    InstallError,
    Missing,
    Plan,
    json_removal,
    mask_args,
    mcp_home,
    other_home_hint,
    read_text,
    remove_hook_groups,
    safe_text,
    tilde,
)

BEGIN = "# >>> switchboard >>>"
END = "# <<< switchboard <<<"


def _toml_str(s: str) -> str:
    # JSON string escapes are valid TOML basic-string escapes (paths were
    # already checked for quotes, backslashes, $ and newlines)
    return json.dumps(s, ensure_ascii=False)


def _span(text: str) -> tuple[int, int] | None:
    """(start, end) offsets of the marker block, end after END's newline; None if absent."""
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise InstallError("config.toml has broken switchboard markers; fix them by hand first")
    if BEGIN not in text:
        return None
    a = text.index(BEGIN)
    if a != 0 and text[a - 1] != "\n":
        raise InstallError("the switchboard start marker must begin a line; fix config.toml by hand first")
    b = text.index(END)
    if b < a:
        raise InstallError("config.toml has broken switchboard markers; fix them by hand first")
    b += len(END)
    if b < len(text) and text[b] == "\n":
        b += 1
    return a, b


# ``[mcp_servers.switchboard]`` or one of its sub-tables (``[mcp_servers.switchboard.env]``)
_OUR_HEADER = re.compile(r"""^\[\s*mcp_servers\s*\.\s*(switchboard|"switchboard"|'switchboard')\s*[.\]]""")


def _split_span(span: str) -> tuple[list[str], list[str]]:
    """(switchboard's lines, other lines) of the marker span, in order.

    switchboard's: the markers, comments and blank lines before the first table
    header, and ``[mcp_servers.switchboard]`` (with sub-tables) up to the next
    other header. Anything else, such as a table Codex appended after
    switchboard's, is someone else's and is kept."""
    ours: list[str] = []
    theirs: list[str] = []
    section = "lead"  # before any header: comments and blanks are ours, keys aren't
    for line in span.splitlines(keepends=True):
        st = line.strip()
        if st in (BEGIN, END):
            ours.append(line)
            continue
        if st.startswith("["):
            section = "ours" if _OUR_HEADER.match(st) and not st.startswith("[[") else "theirs"
        if section == "ours" or (section == "lead" and (not st or st.startswith("#"))):
            ours.append(line)
        else:
            theirs.append(line)
    return ours, theirs


_TOML_KEY = re.compile(r"""^\s*("[^"]*"|'[^']*'|[A-Za-z0-9_.\-]+)\s*=""")
_MAIN_HEADER = re.compile(r"""^\[\s*mcp_servers\s*\.\s*(switchboard|"switchboard"|'switchboard')\s*\]$""")


def _show_toml(lines: list[str]) -> list[str]:
    """Removed TOML lines for the diff, through an allowlist: markers, table
    headers and blank lines as they are; in ``[mcp_servers.switchboard]`` a
    one-line ``command`` string and ``args`` array (see ``mask_args``); every
    other value, continuation line or secret-looking comment ``***``."""
    shown: list[str] = []
    main = False
    for line in lines:
        text = line.rstrip("\r\n")
        st = text.strip()
        if st.startswith("["):
            main = bool(_MAIN_HEADER.match(st))
            shown.append(f"  - {text}")
            continue
        if not st or st in (BEGIN, END) or (st.startswith("#") and not MASK_RE.search(st)):
            shown.append(f"  - {text}")
            continue
        if st.startswith("#"):
            shown.append("  - # ***")
            continue
        m = _TOML_KEY.match(text)
        if m is None:
            shown.append("  -   ***")  # a continuation line of a multi-line value
            continue
        key = m.group(1)
        if main and key in ("command", "args"):
            try:
                val = tomllib.loads(text)[key]
            except (tomllib.TOMLDecodeError, KeyError):
                val = None  # a multi-line value: masked below
            if key == "command" and isinstance(val, str):
                shown.append(f"  - command = {safe_text(_toml_str(val))}")
                continue
            if key == "args" and isinstance(val, list) and all(isinstance(a, str) for a in val):
                args = ", ".join(safe_text(_toml_str(a)) for a in mask_args(val))
                shown.append(f"  - args = [{args}]")
                continue
        shown.append(f"  - {text[: m.end()]} ***")
    return shown


def _without_switchboard(data: dict[str, Any]) -> dict[str, Any]:
    servers = data.get("mcp_servers")
    if isinstance(servers, dict):
        servers.pop("switchboard", None)
        if not servers:
            del data["mcp_servers"]
    return data


def _parse(text: str, what: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise InstallError(f"{what} is not valid TOML ({e}); fix it by hand first") from None


def config_without(before: str, home: str) -> tuple[str, list[str], list[str]]:
    """(new text, removed lines for the diff, notes): switchboard's lines of the
    marker block removed, everything else kept byte for byte."""
    data = _parse(before, "~/.codex/config.toml")
    entry = (
        (data.get("mcp_servers") or {}).get("switchboard")
        if isinstance(data.get("mcp_servers"), dict)
        else None
    )
    span = _span(before)
    if span is None:
        if entry is not None:
            return (
                before,
                [],
                [
                    "~/.codex/config.toml has an [mcp_servers.switchboard] table outside switchboard's"
                    " markers; left alone (install never wrote one there)"
                ],
            )
        return before, [], []
    other = mcp_home(entry)
    if other is not None and other != home:
        return (
            before,
            [],
            [
                f"~/.codex/config.toml: switchboard's block there is for another switchboard home"
                f" ({safe_text(other)}); left alone ({other_home_hint('codex', other)})"
            ],
        )
    ours, theirs = _split_span(before[span[0] : span[1]])
    prefix, suffix = before[: span[0]], before[span[1] :]
    if not theirs and prefix.endswith("\n\n") and (not suffix or suffix.startswith("\n")):
        prefix = prefix[:-1]  # the blank line install put before the block
    after = prefix + "".join(theirs) + suffix
    got = _parse(after, "config.toml without switchboard's block")
    still = isinstance(got.get("mcp_servers"), dict) and "switchboard" in got["mcp_servers"]
    if still or _without_switchboard(got) != _without_switchboard(data):
        raise InstallError(
            "couldn't remove switchboard's block from ~/.codex/config.toml safely; delete the"
            f" lines from {BEGIN!r} to {END!r} by hand"
        )
    return after, _show_toml(ours), []


def unplan(user_home: Path, home: str, *, run_commands: bool = True) -> Plan:
    """``switchboard uninstall codex``: what ``switchboard install codex`` used to write, removed."""
    p = Plan("codex", verb="uninstall")
    cfg = user_home / ".codex" / "config.toml"
    label = tilde(cfg, user_home)
    before = read_text(cfg)
    if before is None:
        p.edits.append(Missing(label))
    else:
        after, shown, notes = config_without(before, home)
        p.edits.append(FileEdit(path=cfg, before=before, after=after, display=shown, label=label))
        p.notes += notes
    moved = 0

    def remove(data: dict[str, Any]) -> list[str]:
        nonlocal moved
        lines, moved = remove_hook_groups(data, home, drop_empty_hooks=False, positional=True)
        return lines

    json_removal(p, user_home / ".codex" / "hooks.json", user_home, home, remove)
    if moved:
        p.notes.append(
            f"your own Codex hooks marked ! move up ({moved}): Codex keys hook trust by position, so"
            " start codex and run /hooks; it may ask you to review and trust them again (switchboard never"
            " writes trust state)"
        )
    if any(e.changed for e in p.edits):
        p.notes.append(
            "Codex's own trust records for the removed hooks stay in ~/.codex/config.toml; switchboard"
            " never edits trust state"
        )
    return p
