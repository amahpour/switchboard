"""Shared install machinery (DESIGN.md §9.7): recognising switchboard's entries, the
allowlisted diff views, JSON shapes install refuses, atomic writes that fail
half-way, the editable-install check, applying plans whose harness command
can't run, and ``--purge-hooks`` when the hooks dir or a config can't be read.
Temp dirs only, never the real ~."""

from __future__ import annotations

import importlib.metadata
import io
import json
import os
from pathlib import Path
from typing import Any

import pytest

from switchboard import DIST_NAME
from switchboard.install import common
from switchboard.install.common import (
    CommandEdit,
    FileEdit,
    InstallError,
    Plan,
    PurgeEdit,
    hook_command,
)
from switchboard.paths import hook_sha12

PY = "/opt/yk/venv/bin/python"
HOME = "/opt/yk/home"
SHA = "abcdef012345"


# ------------------------------------------------------------ recognising
@pytest.mark.parametrize(
    "sha12,harness,event",
    [
        ("ABCDEF012345", "claude", "Stop"),  # upper-case hex
        ("abcdef01234", "claude", "Stop"),  # 11 digits
        (SHA, "Claude", "Stop"),
        (SHA, "claude code", "Stop"),
        (SHA, "claude", "Post-Tool"),
        (SHA, "claude", "Stop;id"),
    ],
)
def test_hook_command_refuses_bad_parts(sha12: str, harness: str, event: str) -> None:
    with pytest.raises(InstallError, match="bad hook command parts"):
        hook_command(PY, HOME, sha12, harness, event)


def test_runs_switchboard_mcp_and_its_home() -> None:
    ours = {"command": PY, "args": ["-I", "-m", "switchboard", "mcp", "--home", HOME]}
    assert common.runs_switchboard_mcp(ours) and common.mcp_home(ours) == HOME
    for not_ours in (
        {"command": PY, "args": "-m switchboard mcp"},  # args not a list
        {"command": PY, "args": ["-m", "switchboard", "mcp", 5]},  # a non-string arg
        {"command": PY, "args": ["-m", "switchboard", "web"]},
        {"command": 5, "args": ["-m", "switchboard", "mcp"]},
        "switchboard",
        None,
    ):
        assert not common.runs_switchboard_mcp(not_ours) and common.mcp_home(not_ours) is None
    # ours, but no usable --home
    assert common.mcp_home({"command": PY, "args": ["-m", "switchboard", "mcp"]}) is None
    assert common.mcp_home({"command": PY, "args": ["-m", "switchboard", "mcp", "--home"]}) is None


def test_removed_entries_are_shown_through_an_allowlist() -> None:
    assert common.mcp_view("a string entry") == "***"
    assert common.mcp_view(["a", "list"]) == "***"
    assert common.mcp_view(
        {"command": PY, "args": ["--api-key", "k", "--x"], "env": {"A": "1"}, "cwd": "/w"}
    ) == {"command": PY, "args": ["--api-key", "***", "--x"], "env": "***", "cwd": "***"}
    assert common.hook_view(10) == 10 and common.hook_view("x") == "x" and common.hook_view(None) is None
    assert common.hook_view(
        [{"matcher": "", "hooks": [{"command": "c", "timeout": 3, "note": "n"}], "id": 7}, 5]
    ) == [{"matcher": "", "hooks": [{"command": "c", "timeout": 3, "note": "***"}], "id": "***"}, 5]


# ------------------------------------------------------------------- json
def test_load_json_obj_refuses_anything_but_an_object(tmp_path: Path) -> None:
    f = tmp_path / "settings.json"
    for text in ("[1, 2]", '"s"', "3", "null"):
        f.write_text(text)
        with pytest.raises(InstallError, match="must contain a JSON object"):
            common.load_json_obj(f)
    f.write_text("  \n")
    assert common.load_json_obj(f) == ("  \n", {})
    assert common.load_json_obj(tmp_path / "missing.json") == (None, {})


def test_set_hook_groups_refuses_an_event_that_is_not_a_list() -> None:
    with pytest.raises(InstallError, match='"hooks.Stop" is not a list'):
        common.set_hook_groups({"hooks": {"Stop": {"matcher": ""}}}, {"Stop": ("cmd", 30)}, HOME)


