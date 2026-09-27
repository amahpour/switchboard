"""``switchboard install codex`` (DESIGN.md §9.7).

- MCP server: a ``[mcp_servers.switchboard]`` table in ``~/.codex/config.toml``,
  between ``# >>> switchboard >>>`` and ``# <<< switchboard <<<``. Only that span is
  ever replaced; everything else in the file is kept byte for byte. If a
  ``mcp_servers.switchboard`` entry exists outside the markers, install refuses.
  The result must parse as TOML with exactly switchboard's entry, or nothing is
  written.
- Hooks: ``~/.codex/hooks.json``, one group per event for UserPromptSubmit,
  PostToolUse, Stop (timeout 10), Interrupt and SessionEnd (3, the maximum).
  New groups are **appended** to each event's array: inserting would shift
  the indices that Codex's hook trust is keyed by and un-trust the groups
  after it (FINDINGS §4b). An older switchboard hook for this home is replaced in
  place (same index).
- Codex asks you to review new hooks: start codex, run /hooks, and trust the
  switchboard ones. switchboard never trusts anything itself; it writes no trust
  state, no approval or sandbox setting and no allowlist.

``switchboard uninstall codex`` (``unplan``) removes switchboard's lines from the
marker block (``[mcp_servers.switchboard]`` and its sub-tables, the markers and
their comments; anything else that ended up between the markers, such as a
table Codex appended, stays where it is) and checks that the result parses
to the same TOML minus ``mcp_servers.switchboard``. It removes switchboard's hook
groups from ``~/.codex/hooks.json``; the user's groups after them move up,
and since Codex keys hook trust by position (event:group:handler) the diff
flags each one that moves: those may need re-trusting in ``/hooks``.
switchboard never writes trust state, and leaves Codex's own trust records alone.
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
    compact,
    deep,
    dump_json,
    hook_command,
    is_switchboard_hook,
    json_removal,
    load_json_obj,
    mask_args,
    mcp_argv,
    mcp_home,
    other_home_hint,
    read_text,
    remove_hook_groups,
    safe_text,
    tilde,
)

BEGIN = "# >>> switchboard >>>"
END = "# <<< switchboard <<<"
# (event, timeout): Interrupt and SessionEnd allow at most 3 s (found in the M0 Codex hooks experiments)
EVENTS: tuple[tuple[str, int], ...] = (
    ("UserPromptSubmit", 10),
    ("PostToolUse", 10),
    ("Stop", 10),
    ("Interrupt", 3),
    ("SessionEnd", 3),
)


def mcp_entry(python: str, home: str) -> dict[str, Any]:
    argv = mcp_argv(python, home)
    return {"command": argv[0], "args": argv[1:]}


def _toml_str(s: str) -> str:
    # JSON string escapes are valid TOML basic-string escapes (paths were
    # already checked for quotes, backslashes, $ and newlines)
    return json.dumps(s, ensure_ascii=False)


def toml_block(python: str, home: str) -> str:
    e = mcp_entry(python, home)
    args = ", ".join(_toml_str(a) for a in e["args"])
    return (
        f"{BEGIN}\n"
        "# Added by `switchboard install codex`; this span is replaced on re-install.\n"
        "[mcp_servers.switchboard]\n"
        f"command = {_toml_str(e['command'])}\n"
        f"args = [{args}]\n"
        f"{END}\n"
    )


def inline_entry(python: str, home: str) -> str:
    """``mcp_servers.switchboard=<inline table>`` for a per-launch ``-c`` flag."""
    e = mcp_entry(python, home)
    args = ",".join(_toml_str(a) for a in e["args"])
    return f"mcp_servers.switchboard={{command={_toml_str(e['command'])},args=[{args}]}}"


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


def config_after(before: str | None, python: str, home: str) -> str:
    """The new config.toml text: only the marker span is written."""
    block = toml_block(python, home)
    text = before or ""
    span = _span(text)
    # lines between the markers that aren't switchboard's (a table Codex appended) are kept
    theirs = "" if span is None else "".join(_split_span(text[span[0]: span[1]])[1])
    outside = text if span is None else text[: span[0]] + theirs + text[span[1]:]
    data = _parse(outside, "~/.codex/config.toml")
    servers = data.get("mcp_servers")
    if isinstance(servers, dict) and "switchboard" in servers:
        raise InstallError("~/.codex/config.toml already defines mcp_servers.switchboard outside switchboard's"
                           " markers; remove it (or wrap it in the markers) and re-run")
    if span is not None:
        after = text[: span[0]] + block + theirs + text[span[1]:]
    elif not text:
        after = block
    else:
        after = text + ("" if text.endswith("\n") else "\n") + "\n" + block
    got = _parse(after, "the updated config.toml")
    if (got.get("mcp_servers") or {}).get("switchboard") != mcp_entry(python, home):
        raise InstallError("couldn't add [mcp_servers.switchboard] to ~/.codex/config.toml safely"
                           " (an inline mcp_servers table?); add it by hand")
    return after


def _install_display(before: str | None, after: str, block: str) -> list[str]:
    """Install's diff for config.toml: switchboard's block as added, unless only
    other lines between the markers move out (then a single ``~`` line)."""
    if after == before:
        return []
    span = _span(before) if before else None
    ours, theirs = _split_span(before[span[0]: span[1]]) if before and span else ([], [])
    shown = [] if theirs and "".join(ours) == block else [f"  + {line}" for line in block.splitlines()]
    if theirs:
        shown.append(f"  ~ {len(theirs)} line(s) between the markers that aren't switchboard's move after {END!r}")
    return shown


def config_without(before: str, home: str) -> tuple[str, list[str], list[str]]:
    """(new text, removed lines for the diff, notes): switchboard's lines of the
    marker block removed, everything else kept byte for byte."""
    data = _parse(before, "~/.codex/config.toml")
    entry = (data.get("mcp_servers") or {}).get("switchboard") if isinstance(data.get("mcp_servers"), dict) else None
    span = _span(before)
    if span is None:
        if entry is not None:
            return before, [], ["~/.codex/config.toml has an [mcp_servers.switchboard] table outside switchboard's"
                                " markers; left alone (install never writes one there)"]
        return before, [], []
    other = mcp_home(entry)
    if other is not None and other != home:
        return before, [], [f"~/.codex/config.toml: switchboard's block there is for another switchboard home"
                            f" ({safe_text(other)}); left alone ({other_home_hint('codex', other)})"]
    ours, theirs = _split_span(before[span[0]: span[1]])
    prefix, suffix = before[: span[0]], before[span[1]:]
    if not theirs and prefix.endswith("\n\n") and (not suffix or suffix.startswith("\n")):
        prefix = prefix[:-1]  # the blank line install put before the block
    after = prefix + "".join(theirs) + suffix
    got = _parse(after, "config.toml without switchboard's block")
    still = isinstance(got.get("mcp_servers"), dict) and "switchboard" in got["mcp_servers"]
    if still or _without_switchboard(got) != _without_switchboard(data):
        raise InstallError("couldn't remove switchboard's block from ~/.codex/config.toml safely; delete the"
                           f" lines from {BEGIN!r} to {END!r} by hand")
    return after, _show_toml(ours), []


def hook_events(python: str, home: str, sha12: str) -> dict[str, tuple[str, int]]:
    return {e: (hook_command(python, home, sha12, "codex", e), t) for e, t in EVENTS}


def set_codex_hooks(data: dict[str, Any], events: dict[str, tuple[str, int]], home: str) -> list[str]:
    """Codex-shaped hooks ``hooks.<Event> = [{"hooks":[{type, command, timeout}]}]``.

    Appended, never inserted (trust is keyed by index). An older switchboard hook
    for this home is replaced in place, keeping its index."""
    lines: list[str] = []
    hooks = data.get("hooks")
    if hooks is None:
        hooks = data["hooks"] = {}
    if not isinstance(hooks, dict):
        raise InstallError('"hooks" in ~/.codex/hooks.json is not an object; fix it by hand first')
    for event, (cmd, timeout) in events.items():
        arr = hooks.get(event)
        if arr is None:
            arr = hooks[event] = []
        if not isinstance(arr, list):
            raise InstallError(f'"hooks.{event}" is not a list; fix it by hand first')
        want = {"type": "command", "command": cmd, "timeout": timeout}
        found = False
        for gi, g in enumerate(arr):
            if not (isinstance(g, dict) and isinstance(g.get("hooks"), list)):
                continue
            for hi, h in enumerate(g["hooks"]):
                if isinstance(h, dict) and is_switchboard_hook(h.get("command"), home):
                    found = True
                    if h != want:
                        g["hooks"][hi] = dict(want)
                        lines.append(f"  ~ hooks.{event}[{gi}].hooks[{hi}]: {compact(want)}")
        if not found:
            group = {"hooks": [want]}
            arr.append(group)
            lines.append(f"  + hooks.{event}[{len(arr) - 1}]: {compact(group)}")
    return lines


def plan(user_home: Path, python: str, home: str, sha12: str, *, run_commands: bool = True) -> Plan:
    p = Plan("codex")
    cfg = user_home / ".codex" / "config.toml"
    before = read_text(cfg)
    after = config_after(before, python, home)
    shown = _install_display(before, after, toml_block(python, home))
    p.edits.append(FileEdit(path=cfg, before=before, after=after, display=shown, label=tilde(cfg, user_home)))
    hj = user_home / ".codex" / "hooks.json"
    hbefore, data = load_json_obj(hj)
    new = deep(data)
    lines = set_codex_hooks(new, hook_events(python, home, sha12), home)
    hafter = dump_json(new) if lines else (hbefore or "")
    p.edits.append(FileEdit(path=hj, before=hbefore, after=hafter, display=lines, label=tilde(hj, user_home)))
    p.notes.append("start codex, run /hooks, then review and trust the switchboard hooks: Codex doesn't run a"
                   " hook until you trust it, and switchboard never trusts anything itself")
    p.notes.append("hooks and the MCP server take effect in new Codex sessions; they are inert until a"
                   " session joins a room")
    p.notes.append("idle wakes need the TUI attached to the Codex app-server daemon (daemon_auto_start, or"
                   " `codex app-server daemon start`); otherwise switchboard uses `codex queue` (up to ~10 s)")
    return p


def unplan(user_home: Path, home: str, *, run_commands: bool = True) -> Plan:
    """``switchboard uninstall codex``: the inverse of ``plan``."""
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
        p.notes.append(f"your own Codex hooks marked ! move up ({moved}): Codex keys hook trust by position, so"
                       " start codex and run /hooks; it may ask you to review and trust them again (switchboard never"
                       " writes trust state)")
    if any(e.changed for e in p.edits):
        p.notes.append("Codex's own trust records for the removed hooks stay in ~/.codex/config.toml; switchboard"
                       " never edits trust state")
    return p


def print_args(python: str, home: str, sha12: str, workspace: Path | None = None) -> dict[str, Any]:
    """Only switchboard's MCP entry and hooks, for one launch; nothing is written.

    ``files`` go into a CODEX_HOME (``config.toml``, ``hooks.json``) or a
    project (``.codex/hooks.json``, which Codex loads only in a trusted
    project); ``argv`` is the same MCP entry as a ``-c`` flag. Hook trust is
    the launcher's business: switchboard never emits trust state."""
    hooks: dict[str, Any] = {}
    set_codex_hooks(hooks, hook_events(python, home, sha12), home)
    return {
        "argv": ["-c", inline_entry(python, home)],
        "env": {},
        "files": {
            "config.toml": toml_block(python, home),
            "hooks.json": dump_json(hooks),
        },
        "notes": ["a -c flag keeps a plain `codex` TUI off the shared daemon; pass it to"
                  " `codex app-server` (or use the files) instead; nothing was written"],
    }
