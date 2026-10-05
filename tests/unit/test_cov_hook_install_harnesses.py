"""switchboard install / uninstall for codex, cursor and devin (DESIGN.md §9.7): the
refusals for config shapes install can't merge into safely, the marker checks,
and what uninstall leaves alone. Temp user homes only, never the real ~."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from switchboard.config import Config
from switchboard.install import codex, cursor, devin
from switchboard.install.common import InstallError, dump_json, hook_command
from switchboard.paths import hook_sha12

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
    return hook_command(PY, home, hook_sha12(), harness, event)


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
        codex.plan(uh, PY, HOME, hook_sha12())
    with pytest.raises(InstallError, match=msg):
        codex.unplan(uh, HOME)


def test_codex_refuses_when_a_key_after_the_block_would_land_in_switchboards_table(tmp_path: Path) -> None:
    """A bare key after the marker block belongs to ``[mcp_servers.switchboard]``
    in TOML: install won't write a block that picks it up, and uninstall won't
    remove the block and so move it to the top level."""
    text = codex.toml_block(PY, HOME) + 'model = "o3"\n'
    uh = user_home(tmp_path, {".codex/config.toml": text})
    with pytest.raises(InstallError, match="couldn't add"):
        codex.plan(uh, PY, HOME, hook_sha12())
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


@pytest.mark.parametrize(
    "hooks,msg",
    [
        ({"hooks": []}, '"hooks" in ~/.codex/hooks.json is not an object'),
        ({"hooks": {"Stop": {"hooks": []}}}, '"hooks.Stop" is not a list'),
    ],
)
def test_codex_refuses_hooks_it_cannot_merge_into(tmp_path: Path, hooks: dict, msg: str) -> None:
    uh = user_home(tmp_path, {".codex/hooks.json": json.dumps(hooks)})
    with pytest.raises(InstallError, match=msg):
        codex.plan(uh, PY, HOME, hook_sha12())


def test_codex_skips_odd_groups_and_appends_after_them(tmp_path: Path) -> None:
    mine = {"hooks": [{"type": "command", "command": "echo mine"}]}
    stop = ["legacy-string", {"hooks": "not-a-list"}, mine]
    uh = user_home(tmp_path, {".codex/hooks.json": json.dumps({"hooks": {"Stop": stop}})})
    plan = codex.plan(uh, PY, HOME, hook_sha12())
    after = json.loads(plan.edits[1].after)["hooks"]["Stop"]
    assert after[:3] == stop  # untouched, same indices (Codex keys trust by position)
    assert after[3] == {"hooks": [{"type": "command", "command": sb_hook("codex", "Stop"), "timeout": 10}]}
    assert any(line.startswith("  + hooks.Stop[3]: ") for line in plan.edits[1].display)


# ------------------------------------------------------------------ cursor
def test_cursor_refuses_a_home_config_it_cannot_read(tmp_path: Path) -> None:
    home = tmp_path / "sbhome"
    home.mkdir()
    (home / "config.toml").write_text("[cursor\nstop_park_s = 5\n")
    uh = user_home(tmp_path, {})
    with pytest.raises(InstallError, match="can't read .*config.toml"):
        cursor.plan(uh, PY, str(home), hook_sha12())


def test_cursor_uses_defaults_when_the_home_config_is_not_a_file(tmp_path: Path) -> None:
    home = tmp_path / "sbhome"
    (home / "config.toml").mkdir(parents=True)  # read_bytes -> IsADirectoryError (an OSError)
    assert cursor.config_for(str(home)) == Config()
    uh = user_home(tmp_path, {})
    plan = cursor.plan(uh, PY, str(home), hook_sha12())
    stop = json.loads(plan.edits[1].after)["hooks"]["stop"]
    assert stop[0]["timeout"] == Config().cursor.stop_park_s + cursor.STOP_TIMEOUT_EXTRA_S


@pytest.mark.parametrize(
    "rel,data,msg",
    [
        (
            ".cursor/hooks.json",
            {"version": 1, "hooks": {"stop": {"command": "x"}}},
            '"hooks.stop" is not a list',
        ),
        (
            ".cursor/mcp.json",
            {"mcpServers": ["switchboard"]},
            '"mcpServers" in ~/.cursor/mcp.json is not an object',
        ),
    ],
)
def test_cursor_refuses_shapes_it_cannot_merge_into(tmp_path: Path, rel: str, data: dict, msg: str) -> None:
    uh = user_home(tmp_path, {rel: json.dumps(data)})
    with pytest.raises(InstallError, match=msg):
        cursor.plan(uh, PY, HOME, hook_sha12())


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
@pytest.mark.parametrize(
    "rel,data,msg",
    [
        (
            ".config/devin/config.json",
            {"permissions": ["mcp__switchboard__join"]},
            '"permissions" in ~/.config/devin/config.json is not an object',
        ),
        (
            ".config/devin/config.json",
            {"permissions": {"allow": "mcp__switchboard__*"}},
            '"permissions.allow" is not a list',
        ),
        (
            ".config/devin/mcp_config.json",
            {"mcpServers": "switchboard"},
            '"mcpServers" in ~/.config/devin/mcp_config.json is not an object',
        ),
    ],
)
def test_devin_refuses_shapes_it_cannot_merge_into(tmp_path: Path, rel: str, data: dict, msg: str) -> None:
    uh = user_home(tmp_path, {rel: json.dumps(data)})
    with pytest.raises(InstallError, match=msg):
        devin.plan(uh, PY, HOME, hook_sha12())


def test_devin_other_mcp_home_reads_only_a_switchboard_entry_for_another_home() -> None:
    other = json.dumps({"mcpServers": {"switchboard": devin.mcp_entry(PY, "/opt/other")}})
    assert devin._other_mcp_home(other, HOME) == "/opt/other"
    same = json.dumps({"mcpServers": {"switchboard": devin.mcp_entry(PY, HOME)}})
    assert devin._other_mcp_home(same, HOME) is None
    assert devin._other_mcp_home(json.dumps({"mcpServers": {"x": {"command": "node"}}}), HOME) is None
    assert devin._other_mcp_home(json.dumps(["not", "an", "object"]), HOME) is None
    for empty in (None, "", "  \n"):
        assert devin._other_mcp_home(empty, HOME) is None
    assert devin._other_mcp_home("{not json", HOME) is None
