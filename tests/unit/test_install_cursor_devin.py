"""switchboard install cursor|devin (DESIGN.md §9.7): golden diffs, the stop hook's
timeout / loop_limit / --max-wait, Devin's eight allow names (and nothing else),
idempotency, replacing an older switchboard hook, print-args and applying into a
temp user home. Never the real ~/.cursor or ~/.config/devin."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import shutil
from pathlib import Path

import pytest

from switchboard import guardrails
from switchboard.cli import build_parser
from switchboard.config import Config
from switchboard.install import cursor, devin
from switchboard.install.common import InstallError, render_plan, run_install
from switchboard.paths import hook_sha12

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "install"
PY = "/opt/yk/venv/bin/python"
HOME = "/opt/yk/home"
REGEN = os.environ.get("SWITCHBOARD_REGEN_GOLDEN") == "1"
SEEDS = {
    "cursor": {".cursor/mcp.json": "mcp.seed.json", ".cursor/hooks.json": "hooks.seed.json"},
    "devin": {
        ".config/devin/mcp_config.json": "mcp_config.seed.json",
        ".config/devin/config.json": "config.seed.json",
    },
}
MODS = {"cursor": cursor, "devin": devin}


def norm(s: str) -> str:
    return s.replace(hook_sha12(), "<SHA12>")


def seeded(tmp_path: Path, harness: str, seed: bool) -> Path:
    uh = tmp_path / "userhome"
    for rel, name in SEEDS[harness].items():
        (uh / rel).parent.mkdir(parents=True, exist_ok=True)
        if seed:
            shutil.copy(FIX / harness / name, uh / rel)
    return uh


def gold(harness: str, name: str, got: str) -> None:
    p = FIX / harness / name
    if REGEN:
        p.write_text(got)
    assert got == p.read_text(), f"{harness}/{name}"


@pytest.mark.parametrize("harness", ["cursor", "devin"])
@pytest.mark.parametrize("name,seed", [("fresh", False), ("merge", True)])
def test_golden_plan_and_diff(tmp_path: Path, harness: str, name: str, seed: bool) -> None:
    uh = seeded(tmp_path, harness, seed)
    plan = MODS[harness].plan(uh, PY, HOME, hook_sha12(), run_commands=False)
    out = io.StringIO()
    render_plan(plan, out)
    for e, rel in zip(plan.edits, SEEDS[harness]):
        gold(harness, f"{name}.{Path(rel).name}.after", norm(e.after))
    gold(harness, f"{name}.diff.txt", norm(out.getvalue()))


def test_cursor_hooks_shape_and_the_stop_park_settings(tmp_path: Path) -> None:
    uh = seeded(tmp_path, "cursor", True)
    plan = cursor.plan(uh, PY, HOME, hook_sha12())
    mcp = json.loads(plan.edits[0].after)
    assert mcp["mcpServers"]["switchboard"] == {
        "command": PY,
        "args": ["-I", "-m", "switchboard", "mcp", "--home", HOME],
    }
    assert mcp["mcpServers"]["other-server"]["env"] == {"OTHER_API_TOKEN": "placeholder-not-a-secret"}
    data = json.loads(plan.edits[1].after)
    assert data["version"] == 1
    hooks = data["hooks"]
    assert set(hooks) == {
        "sessionStart",
        "beforeSubmitPrompt",
        "postToolUse",
        "postToolUseFailure",
        "stop",
        "sessionEnd",
        "afterFileEdit",
    }
    assert hooks["afterFileEdit"] == [{"command": "echo user-hook"}]
    assert hooks["stop"][0] == {"command": "notify-user done", "timeout": 30}  # the user's own hook is kept
    ours = hooks["stop"][1]
    assert ours["timeout"] == 660 and "loop_limit" in ours and ours["loop_limit"] is None
    assert "--harness cursor --event stop --max-wait 630;" in ours["command"]
    for ev in ("sessionStart", "beforeSubmitPrompt", "postToolUse", "postToolUseFailure", "sessionEnd"):
        [h] = hooks[ev]
        assert h["timeout"] == 10 and f"--event {ev};" in h["command"] and "--max-wait" not in h["command"]
    assert "preToolUse" not in hooks and "beforeShellExecution" not in hooks


def test_cursor_stop_timing_follows_the_homes_config(tmp_path: Path, tmp_home: Path) -> None:
    (tmp_home / "config.toml").write_text("[cursor]\nstop_park_s = 120\n")
    uh = seeded(tmp_path, "cursor", False)
    hooks = json.loads(cursor.plan(uh, PY, str(tmp_home), hook_sha12()).edits[1].after)["hooks"]
    assert hooks["stop"][0]["timeout"] == 180 and "--max-wait 150;" in hooks["stop"][0]["command"]
    assert (
        cursor.hook_entries(
            PY,
            HOME,
            hook_sha12(),
            dataclasses.replace(Config(), cursor=dataclasses.replace(Config().cursor, stop_park_s=60)),
        )["stop"]["timeout"]
        == 120
    )


def test_devin_hooks_and_exactly_the_eight_allow_names(tmp_path: Path) -> None:
    uh = seeded(tmp_path, "devin", True)
    plan = devin.plan(uh, PY, HOME, hook_sha12())
    cfg = json.loads(plan.edits[1].after)
    assert cfg["shell"] == "zsh" and cfg["version"] == 1
    assert cfg["permissions"]["deny"] == ["exec(rm *)"]
    assert cfg["permissions"]["allow"] == ["read"] + [f"mcp__switchboard__{t}" for t in devin.TOOLS]
    assert len(devin.ALLOW) == 8 and not any("*" in a for a in devin.ALLOW)
    hooks = cfg["hooks"]
    assert set(hooks) == {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "SessionEnd",
    }
    assert hooks["Stop"][0]["hooks"][0]["command"] == "notify-user done"
    timeouts = {e: hooks[e][-1]["hooks"][0]["timeout"] for e in hooks}
    assert timeouts == {
        "SessionStart": 10,
        "UserPromptSubmit": 10,
        "PreToolUse": 10,
        "PostToolUse": 10,
        "Stop": 30,
        "SessionEnd": 10,
    }
    assert all(hooks[e][-1]["matcher"] == "" for e in hooks)
    for key in ("read_config_from", "PermissionRequest", "trust", "bypass", "mcp__switchboard__*"):
        assert key not in plan.edits[1].after
    mcp = json.loads(plan.edits[0].after)
    assert mcp["mcpServers"]["switchboard"]["command"] == PY and "other-server" in mcp["mcpServers"]


@pytest.mark.parametrize("harness", ["cursor", "devin"])
def test_reinstall_is_idempotent_and_replaces_an_older_hook(tmp_path: Path, harness: str) -> None:
    mod = MODS[harness]
    uh = seeded(tmp_path, harness, True)
    plan = mod.plan(uh, PY, HOME, "0" * 12)
    for e in plan.edits:
        e.path.write_text(e.after)
    newer = mod.plan(uh, PY, HOME, hook_sha12())
    text = newer.edits[1].after
    assert "0" * 12 not in text and hook_sha12() in text
    for e in newer.edits:
        e.path.write_text(e.after)
    again = mod.plan(uh, PY, HOME, hook_sha12())
    assert not again.changed
    # the allow list is never doubled
    if harness == "devin":
        allow = json.loads(again.edits[1].after or (uh / ".config/devin/config.json").read_text())
        assert allow["permissions"]["allow"].count("mcp__switchboard__wait") == 1


@pytest.mark.parametrize("harness", ["cursor", "devin"])
def test_diff_masks_other_values_and_nothing_forbidden_is_written(tmp_path: Path, harness: str) -> None:
    uh = seeded(tmp_path, harness, True)
    plan = MODS[harness].plan(uh, PY, HOME, hook_sha12())
    out = io.StringIO()
    render_plan(plan, out)
    text = out.getvalue()
    assert "placeholder-not-a-secret" not in text and "notify-user" not in text and "other-server" not in text
    for e in plan.edits:
        assert guardrails.find_forbidden(e.after) == []
        assert "permissionDecision" not in e.after and '"decision"' not in e.after


@pytest.mark.parametrize("harness", ["cursor", "devin"])
def test_refuses_a_malformed_file(tmp_path: Path, harness: str) -> None:
    uh = seeded(tmp_path, harness, False)
    rel = list(SEEDS[harness])[1]
    (uh / rel).write_text('{"hooks": []}')
    with pytest.raises(InstallError):
        MODS[harness].plan(uh, PY, HOME, hook_sha12())
    (uh / rel).write_text("{not json")
    with pytest.raises(InstallError, match="not valid JSON"):
        MODS[harness].plan(uh, PY, HOME, hook_sha12())


@pytest.mark.parametrize("harness", ["cursor", "devin"])
def test_an_mcp_server_named_switchboard_that_isnt_ours_is_refused(
    tmp_path: Path, tmp_home: Path, harness: str
) -> None:
    """ "switchboard" is a common word: another tool's server of that name is never
    overwritten; install fails for that harness and leaves the file alone."""
    uh = seeded(tmp_path, harness, False)
    rel = list(SEEDS[harness])[0]
    where = {"cursor": "~/.cursor/mcp.json", "devin": "~/.config/devin/mcp_config.json"}[harness]
    for foreign in (
        {"command": "npx", "args": ["-y", "switchboard-flags"]},
        {"url": "https://example.test/mcp"},
        {"command": "/usr/bin/python3", "args": ["-m", "switchboard"]},
        "switchboard",
    ):
        (uh / rel).write_text(json.dumps({"mcpServers": {"other": {"command": "x"}, "switchboard": foreign}}))
        before = (uh / rel).read_bytes()
        with pytest.raises(
            InstallError,
            match=f"{where} already has an MCP server named switchboard that isn't switchboard's",
        ):
            MODS[harness].plan(uh, PY, HOME, hook_sha12())
        a = build_parser().parse_args(
            ["install", harness, "--home", str(tmp_home), "--user-home", str(uh), "--yes", "--allow-editable"]
        )
        assert run_install(a, out=io.StringIO()) == 1
        assert (uh / rel).read_bytes() == before
        assert not list((uh / rel).parent.glob("*.bak-switchboard-*"))
    # switchboard's own entry, for another home or an older Python, is still replaced
    (uh / rel).write_text(
        json.dumps(
            {
                "mcpServers": {
                    "switchboard": {
                        "command": "/old/python",
                        "args": ["-I", "-m", "switchboard", "mcp", "--home", "/opt/other"],
                    }
                }
            }
        )
    )
    plan = MODS[harness].plan(uh, PY, HOME, hook_sha12())
    assert json.loads(plan.edits[0].after)["mcpServers"]["switchboard"]["args"][-1] == HOME
    assert plan.edits[0].display[0].startswith("  ~ mcpServers.switchboard: ")


@pytest.mark.parametrize("harness", ["cursor", "devin"])
def test_print_args_writes_nothing(tmp_home: Path, harness: str) -> None:
    before = sorted(p.name for p in tmp_home.iterdir())
    out = io.StringIO()
    rc = run_install(
        build_parser().parse_args(["install", harness, "--print-args", "--home", str(tmp_home)]), out=out
    )
    assert rc == 0
    data = json.loads(out.getvalue())
    assert set(data) == {"argv", "env", "files", "notes"} and data["argv"] == []
    files = data["files"]
    if harness == "cursor":
        assert set(files) == {".cursor/mcp.json", ".cursor/hooks.json"}
        hooks = json.loads(files[".cursor/hooks.json"])
        assert hooks["version"] == 1 and hooks["hooks"]["stop"][0]["loop_limit"] is None
        mcp = json.loads(files[".cursor/mcp.json"])
    else:
        assert set(files) == {".devin/config.json", ".devin/mcp_config.json"}
        cfg = json.loads(files[".devin/config.json"])
        assert cfg["permissions"] == {"allow": list(devin.ALLOW)} and "read_config_from" not in cfg
        mcp = json.loads(files[".devin/mcp_config.json"])
    assert mcp["mcpServers"]["switchboard"]["args"][-2:] == ["--home", os.path.realpath(tmp_home)]
    assert guardrails.find_forbidden(data) == []
    assert sorted(p.name for p in tmp_home.iterdir()) == before


@pytest.mark.parametrize("harness", ["cursor", "devin"])
def test_apply_into_a_temp_user_home(tmp_path: Path, tmp_home: Path, harness: str) -> None:
    uh = seeded(tmp_path, harness, True)
    for rel in SEEDS[harness]:
        (uh / rel).chmod(0o644)
    a = build_parser().parse_args(
        ["install", harness, "--home", str(tmp_home), "--user-home", str(uh), "--yes", "--allow-editable"]
    )
    assert run_install(a, out=io.StringIO()) == 0
    for rel in SEEDS[harness]:
        baks = sorted((uh / rel).parent.glob(f"{Path(rel).name}.bak-switchboard-*"))
        assert len(baks) == 1 and oct(baks[0].stat().st_mode & 0o777) == "0o600"
        assert json.loads((uh / rel).read_text())
    assert run_install(a, out=(o := io.StringIO())) == 0 and "no changes" in o.getvalue()
