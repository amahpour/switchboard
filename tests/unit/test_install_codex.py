"""switchboard install codex (DESIGN.md §9.7): golden diffs, the marker block, hooks only
appended, idempotency, refusals, and what is never written."""

from __future__ import annotations

import io
import json
import os
import shutil
import tomllib
from pathlib import Path

import pytest

from switchboard import guardrails
from switchboard.cli import build_parser
from switchboard.install import codex
from switchboard.install.common import InstallError, render_plan, run_install
from switchboard.paths import hook_sha12

GOLD = Path(__file__).resolve().parents[1] / "fixtures" / "install" / "codex"
PY = "/opt/yk/venv/bin/python"
HOME = "/opt/yk/home"
REGEN = os.environ.get("SWITCHBOARD_REGEN_GOLDEN") == "1"


def norm(s: str) -> str:
    return s.replace(hook_sha12(), "<SHA12>")


def seeded(tmp_path: Path, seed: bool) -> Path:
    uh = tmp_path / "userhome"
    (uh / ".codex").mkdir(parents=True)
    if seed:
        shutil.copy(GOLD / "config.seed.toml", uh / ".codex" / "config.toml")
        shutil.copy(GOLD / "hooks.seed.json", uh / ".codex" / "hooks.json")
    return uh


def gold(name: str, got: str) -> None:
    p = GOLD / name
    if REGEN:
        p.write_text(got)
    assert got == p.read_text(), name


@pytest.mark.parametrize("name,seed", [("fresh", False), ("merge", True)])
def test_golden_plan_and_diff(tmp_path: Path, name: str, seed: bool) -> None:
    uh = seeded(tmp_path, seed)
    plan = codex.plan(uh, PY, HOME, hook_sha12(), run_commands=False)
    out = io.StringIO()
    render_plan(plan, out)
    gold(f"{name}.config.after.toml", norm(plan.edits[0].after))
    gold(f"{name}.hooks.after.json", norm(plan.edits[1].after))
    gold(f"{name}.diff.txt", norm(out.getvalue()))


def test_merge_keeps_everything_else_and_only_appends(tmp_path: Path) -> None:
    uh = seeded(tmp_path, True)
    plan = codex.plan(uh, PY, HOME, hook_sha12())
    seed = (GOLD / "config.seed.toml").read_text()
    after = plan.edits[0].after
    assert after.startswith(seed)  # the user's text is kept byte for byte
    data = tomllib.loads(after)
    assert data["mcp_servers"]["switchboard"] == {
        "command": PY,
        "args": ["-I", "-m", "switchboard", "mcp", "--home", HOME],
    }
    assert data["mcp_servers"]["other"]["env"] == {"OTHER_API_TOKEN": "placeholder-not-a-secret"}
    assert data["approval_policy"] == "on-request" and data["features"] == {"daemon_auto_start": True}
    hooks = json.loads(plan.edits[1].after)["hooks"]
    before = json.loads((GOLD / "hooks.seed.json").read_text())["hooks"]
    # the user's groups keep their indices (Codex keys hook trust by index)
    assert hooks["Stop"][0] == before["Stop"][0] and hooks["PostToolUse"][0] == before["PostToolUse"][0]
    assert len(hooks["Stop"]) == 2 and len(hooks["PostToolUse"]) == 2
    assert set(hooks) == {"UserPromptSubmit", "PostToolUse", "Stop", "Interrupt", "SessionEnd"}
    timeouts = {e: hooks[e][-1]["hooks"][0]["timeout"] for e in hooks}
    assert timeouts == {
        "UserPromptSubmit": 10,
        "PostToolUse": 10,
        "Stop": 10,
        "Interrupt": 3,
        "SessionEnd": 3,
    }


def test_diff_never_prints_other_values(tmp_path: Path) -> None:
    uh = seeded(tmp_path, True)
    out = io.StringIO()
    render_plan(codex.plan(uh, PY, HOME, hook_sha12()), out)
    text = out.getvalue()
    assert "placeholder-not-a-secret" not in text and "notify-user" not in text and "user-hook" not in text
    assert "other-server" not in text


def test_reinstall_is_idempotent_and_replaces_only_the_span(tmp_path: Path) -> None:
    uh = seeded(tmp_path, True)
    plan = codex.plan(uh, PY, HOME, hook_sha12())
    for e in plan.edits:
        e.path.write_text(e.after)
    again = codex.plan(uh, PY, HOME, hook_sha12())
    assert not again.changed
    # a new Python path: only the marker span changes; the user's text around it stays
    cfg = uh / ".codex" / "config.toml"
    cfg.write_text(cfg.read_text() + '\n[profiles.fast]\nmodel = "x"\n')
    newer = codex.plan(uh, "/opt/yk/venv2/bin/python", HOME, hook_sha12())
    after = newer.edits[0].after
    assert after.count(codex.BEGIN) == 1 and "/opt/yk/venv2/bin/python" in after and "venv/bin" not in after
    assert after.endswith('[profiles.fast]\nmodel = "x"\n')


