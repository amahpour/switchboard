"""switchboard install claude: golden diffs, idempotency, backups, refusals (DESIGN.md §9.7)."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

import pytest

from switchboard import guardrails
from switchboard.cli import build_parser
from switchboard.install import claude, common
from switchboard.install.common import InstallError, hook_command, render_plan, run_install
from switchboard.paths import hook_sha12

GOLD = Path(__file__).resolve().parents[1] / "fixtures" / "install" / "claude"
PY = "/opt/yk/venv/bin/python"
HOME = "/opt/yk/home"


def norm(s: str) -> str:
    return s.replace(hook_sha12(), "<SHA12>")


def seeded(tmp_path: Path, seed: str | None) -> Path:
    uh = tmp_path / "userhome"
    (uh / ".claude").mkdir(parents=True)
    if seed:
        shutil.copy(GOLD / seed, uh / ".claude" / "settings.json")
    return uh


@pytest.mark.parametrize("name,seed", [("fresh", None), ("merge", "settings.seed.json")])
def test_golden_plan_and_diff(tmp_path: Path, name: str, seed: str | None) -> None:
    uh = seeded(tmp_path, seed)
    plan = claude.plan(uh, PY, HOME, hook_sha12(), run_commands=False)
    out = io.StringIO()
    render_plan(plan, out)
    assert norm(plan.edits[1].after) == (GOLD / f"{name}.settings.after.json").read_text()
    assert norm(out.getvalue()) == (GOLD / f"{name}.diff.txt").read_text()


def test_merge_keeps_everything_else(tmp_path: Path) -> None:
    uh = seeded(tmp_path, "settings.seed.json")
    plan = claude.plan(uh, PY, HOME, hook_sha12())
    before = json.loads((GOLD / "settings.seed.json").read_text())
    after = json.loads(plan.edits[1].after)
    assert after["model"] == before["model"] and after["env"] == before["env"]
    assert after["permissions"] == before["permissions"]  # never adds an allow rule
    assert after["hooks"]["PostToolUse"][0] == before["hooks"]["PostToolUse"][0]
    assert after["hooks"]["Notification"] == before["hooks"]["Notification"]
    assert "PermissionRequest" not in after["hooks"]
    assert set(after["hooks"]) == {"PostToolUse", "Notification", *claude.EVENTS}


def test_diff_never_prints_other_values(tmp_path: Path) -> None:
    uh = seeded(tmp_path, "settings.seed.json")
    out = io.StringIO()
    render_plan(claude.plan(uh, PY, HOME, hook_sha12()), out)
    text = out.getvalue()
    assert "placeholder-not-a-secret" not in text and "user-hook" not in text and "git status" not in text


def test_mask() -> None:
    m = common.mask({"env": {"A": "1"}, "apiKey": "x", "url": "http://x", "nested": [{"token": "t"}], "ok": "v"})
    assert m["apiKey"] == "***" and m["url"] == "***" and m["nested"][0]["token"] == "***" and m["ok"] == "v"
    assert m["env"] == "***"  # a whole object or list under a secret-looking key is masked


def test_mcp_command_runs_python_directly() -> None:
    e = claude.mcp_entry(sys.executable, "/opt/yk/home")
    assert e["command"] == sys.executable and e["args"] == ["-I", "-m", "switchboard", "mcp", "--home", "/opt/yk/home"]
    assert "uv" not in os.path.basename(e["command"]) and "run" not in e["args"]
    plan = claude.plan(Path("/nonexistent-home"), PY, HOME, hook_sha12())
    cmd = plan.edits[0]
    assert isinstance(cmd, common.CommandEdit)
    assert cmd.argv[:6] == ["claude", "mcp", "add-json", "--scope", "user", "switchboard"]
    assert json.loads(cmd.argv[6]) == {"type": "stdio", "command": PY,
                                       "args": ["-I", "-m", "switchboard", "mcp", "--home", HOME]}


def test_hook_command_shape_and_refusals() -> None:
    c = hook_command(PY, HOME, "abcdef012345", "claude", "Stop")
    assert c.startswith("/bin/sh -c '") and c.endswith("; exit 0'")
    assert "exec \"$P\" -I -S \"$H\" --home \"/opt/yk/home\" --harness claude --event Stop" in c
    assert hook_command(PY, HOME, "abcdef012345", "claude", "Stop") == c  # byte-stable
    for bad in ("/opt/it's/home", '/opt/"q"/home', "/opt/$HOME", "relative/home", "/opt/a`b`"):
        with pytest.raises(InstallError):
            hook_command(PY, bad, "abcdef012345", "claude", "Stop")


def args(*extra: str):
    return build_parser().parse_args(["install", "claude", *extra])


def test_apply_backup_and_idempotency(tmp_path: Path, tmp_home: Path, monkeypatch) -> None:
    monkeypatch.setattr(common, "editable_install", lambda: False)
    uh = seeded(tmp_path, "settings.seed.json")
    target = uh / ".claude" / "settings.json"
    os.chmod(target, 0o644)
    out = io.StringIO()
    rc = run_install(args("--home", str(tmp_home), "--user-home", str(uh), "--yes"), out=out)
    assert rc == 0, out.getvalue()
    assert "skipped (--user-home): claude mcp add-json" in out.getvalue()
    backups = list(target.parent.glob("settings.json.bak-switchboard-*"))
    assert len(backups) == 1 and re.fullmatch(r"settings\.json\.bak-switchboard-\d{14}", backups[0].name)
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600  # 0644 original: backup tightened
    assert backups[0].read_text() == (GOLD / "settings.seed.json").read_text()
    assert stat.S_IMODE(target.stat().st_mode) == 0o644  # the file keeps its mode
    assert (tmp_home / "hooks" / f"switchboard_hook-{hook_sha12()}.py").exists()
    data = target.read_text()
    out2 = io.StringIO()
    assert run_install(args("--home", str(tmp_home), "--user-home", str(uh), "--yes"), out=out2) == 0
    assert "no changes" in out2.getvalue() and target.read_text() == data
    assert len(list(target.parent.glob("settings.json.bak-switchboard-*"))) == 1


def test_older_hook_version_is_replaced_not_duplicated(tmp_path: Path, tmp_home: Path) -> None:
    uh = seeded(tmp_path, None)
    home = str(tmp_home.resolve())
    old = hook_command(PY, home, "000000000000", "claude", "Stop")
    (uh / ".claude" / "settings.json").write_text(json.dumps(
        {"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": old, "timeout": 10}]}]}}))
    plan = claude.plan(uh, PY, home, hook_sha12())
    stop = json.loads(plan.edits[1].after)["hooks"]["Stop"]
    cmds = [h["command"] for g in stop for h in g["hooks"]]
    assert len(cmds) == 1 and hook_sha12() in cmds[0]
    assert any("older switchboard hook" in line for line in plan.edits[1].display)


def test_print_args_writes_nothing(tmp_path: Path, tmp_home: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    (tmp_path / "h").mkdir()
    out = io.StringIO()
    assert run_install(args("--home", str(tmp_home), "--print-args"), out=out) == 0
    data = json.loads(out.getvalue())
    assert data["argv"][0] == "--mcp-config" and data["argv"][2] == "--settings"
    mcp = json.loads(data["argv"][1])["mcpServers"]["switchboard"]
    assert mcp["command"] == sys.executable
    settings = json.loads(data["argv"][3])
    assert set(settings) == {"hooks"} and set(settings["hooks"]) == set(claude.EVENTS)
    assert guardrails.find_forbidden(data) == []
    assert "allowedTools" not in out.getvalue() and "permission" not in out.getvalue().lower()
    assert list((tmp_path / "h").iterdir()) == []
    assert not (tmp_home / "hooks").exists()


def test_editable_install_is_refused(tmp_path: Path, tmp_home: Path, monkeypatch) -> None:
    monkeypatch.setattr(common, "editable_install", lambda: True)
    uh = seeded(tmp_path, None)
    assert run_install(args("--home", str(tmp_home), "--user-home", str(uh), "--yes"), out=io.StringIO()) == 1
    assert not (uh / ".claude" / "settings.json").exists()
    assert run_install(args("--home", str(tmp_home), "--user-home", str(uh), "--yes", "--allow-editable"),
                       out=io.StringIO()) == 0


def test_this_dev_venv_is_detected_as_editable() -> None:
    assert common.editable_install() is True  # uv sync installs the project in editable mode


def test_no_tty_and_no_yes_refuses(tmp_path: Path, tmp_home: Path, monkeypatch) -> None:
    monkeypatch.setattr(common, "editable_install", lambda: False)
    uh = seeded(tmp_path, None)
    rc = run_install(args("--home", str(tmp_home), "--user-home", str(uh)), stdin=io.StringIO("y\n"),
                     out=io.StringIO())
    assert rc == 1 and not (uh / ".claude" / "settings.json").exists()


def test_dry_run_writes_nothing(tmp_path: Path, tmp_home: Path) -> None:
    uh = seeded(tmp_path, "settings.seed.json")
    before = (uh / ".claude" / "settings.json").read_bytes()
    out = io.StringIO()
    assert run_install(args("--home", str(tmp_home), "--user-home", str(uh), "--dry-run"), out=out) == 0
    assert (uh / ".claude" / "settings.json").read_bytes() == before
    assert not list((uh / ".claude").glob("*.bak-switchboard-*"))
    assert "hooks.Stop" in out.getvalue()


def test_command_edits_never_run_with_user_home(tmp_path: Path, tmp_home: Path, monkeypatch) -> None:
    monkeypatch.setattr(common, "editable_install", lambda: False)

    def boom(*a, **k):
        raise AssertionError("must not run harness CLIs in tests")

    monkeypatch.setattr(common.subprocess, "run", boom)
    uh = seeded(tmp_path, None)
    assert run_install(args("--home", str(tmp_home), "--user-home", str(uh), "--yes"), out=io.StringIO()) == 0


def test_bad_json_is_refused(tmp_path: Path, tmp_home: Path) -> None:
    uh = seeded(tmp_path, None)
    (uh / ".claude" / "settings.json").write_text("{not json")
    assert run_install(args("--home", str(tmp_home), "--user-home", str(uh), "--dry-run"), out=io.StringIO()) == 1


def test_every_harness_has_an_installer(tmp_home: Path, tmp_path: Path) -> None:
    """Since M5 all four harnesses install (dry-run here: nothing is written)."""
    uh = tmp_path / "uh"
    uh.mkdir()
    for h in ("claude", "codex", "cursor", "devin"):
        out = io.StringIO()
        assert run_install(build_parser().parse_args(["install", h, "--dry-run", "--home", str(tmp_home),
                                                      "--user-home", str(uh)]), out=out) == 0
        assert out.getvalue().startswith(f"switchboard install {h}:")
    assert list(uh.iterdir()) == []


def test_installed_config_has_no_forbidden_strings(tmp_path: Path) -> None:
    uh = seeded(tmp_path, "settings.seed.json")
    plan = claude.plan(uh, PY, HOME, hook_sha12())
    assert guardrails.find_forbidden(plan.edits[1].after) == []
    assert guardrails.find_forbidden(plan.edits[0].argv) == []


# ------------------------------------------------ re-install (review M2)
def register(uh: Path, entry: dict) -> None:
    """What `claude mcp add-json --scope user` leaves in ~/.claude.json (plus unrelated keys)."""
    (uh / ".claude.json").write_text(json.dumps({"numStartups": 3, "projects": {"/ws": {}},
                                                 "mcpServers": {"other": {"command": "x", "env": {"K": "v"}},
                                                                "switchboard": entry}}))


def test_reinstall_with_everything_in_place_says_no_changes(tmp_path: Path, tmp_home: Path, monkeypatch) -> None:
    monkeypatch.setattr(common, "editable_install", lambda: False)
    monkeypatch.setattr(common.subprocess, "run", lambda *a, **k: pytest.fail("nothing to run"))
    uh = seeded(tmp_path, "settings.seed.json")
    home = str(tmp_home.resolve())
    assert run_install(args("--home", home, "--user-home", str(uh), "--yes"), out=io.StringIO()) == 0
    register(uh, claude.mcp_entry(sys.executable, home))
    snapshot = {p: p.read_bytes() for p in uh.rglob("*") if p.is_file()}
    out = io.StringIO()
    assert run_install(args("--home", home, "--user-home", str(uh), "--yes"), out=out) == 0
    lines = out.getvalue().splitlines()
    assert lines[-1] == "no changes", out.getvalue()
    assert "claude mcp switchboard (user scope): no changes" in lines
    assert {p: p.read_bytes() for p in uh.rglob("*") if p.is_file()} == snapshot
    assert "numStartups" not in out.getvalue() and "other" not in out.getvalue()


def test_a_different_registered_entry_is_removed_then_added(tmp_path: Path) -> None:
    uh = seeded(tmp_path, None)
    register(uh, {"type": "stdio", "command": "/old/venv/bin/python", "args": ["-I", "-m", "switchboard", "mcp"]})
    plan = claude.plan(uh, PY, HOME, hook_sha12())
    cmds = [e for e in plan.edits if isinstance(e, common.CommandEdit)]
    assert [c.argv[:3] for c in cmds] == [["claude", "mcp", "remove"], ["claude", "mcp", "add-json"]]
    assert cmds[0].argv == ["claude", "mcp", "remove", "--scope", "user", "switchboard"]
    assert all(c.changed for c in cmds)


@pytest.mark.parametrize("foreign", [
    {"type": "stdio", "command": "npx", "args": ["-y", "switchboard-flags"]},
    {"type": "http", "url": "https://example.test/mcp"},
    {"type": "stdio", "command": "/usr/bin/python3", "args": ["-m", "switchboard"]},
])
def test_an_mcp_server_named_switchboard_that_isnt_ours_is_refused(tmp_path: Path, foreign: dict) -> None:
    """"switchboard" is a common word: another tool's user-scope server of that
    name is never removed (no `claude mcp remove`, no hooks written)."""
    uh = seeded(tmp_path, None)
    register(uh, foreign)
    with pytest.raises(InstallError, match=r"~/\.claude\.json \(user scope\) already has an MCP server named"
                                           r" switchboard that isn't switchboard's"):
        claude.plan(uh, PY, HOME, hook_sha12())
    # switchboard's own entry, for another home or an older Python, is still replaced
    register(uh, {"type": "stdio", "command": "/old/python", "args": ["-I", "-m", "switchboard", "mcp", "--home", "/x"]})
    cmds = [e for e in claude.plan(uh, PY, HOME, hook_sha12()).edits if isinstance(e, common.CommandEdit)]
    assert [c.argv[:3] for c in cmds] == [["claude", "mcp", "remove"], ["claude", "mcp", "add-json"]]
    assert cmds[0].note == "replaces an older switchboard entry"


def test_files_are_written_first_and_a_failed_command_is_reported(tmp_path: Path, monkeypatch, capsys) -> None:
    uh = seeded(tmp_path, None)
    register(uh, {"type": "stdio", "command": "/old/python", "args": ["-I", "-m", "switchboard", "mcp", "--home", "/old"]})
    plan = claude.plan(uh, PY, HOME, hook_sha12())
    ran: list[list[str]] = []

    class R:
        returncode = 1

    def fake_run(argv, **kw):
        ran.append(argv)
        assert (uh / ".claude" / "settings.json").exists()  # files before commands
        return R()

    monkeypatch.setattr(common.subprocess, "run", fake_run)
    out = io.StringIO()
    assert common.apply_plan(plan, run_commands=True, out=out) == 1
    assert (uh / ".claude" / "settings.json").exists()
    assert [a[:3] for a in ran] == [["claude", "mcp", "remove"]]  # the add after a failed remove is skipped
    assert "Run it yourself" in capsys.readouterr().err


def test_atomic_write_does_not_follow_a_planted_temp_symlink(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text("{}")
    os.chmod(target, 0o640)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    for name in (f".settings.json.switchboard-{os.getpid()}.tmp", ".settings.json.switchboard-.tmp"):
        (tmp_path / name).symlink_to(victim)
    common.atomic_write(target, '{"a": 1}\n')
    assert victim.read_text() == "keep me" and target.read_text() == '{"a": 1}\n'
    assert not target.is_symlink() and stat.S_IMODE(target.stat().st_mode) == 0o640
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".settings.json.switchboard-") and not p.is_symlink()]