def test_set_hook_groups_keeps_the_users_handlers_in_a_shared_group() -> None:
    """An older switchboard handler inside the user's own group is dropped from
    that group; the user's handlers there stay, and switchboard gets its own group."""
    old = hook_command(PY, HOME, "0" * 12, "claude", "Stop")
    new = hook_command(PY, HOME, SHA, "claude", "Stop")
    mine = {"type": "command", "command": "echo mine"}
    data = {"hooks": {"Stop": [{"matcher": "x", "hooks": [mine, {"type": "command", "command": old}]}]}}
    lines = common.set_hook_groups(data, {"Stop": (new, 30)}, HOME)
    assert data["hooks"]["Stop"] == [
        {"matcher": "x", "hooks": [mine]},
        {"matcher": "", "hooks": [{"type": "command", "command": new, "timeout": 30}]},
    ]
    assert lines[0] == "  - hooks.Stop: an older switchboard hook" and lines[1].startswith(
        "  + hooks.Stop[1]: "
    )


def test_remove_hook_groups_skips_events_that_are_not_lists() -> None:
    cmd = hook_command(PY, HOME, SHA, "claude", "Stop")
    data = {"hooks": {"Notification": {"odd": True}, "Stop": [{"hooks": [{"command": cmd}]}]}}
    lines, moved = common.remove_hook_groups(data, HOME, drop_empty_hooks=True)
    assert data == {"hooks": {"Notification": {"odd": True}}} and moved == 0
    assert [line.split(":")[0] for line in lines] == ["  - hooks.Stop[0]"]


# ---------------------------------------------------------------- writing
def test_atomic_write_that_fails_leaves_the_file_and_no_temp(tmp_path: Path) -> None:
    f = tmp_path / "hooks.json"
    f.write_text("original\n")
    with pytest.raises(UnicodeEncodeError):
        common.atomic_write(f, "half \ud800 written")  # a lone surrogate can't be UTF-8
    assert f.read_text() == "original\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["hooks.json"]


def test_atomic_write_onto_a_directory_fails_cleanly(tmp_path: Path) -> None:
    d = tmp_path / "settings.json"
    d.mkdir()
    with pytest.raises(OSError):
        common.atomic_write(d, "{}\n")
    assert d.is_dir() and sorted(p.name for p in tmp_path.iterdir()) == ["settings.json"]


def test_atomic_write_reports_the_real_error_if_the_temp_file_is_already_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def replace_then_fail(src: Any, dst: Any) -> None:
        os.unlink(src)  # something else cleaned it up first
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(common.os, "replace", replace_then_fail)
    f = tmp_path / "config.json"
    with pytest.raises(OSError, match="No space left"):
        common.atomic_write(f, "{}\n")
    assert list(tmp_path.iterdir()) == []


# ----------------------------------------------------------------- checks
class _Dist:
    def __init__(self, raw: str | None):
        self.raw = raw

    def read_text(self, name: str) -> str | None:
        assert name == "direct_url.json"
        return self.raw


@pytest.mark.parametrize(
    "dist,editable",
    [
        (None, False),  # not installed as a distribution at all
        (_Dist(None), False),  # no direct_url.json: installed from an index
        (_Dist(""), False),
        (_Dist("{not json"), False),
        (_Dist(json.dumps({"url": "file:///src", "dir_info": {}})), False),
        (_Dist(json.dumps({"url": "file:///src", "dir_info": {"editable": False}})), False),
        (_Dist(json.dumps({"url": "file:///src", "dir_info": {"editable": True}})), True),
    ],
)
def test_editable_install_reads_direct_url(
    monkeypatch: pytest.MonkeyPatch, dist: _Dist | None, editable: bool
) -> None:
    def distribution(name: str) -> _Dist:
        assert name == DIST_NAME  # the distribution's own name, not the package's (#233)
        if dist is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return dist

    monkeypatch.setattr(importlib.metadata, "distribution", distribution)
    assert common.editable_install() is editable


def test_module_refuses_an_unknown_harness() -> None:
    with pytest.raises(InstallError, match="unknown harness gemini"):
        common._module("gemini")