def test_an_older_switchboard_hook_is_replaced_in_place(tmp_path: Path) -> None:
    uh = seeded(tmp_path, True)
    plan = codex.plan(uh, PY, HOME, "0" * 12)
    for e in plan.edits:
        e.path.write_text(e.after)
    newer = codex.plan(uh, PY, HOME, hook_sha12())
    hooks = json.loads(newer.edits[1].after)["hooks"]
    assert len(hooks["Stop"]) == 2  # replaced at its index, not appended again
    assert hook_sha12() in hooks["Stop"][1]["hooks"][0]["command"]
    assert "0" * 12 not in newer.edits[1].after


def test_refuses_an_entry_outside_the_markers(tmp_path: Path) -> None:
    uh = seeded(tmp_path, False)
    (uh / ".codex" / "config.toml").write_text('[mcp_servers.switchboard]\ncommand = "x"\n')
    with pytest.raises(InstallError, match="outside switchboard's"):
        codex.plan(uh, PY, HOME, hook_sha12())


def test_refuses_broken_toml_and_markers(tmp_path: Path) -> None:
    uh = seeded(tmp_path, False)
    cfg = uh / ".codex" / "config.toml"
    cfg.write_text("model = [broken")
    with pytest.raises(InstallError, match="not valid TOML"):
        codex.plan(uh, PY, HOME, hook_sha12())
    cfg.write_text(f"{codex.BEGIN}\n[mcp_servers.switchboard]\n")
    with pytest.raises(InstallError, match="markers"):
        codex.plan(uh, PY, HOME, hook_sha12())
    cfg.write_text('mcp_servers = { other = { command = "x" } }\n')  # an inline table can't be extended
    with pytest.raises(InstallError):
        codex.plan(uh, PY, HOME, hook_sha12())


def test_never_writes_trust_approval_or_sandbox(tmp_path: Path) -> None:
    uh = seeded(tmp_path, True)
    plan = codex.plan(uh, PY, HOME, hook_sha12())
    for e in plan.edits:
        added = e.after.replace((GOLD / "config.seed.toml").read_text(), "")
        assert guardrails.find_forbidden(added) == []
        for word in ("trust", "approval_policy", "sandbox", "PermissionRequest", "PreToolUse", "enabled"):
            assert (
                word not in added.replace(codex.BEGIN, "").replace(codex.END, "")
                or word == "trust"
                and "trust them" in added
            )
    assert "PermissionRequest" not in plan.edits[1].after


def test_print_args_writes_nothing(tmp_path: Path, tmp_home: Path) -> None:
    before = sorted(p.name for p in tmp_home.iterdir())
    out = io.StringIO()
    rc = run_install(
        build_parser().parse_args(["install", "codex", "--print-args", "--home", str(tmp_home)]), out=out
    )
    assert rc == 0
    data = json.loads(out.getvalue())
    assert set(data) == {"argv", "env", "files", "notes"}
    assert data["argv"][0] == "-c" and data["argv"][1].startswith("mcp_servers.switchboard={command=")
    assert tomllib.loads(data["files"]["config.toml"])["mcp_servers"]["switchboard"]["args"][-2:] == [
        "--home",
        os.path.realpath(tmp_home),
    ]
    assert set(json.loads(data["files"]["hooks.json"])["hooks"]) == {e for e, _t in codex.EVENTS}
    assert guardrails.find_forbidden(data) == []
    assert sorted(p.name for p in tmp_home.iterdir()) == before


def test_apply_into_a_temp_user_home(tmp_path: Path, tmp_home: Path) -> None:
    uh = seeded(tmp_path, True)
    (uh / ".codex" / "config.toml").chmod(0o644)
    a = build_parser().parse_args(
        ["install", "codex", "--home", str(tmp_home), "--user-home", str(uh), "--yes", "--allow-editable"]
    )
    assert run_install(a, out=io.StringIO()) == 0
    baks = sorted((uh / ".codex").glob("*.bak-switchboard-*"))
    assert len(baks) == 2 and all(oct(b.stat().st_mode & 0o777) == "0o600" for b in baks)
    assert run_install(a, out=(o := io.StringIO())) == 0 and "no changes" in o.getvalue()
    assert tomllib.loads((uh / ".codex" / "config.toml").read_text())["mcp_servers"]["switchboard"]
