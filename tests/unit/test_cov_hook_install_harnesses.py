"""switchboard install / uninstall for codex, cursor and devin (DESIGN.md §9.7): the
refusals for config shapes install can't merge into safely, the marker checks,
and what uninstall leaves alone. Temp user homes only, never the real ~."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from switchboard.install import codex, cursor, devin
from switchboard.install.common import InstallError, dump_json, hook_command

PY = "/opt/yk/venv/bin/python"
HOME = "/opt/yk/home"


def user_home(tmp_path: Path, files: dict[str, str]) -> Path:
    uh = tmp_path / "uh"
    uh.mkdir()
    for rel, text in files.items():
        (uh / rel).parent.mkdir(parents=True, exist_ok=True)
        (uh / rel).write_text(text)
    return uh


def sb_hook(harness: str, event: str, home: str = HOME) -> str:
    return hook_command(PY, home, "abcdef012345", harness, event)


# ------------------------------------------------------------------- codex
@pytest.mark.parametrize(
    "text,msg",
    [
        (f'model = "o3"  {codex.BEGIN}\n{codex.END}\n', "must begin a line"),
        (f"{codex.END}\n{codex.BEGIN}\n", "broken switchboard markers"),
    ],
)
def test_codex_refuses_misplaced_markers(tmp_path: Path, text: str, msg: str) -> None:
    uh = user_home(tmp_path, {".codex/config.toml": text})
    with pytest.raises(InstallError, match=msg):
        codex.unplan(uh, HOME)


def test_codex_refuses_when_a_key_after_the_block_would_land_in_switchboards_table(tmp_path: Path) -> None:
    """A bare key after the marker block belongs to ``[mcp_servers.switchboard]``
    in TOML: uninstall won't remove the block and so move it to the top level."""
    text = (
        f'{codex.BEGIN}\n[mcp_servers.switchboard]\ncommand = "{PY}"\n'
        f'args = ["-I", "-m", "switchboard", "mcp", "--home", "{HOME}"]\n{codex.END}\nmodel = "o3"\n'
    )
    uh = user_home(tmp_path, {".codex/config.toml": text})
    with pytest.raises(InstallError, match="couldn't remove switchboard's block"):
        codex.unplan(uh, HOME)
    assert (uh / ".codex/config.toml").read_text() == text


def test_codex_uninstall_masks_a_hand_wrapped_args_array(tmp_path: Path) -> None:
    text = (
        'model = "o3"\n\n'
        f"{codex.BEGIN}\n"
        "[mcp_servers.switchboard]\n"
        f'command = "{PY}"\n'
        "args = [\n"
        f'  "-I", "-m", "switchboard", "mcp", "--home", "{HOME}",\n'
        "]\n"
        f"{codex.END}\n"
    )
    uh = user_home(tmp_path, {".codex/config.toml": text})
    plan = codex.unplan(uh, HOME)
    cfg = plan.edits[0]
    assert cfg.after == 'model = "o3"\n'
    assert cfg.display == [
        f"  - {codex.BEGIN}",
        "  - [mcp_servers.switchboard]",
        f'  - command = "{PY}"',
        "  - args = ***",  # a multi-line value: not parsed on its own, so masked
        "  -   ***",
        "  -   ***",
        f"  - {codex.END}",
    ]


# ------------------------------------------------------------------ cursor
def test_cursor_uninstall_leaves_shapes_it_does_not_know(tmp_path: Path) -> None:
    odd = dump_json({"version": 1, "hooks": ["not", "an", "object"]})
    uh = user_home(tmp_path, {".cursor/hooks.json": odd})
    plan = cursor.unplan(uh, HOME)
    assert not plan.edits[1].changed and plan.edits[1].after == odd

    mixed = {
        "version": 1,
        "hooks": {
            "stop": "echo not-a-list",
            "sessionStart": [
                {"command": sb_hook("cursor", "sessionStart"), "timeout": 10},
                {"command": "echo mine"},
            ],
        },
    }
    (uh / ".cursor/hooks.json").write_text(dump_json(mixed))
    plan = cursor.unplan(uh, HOME)
    assert json.loads(plan.edits[1].after) == {
        "version": 1,
        "hooks": {"stop": "echo not-a-list", "sessionStart": [{"command": "echo mine"}]},
    }
    assert [line.split(":")[0] for line in plan.edits[1].display] == ["  - hooks.sessionStart[0]"]


def test_uninstall_notes_a_hook_command_in_a_place_it_does_not_know(tmp_path: Path) -> None:
    """A switchboard hook command outside ``hooks.<event>`` (hand-copied) is left
    in place with a note, not guessed at."""
    data = {"version": 1, "hooks": {}, "statusLine": {"command": sb_hook("cursor", "stop")}}
    uh = user_home(tmp_path, {".cursor/hooks.json": dump_json(data)})
    plan = cursor.unplan(uh, HOME)
    assert not plan.edits[1].changed
    assert (
        "~/.cursor/hooks.json still mentions switchboard's hooks somewhere uninstall doesn't recognise;"
        " remove them by hand" in plan.notes
    )


# ------------------------------------------------------------------- devin
def test_devin_other_mcp_home_reads_only_a_switchboard_entry_for_another_home() -> None:
    def entry(home: str) -> dict:
        return {"command": PY, "args": ["-I", "-m", "switchboard", "mcp", "--home", home]}

    other = json.dumps({"mcpServers": {"switchboard": entry("/opt/other")}})
    assert devin._other_mcp_home(other, HOME) == "/opt/other"
    same = json.dumps({"mcpServers": {"switchboard": entry(HOME)}})
    assert devin._other_mcp_home(same, HOME) is None
    assert devin._other_mcp_home(json.dumps({"mcpServers": {"x": {"command": "node"}}}), HOME) is None
    assert devin._other_mcp_home(json.dumps(["not", "an", "object"]), HOME) is None
    for empty in (None, "", "  \n"):
        assert devin._other_mcp_home(empty, HOME) is None
    assert devin._other_mcp_home("{not json", HOME) is None