# ---------------------------------------------------------------- applying
def test_a_harness_command_that_cannot_start_fails_after_the_file_edits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "uh" / ".claude" / "settings.json"
    plan = Plan("claude")
    plan.edits.append(
        FileEdit(path=f, before=None, after="{}\n", display=["  + {}"], label="~/.claude/settings.json")
    )
    missing = str(tmp_path / "no-such-bin" / "claude")
    plan.edits.append(
        CommandEdit(argv=[missing, "mcp", "add", "switchboard"], display="claude mcp add switchboard")
    )
    out = io.StringIO()
    run = common._Run("claude", plan=plan)
    run.rc = common.apply_plan(plan, run_commands=True, out=out)
    assert run.rc == 1
    assert f.read_text() == "{}\n" and out.getvalue() == "wrote ~/.claude/settings.json\n"
    err = capsys.readouterr().err
    assert "failed (FileNotFoundError); any file edits above were applied" in err
    assert "Run it yourself:\n  claude mcp add switchboard" in err
    summary = io.StringIO()
    common._summary([run], "install", applied=True, out=summary)
    assert (
        summary.getvalue() == "summary:\n  claude: files written, but a harness command failed (see above)\n"
    )


def test_apply_purge_counts_only_what_it_deleted(tmp_path: Path) -> None:
    here = tmp_path / f"switchboard_hook-{SHA}.py"
    here.write_text("# hook\n")
    gone = tmp_path / f"switchboard_hook-{'0' * 12}.py"
    out = io.StringIO()
    common.apply_purge(PurgeEdit(label="~/sb/hooks", files=[here, gone]), out)
    assert out.getvalue() == "deleted 1 hook copy from ~/sb/hooks\n"
    assert not here.exists()
    out = io.StringIO()
    common.apply_purge(PurgeEdit(label="~/sb/hooks", files=[here, gone]), out)
    assert out.getvalue() == "deleted 0 hook copies from ~/sb/hooks\n"


# ------------------------------------------------------------ purge-hooks
def test_hook_copies_under_a_file_cannot_be_read(tmp_path: Path) -> None:
    f = tmp_path / "home"
    f.write_text("a file, not a switchboard home\n")
    files, problem = common.hook_copies(f / "hooks")
    assert files == [] and problem.startswith("can't read it: ")


def test_hook_copies_when_the_listing_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = tmp_path / "hooks"
    d.mkdir()
    (d / f"switchboard_hook-{SHA}.py").write_text("# hook\n")
    assert common.hook_copies(d) == ([d / f"switchboard_hook-{SHA}.py"], "")

    def denied(path: Any) -> list[str]:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(common.os, "listdir", denied)
    assert common.hook_copies(d) == ([], "can't read it: Permission denied")


def _copies(home: Path) -> list[Path]:
    hooks = home / "hooks"
    hooks.mkdir(parents=True)
    copy = hooks / f"switchboard_hook-{hook_sha12()}.py"
    copy.write_text("# hook\n")
    return [copy]


def test_purge_plan_without_a_real_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``Path.home()`` can fail (no HOME, no passwd entry): only --user-home is checked."""
    home, uh = tmp_path / "sb", tmp_path / "uh"
    files = _copies(home)
    uh.mkdir()

    def no_home() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(common.Path, "home", no_home)
    p = common.purge_plan([], uh, str(home))
    (edit,) = p.edits
    assert isinstance(edit, PurgeEdit) and edit.files == files and edit.blocked_by == [] and edit.changed
    assert p.title == "switchboard uninstall --purge-hooks"


def test_purge_plan_keeps_copies_when_a_config_cannot_be_read(tmp_path: Path) -> None:
    home, uh = tmp_path / "sb", tmp_path / "uh"
    files = _copies(home)
    (uh / ".claude").mkdir(parents=True)
    (uh / ".claude" / "settings.json").write_bytes(b'{"hooks": "\xff\xfe"}')  # not UTF-8
    (uh / ".cursor" / "hooks.json").mkdir(parents=True)  # a directory: OSError on read
    p = common.purge_plan([], uh, str(home))
    (edit,) = p.edits
    assert edit.files == files and not edit.changed
    assert edit.blocked_by == ["~/.claude/settings.json (unreadable)", "~/.cursor/hooks.json (unreadable)"]
    out = io.StringIO()
    common.render_plan(p, out)
    assert (
        "kept (still used by ~/.claude/settings.json (unreadable), ~/.cursor/hooks.json (unreadable))"
        in out.getvalue()
    )
