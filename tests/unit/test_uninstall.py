"""switchboard uninstall <h|all> and install all (DESIGN.md §9.7): the exact inverse of
install, only switchboard's own entries, idempotency, dry-run, 0600 backups, the
Codex trust-position warning, `claude mcp remove` only for switchboard's entry,
--purge-hooks, and editable installs. Temp user homes only, never the real ~."""

from __future__ import annotations

import copy
import io
import json
import os
import shutil
import stat
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from switchboard import cli, guardrails
from switchboard.cli import build_parser
from switchboard.install import claude, codex, common, cursor, devin
from switchboard.install.common import dump_json, hook_command, run_install, run_uninstall
from switchboard.paths import hook_sha12

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "install"
PY = "/opt/yk/venv/bin/python"
MODS = {"claude": claude, "codex": codex, "cursor": cursor, "devin": devin}
# every user-level file install writes, and its seed fixture
SEEDS: dict[str, dict[str, str]] = {
    "claude": {".claude/settings.json": "claude/settings.seed.json"},
    "codex": {".codex/config.toml": "codex/config.seed.toml", ".codex/hooks.json": "codex/hooks.seed.json"},
    "cursor": {".cursor/mcp.json": "cursor/mcp.seed.json", ".cursor/hooks.json": "cursor/hooks.seed.json"},
    "devin": {".config/devin/mcp_config.json": "devin/mcp_config.seed.json",
              ".config/devin/config.json": "devin/config.seed.json"},
}
ALL_FILES = [rel for files in SEEDS.values() for rel in files]


def real(p: Path) -> str:
    return os.path.realpath(p)


@pytest.fixture(autouse=True)
def _fake_real_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``Path.home()`` (read by --purge-hooks as "the real ~") points at an
    empty temp dir, so no test reads or depends on the machine's real configs."""
    fake = tmp_path / "real-home"
    fake.mkdir()
    monkeypatch.setenv("HOME", str(fake))
    return fake


def seed(uh: Path, harness: str, *, normalize: bool) -> None:
    """The user's own config. ``normalize``: as switchboard's JSON writer formats
    it, so a round trip can be compared byte for byte."""
    for rel, name in SEEDS[harness].items():
        dst = uh / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        text = (FIX / name).read_text()
        if normalize and rel.endswith(".json"):
            text = dump_json(json.loads(text))
        dst.write_text(text)


def snapshot(uh: Path) -> dict[str, bytes]:
    return {str(p.relative_to(uh)): p.read_bytes() for p in sorted(uh.rglob("*")) if p.is_file()}


def configs(uh: Path) -> dict[str, bytes]:
    """The harness config files only (no backups)."""
    return {k: v for k, v in snapshot(uh).items() if ".bak-switchboard-" not in k}


def install(h: str, home: Path, uh: Path, *extra: str) -> tuple[int, str]:
    out = io.StringIO()
    rc = run_install(build_parser().parse_args(["install", h, "--home", str(home), "--user-home", str(uh), "--yes",
                                                "--allow-editable", *extra]), out=out)
    return rc, out.getvalue()


def uninstall(h: str, home: Path, uh: Path, *extra: str, stdin: Any = None) -> tuple[int, str]:
    out = io.StringIO()
    rc = run_uninstall(build_parser().parse_args(["uninstall", h, "--home", str(home), "--user-home", str(uh),
                                                  *extra]), stdin=stdin, out=out)
    return rc, out.getvalue()


# ---- what the user adds after install, around switchboard's entries (same edit on the original)
def _later_json(rel: str, data: dict[str, Any]) -> None:
    if rel == ".claude/settings.json":
        data.setdefault("hooks", {}).setdefault("Stop", []).append(
            {"matcher": "", "hooks": [{"type": "command", "command": "echo later-stop"}]})
        data["theme"] = "dark"
    elif rel == ".codex/hooks.json":
        data["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "echo later-stop"}]})
        data["hooks"].setdefault("SessionEnd", []).append({"hooks": [{"type": "command", "command": "echo bye"}]})
    elif rel == ".cursor/hooks.json":
        data["hooks"]["stop"].append({"command": "echo later-stop"})
    elif rel in (".cursor/mcp.json", ".config/devin/mcp_config.json"):
        data["mcpServers"]["later-server"] = {"command": "node", "args": ["later.js"]}
    elif rel == ".config/devin/config.json":
        data["hooks"]["Stop"].append({"matcher": "", "hooks": [{"type": "command", "command": "echo later-stop"}]})
        data["permissions"]["allow"].append("exec(ls)")
        data["model"] = "later"


def later(rel: str, text: str) -> str:
    if rel.endswith(".toml"):
        return text + '\n[profiles.fast]\nmodel = "x"\n'
    data = json.loads(text)
    _later_json(rel, data)
    return dump_json(data)


# ------------------------------------------------------------ round trips
@pytest.mark.parametrize("harness", list(SEEDS))
def test_round_trip_is_byte_for_byte(tmp_path: Path, tmp_home: Path, harness: str) -> None:
    uh = tmp_path / "uh"
    seed(uh, harness, normalize=True)
    original = configs(uh)
    assert install(harness, tmp_home, uh)[0] == 0
    assert configs(uh) != original
    rc, out = uninstall(harness, tmp_home, uh, "--yes")
    assert rc == 0, out
    assert configs(uh) == original, out


@pytest.mark.parametrize("harness", list(SEEDS))
def test_user_entries_added_after_switchboard_survive_byte_for_byte(tmp_path: Path, tmp_home: Path,
                                                                 harness: str) -> None:
    """The user's entries sit before switchboard's (from the seed) and after them
    (added once installed); uninstall leaves exactly the user's file."""
    uh = tmp_path / "uh"
    seed(uh, harness, normalize=True)
    seeded = configs(uh)
    expected = {rel: later(rel, (uh / rel).read_text()).encode() for rel in SEEDS[harness]}
    assert install(harness, tmp_home, uh)[0] == 0
    for rel in SEEDS[harness]:
        assert (uh / rel).read_bytes() != seeded[rel]  # install put switchboard's entries in every file
        (uh / rel).write_text(later(rel, (uh / rel).read_text()))
    rc, out = uninstall(harness, tmp_home, uh, "--yes")
    assert rc == 0, out
    assert configs(uh) == expected, out


@pytest.mark.parametrize("harness", list(SEEDS))
def test_hand_formatted_seed_round_trips_semantically(tmp_path: Path, tmp_home: Path, harness: str) -> None:
    uh = tmp_path / "uh"
    seed(uh, harness, normalize=False)
    original = configs(uh)
    assert install(harness, tmp_home, uh)[0] == 0
    assert uninstall(harness, tmp_home, uh, "--yes")[0] == 0
    for rel in SEEDS[harness]:
        got, want = (uh / rel).read_bytes(), original[rel]
        if rel.endswith(".toml"):
            assert got == want  # the marker span is the only thing removed
        else:
            assert json.loads(got) == json.loads(want)


@pytest.mark.parametrize("harness", list(SEEDS))
def test_fresh_round_trip_leaves_empty_configs(tmp_path: Path, tmp_home: Path, harness: str) -> None:
    """Files install created are left in place, empty of switchboard (never deleted)."""
    uh = tmp_path / "uh"
    uh.mkdir()
    assert install(harness, tmp_home, uh)[0] == 0
    assert uninstall(harness, tmp_home, uh, "--yes")[0] == 0
    want = {
        ".claude/settings.json": {},
        ".codex/hooks.json": {"hooks": {}},
        ".cursor/mcp.json": {"mcpServers": {}},
        ".cursor/hooks.json": {"version": 1, "hooks": {}},
        ".config/devin/mcp_config.json": {"mcpServers": {}},
        ".config/devin/config.json": {},
    }
    for rel in SEEDS[harness]:
        text = (uh / rel).read_text()
        if rel.endswith(".toml"):
            assert text == ""
        else:
            assert json.loads(text) == want[rel]


# ------------------------------------------------------ safety and UX
@pytest.mark.parametrize("harness", list(SEEDS))
def test_idempotent_nothing_to_remove(tmp_path: Path, tmp_home: Path, harness: str) -> None:
    uh = tmp_path / "uh"
    seed(uh, harness, normalize=False)
    install(harness, tmp_home, uh)
    assert uninstall(harness, tmp_home, uh, "--yes")[0] == 0
    before = snapshot(uh)
    rc, out = uninstall(harness, tmp_home, uh, "--yes")
    assert rc == 0 and out.splitlines()[-1] == "nothing to remove", out
    assert snapshot(uh) == before  # no write, no new backup


def test_clean_home_says_nothing_to_remove_and_creates_nothing(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    uh.mkdir()
    rc, out = uninstall("all", tmp_home, uh, "--yes")
    assert rc == 0
    assert list(uh.iterdir()) == []
    assert out.count("not there, skipped") == len(ALL_FILES)
    assert out.splitlines()[-4:] == ["  claude: nothing to remove", "  codex: nothing to remove",
                                     "  cursor: nothing to remove", "  devin: nothing to remove"]


@pytest.mark.parametrize("harness", list(SEEDS))
def test_dry_run_writes_nothing(tmp_path: Path, tmp_home: Path, harness: str) -> None:
    uh = tmp_path / "uh"
    seed(uh, harness, normalize=False)
    install(harness, tmp_home, uh)
    before = snapshot(uh)
    hooks_before = sorted(os.listdir(tmp_home / "hooks"))
    rc, out = uninstall(harness, tmp_home, uh, "--dry-run", "--purge-hooks")
    assert rc == 0 and "  - " in out
    assert snapshot(uh) == before
    assert sorted(os.listdir(tmp_home / "hooks")) == hooks_before


@pytest.mark.parametrize("harness", list(SEEDS))
def test_backups_are_0600_copies_of_what_was_there(tmp_path: Path, tmp_home: Path, harness: str) -> None:
    uh = tmp_path / "uh"
    seed(uh, harness, normalize=False)
    install(harness, tmp_home, uh)
    for rel in SEEDS[harness]:
        (uh / rel).chmod(0o644)
    installed = configs(uh)
    old = {k for k in snapshot(uh) if ".bak-switchboard-" in k}
    assert uninstall(harness, tmp_home, uh, "--yes")[0] == 0
    new = {k for k in snapshot(uh) if ".bak-switchboard-" in k} - old
    assert len(new) == len(SEEDS[harness])
    for rel in SEEDS[harness]:
        [b] = [uh / k for k in new if k.startswith(rel + ".bak-switchboard-")]
        assert stat.S_IMODE(b.stat().st_mode) == 0o600
        assert b.read_bytes() == installed[rel]
        assert stat.S_IMODE((uh / rel).stat().st_mode) == 0o644  # the file keeps its mode


def test_no_tty_and_no_yes_refuses(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    seed(uh, "claude", normalize=False)
    install("claude", tmp_home, uh)
    before = snapshot(uh)
    rc, out = uninstall("claude", tmp_home, uh, stdin=io.StringIO("y\n"))
    assert rc == 1 and "not applied" in out and snapshot(uh) == before


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_confirmation_prompt_yes_and_no(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    seed(uh, "devin", normalize=False)
    install("devin", tmp_home, uh)
    before = snapshot(uh)
    rc, out = uninstall("devin", tmp_home, uh, stdin=TTY("n\n"))
    assert rc == 1 and "Apply? [y/N]" in out and snapshot(uh) == before
    rc, out = uninstall("devin", tmp_home, uh, stdin=TTY("y\n"))
    assert rc == 0 and "mcp__switchboard__" not in (uh / ".config/devin/config.json").read_text()


def test_editable_install_is_not_refused(tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "editable_install", lambda: True)
    uh = tmp_path / "uh"
    seed(uh, "cursor", normalize=True)
    original = configs(uh)
    install("cursor", tmp_home, uh)  # (--allow-editable)
    rc, out = uninstall("cursor", tmp_home, uh, "--yes")
    assert rc == 0 and "refusing" not in out and configs(uh) == original


def test_diff_shows_only_switchboard_entries_masked(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    for h in SEEDS:
        seed(uh, h, normalize=False)
        install(h, tmp_home, uh)
    rc, out = uninstall("all", tmp_home, uh, "--dry-run")
    assert rc == 0
    for secret in ("placeholder-not-a-secret", "notify-user", "user-hook", "other-server", "git status", "zsh"):
        assert secret not in out
    diff = out.split("\nsummary:\n")[0].splitlines()
    indented = [line for line in diff if line.startswith("  ")]
    assert indented and all(line.startswith(("  - ", "  ! ")) for line in indented)


def test_masking_of_removed_toml_lines() -> None:
    lines = ["[mcp_servers.switchboard]\n", 'command = "/p"\n', 'api_key = "sk-x"\n', "[mcp_servers.switchboard.env]\n",
             'FOO = "bar"\n']
    shown = codex._show_toml(lines)
    assert shown == ["  - [mcp_servers.switchboard]", '  - command = "/p"', "  - api_key = ***",
                     "  - [mcp_servers.switchboard.env]", "  - FOO = ***"]


def test_results_have_no_forbidden_strings(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    for h in SEEDS:
        seed(uh, h, normalize=False)
        install(h, tmp_home, uh)
    home = real(tmp_home)
    for h, mod in MODS.items():
        for e in mod.unplan(uh, home).edits:
            if isinstance(e, common.FileEdit):
                assert guardrails.find_forbidden(e.after) == []
            if isinstance(e, common.CommandEdit):
                assert guardrails.find_forbidden(e.argv) == []


# ------------------------------------------------ only switchboard's entries
def test_older_hook_versions_are_removed_and_other_homes_are_kept(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    home = real(tmp_home)
    old = hook_command(PY, home, "0" * 12, "claude", "Stop")
    other = hook_command(PY, "/opt/other/home", "1" * 12, "claude", "Stop")
    mine = "echo switchboard is great"  # mentions switchboard, isn't a switchboard hook
    data = {"hooks": {"Stop": [
        {"matcher": "", "hooks": [{"type": "command", "command": old}]},
        {"matcher": "", "hooks": [{"type": "command", "command": other}]},
        {"matcher": "", "hooks": [{"type": "command", "command": mine}]},
    ]}}
    (uh / ".claude").mkdir(parents=True)
    (uh / ".claude/settings.json").write_text(dump_json(data))
    plan = claude.unplan(uh, home)
    after = json.loads(plan.edits[0].after)
    assert after == {"hooks": {"Stop": data["hooks"]["Stop"][1:]}}
    assert any("another switchboard home (/opt/other/home)" in n for n in plan.notes)


def test_a_home_whose_path_ends_with_this_one_is_not_ours(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    home = real(tmp_home)
    longer = "/opt" + home  # ".../opt/private/tmp/yk-x" contains "/private/tmp/yk-x/hooks/..."
    theirs = {"hooks": [{"type": "command", "command": hook_command(PY, longer, hook_sha12(), "cursor", "stop")}]}
    (uh / ".codex").mkdir(parents=True)
    (uh / ".codex/hooks.json").write_text(dump_json({"hooks": {"Stop": [theirs]}}))
    plan = codex.unplan(uh, home)
    assert not plan.edits[1].changed
    assert not any("doesn't recognise" in n for n in plan.notes)
    assert any(f"another switchboard home ({longer})" in n for n in plan.notes)
    # install doesn't mistake it for an older switchboard hook either
    again = json.loads(codex.plan(uh, PY, home, hook_sha12()).edits[1].after)["hooks"]["Stop"]
    assert again[0] == theirs and len(again) == 2


def test_a_switchboard_hook_inside_a_user_group_removes_just_that_handler(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    home = real(tmp_home)
    ours = {"type": "command", "command": hook_command(PY, home, hook_sha12(), "devin", "Stop"), "timeout": 30}
    group = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo a"}, ours]}
    (uh / ".config/devin").mkdir(parents=True)
    (uh / ".config/devin/config.json").write_text(dump_json({"hooks": {"Stop": [group]}}))
    plan = devin.unplan(uh, home)
    assert json.loads(plan.edits[1].after) == {"hooks": {"Stop": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "echo a"}]}]}}
    assert "  - hooks.Stop[0].hooks[1]:" in plan.edits[1].display[0]


def test_already_empty_containers_are_left_alone(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    home = real(tmp_home)
    ours = {"matcher": "", "hooks": [{"type": "command",
                                       "command": hook_command(PY, home, hook_sha12(), "claude", "Stop")}]}
    (uh / ".claude").mkdir(parents=True)
    (uh / ".claude/settings.json").write_text(dump_json(
        {"hooks": {"Notification": [], "PreCompact": [{"matcher": "", "hooks": []}], "Stop": [ours]}}))
    after = json.loads(claude.unplan(uh, home).edits[0].after)
    assert after == {"hooks": {"Notification": [], "PreCompact": [{"matcher": "", "hooks": []}]}}


def test_devin_removes_exactly_the_eight_allow_names(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    allow = ["read", "mcp__switchboard__*", *devin.ALLOW, "mcp__other__join", "mcp__switchboard__join"]
    (uh / ".config/devin").mkdir(parents=True)
    (uh / ".config/devin/config.json").write_text(dump_json({"permissions": {"allow": allow, "deny": ["x"]}}))
    after = json.loads(devin.unplan(uh, real(tmp_home)).edits[1].after)
    assert after == {"permissions": {"allow": ["read", "mcp__switchboard__*", "mcp__other__join"], "deny": ["x"]}}
    (uh / ".config/devin/config.json").write_text(dump_json({"permissions": {"allow": list(devin.ALLOW)}}))
    assert json.loads(devin.unplan(uh, real(tmp_home)).edits[1].after) == {}


@pytest.mark.parametrize("harness,rel", [("cursor", ".cursor/mcp.json"), ("devin", ".config/devin/mcp_config.json")])
def test_mcp_entry_removed_only_if_it_is_switchboard_for_this_home(tmp_path: Path, tmp_home: Path, harness: str,
                                                                rel: str) -> None:
    uh = tmp_path / "uh"
    home = real(tmp_home)
    (uh / rel).parent.mkdir(parents=True)
    cases = {
        "ours-old-python": ({"command": "/old/python", "args": ["-I", "-m", "switchboard", "mcp", "--home", home]}, True),
        "other-home": ({"command": PY, "args": ["-I", "-m", "switchboard", "mcp", "--home", "/opt/other"]}, False),
        "not-switchboard": ({"command": "npx", "args": ["-y", "switchboard-lookalike"]}, False),
    }
    for name, (entry, removed) in cases.items():
        (uh / rel).write_text(dump_json({"mcpServers": {"a": {"command": "x"}, "switchboard": entry}}))
        plan = MODS[harness].unplan(uh, home)
        after = json.loads(plan.edits[0].after)
        assert ("switchboard" not in after["mcpServers"]) is removed, name
        assert after["mcpServers"]["a"] == {"command": "x"}
        if not removed:
            assert plan.edits[0].after == (uh / rel).read_text() and plan.notes, name


# ------------------------------------------------------------------- codex
def codex_group(home: str, event: str, sha: str | None = None) -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": hook_command(PY, home, sha or hook_sha12(), "codex", event),
                       "timeout": 10}]}


def user_group(cmd: str) -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": cmd}]}


def test_codex_warns_about_user_groups_that_move_up(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    home = real(tmp_home)
    (uh / ".codex").mkdir(parents=True)
    hooks = {"Stop": [user_group("echo a"), codex_group(home, "Stop"), user_group("echo b"), user_group("echo c")],
             "PostToolUse": [user_group("echo d"), codex_group(home, "PostToolUse")],
             "SessionEnd": [{"hooks": [{"type": "command", "command": "echo e"},
                                       codex_group(home, "SessionEnd")["hooks"][0],
                                       {"type": "command", "command": "echo f"}]}]}
    (uh / ".codex/hooks.json").write_text(dump_json({"hooks": hooks}))
    plan = codex.unplan(uh, home)
    lines = plan.edits[1].display
    assert "  ! hooks.Stop[2] (not switchboard's) moves to hooks.Stop[1]" in lines
    assert "  ! hooks.Stop[3] (not switchboard's) moves to hooks.Stop[2]" in lines
    assert "  ! hooks.SessionEnd[0].hooks[2] (not switchboard's) moves to hooks.SessionEnd[0].hooks[1]" in lines
    assert not any("PostToolUse" in line and "!" in line for line in lines)  # switchboard's was last: nothing moves
    assert "hooks.Stop[0]" not in "".join(lines)
    assert any("marked ! move up (3)" in n and "/hooks" in n for n in plan.notes)
    after = json.loads(plan.edits[1].after)["hooks"]
    assert after["Stop"] == [user_group("echo a"), user_group("echo b"), user_group("echo c")]
    assert after["SessionEnd"][0]["hooks"] == [{"type": "command", "command": "echo e"},
                                               {"type": "command", "command": "echo f"}]
    for text in ("echo a", "echo b", "echo e"):
        assert text not in "\n".join(lines)  # the user's hooks are named by position only


def test_codex_no_warning_when_switchboard_groups_are_last(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    seed(uh, "codex", normalize=False)
    install("codex", tmp_home, uh)
    plan = codex.unplan(uh, real(tmp_home))
    assert not any(line.startswith("  !") for line in plan.edits[1].display)
    assert not any("move up" in n for n in plan.notes)
    assert any("trust records" in n for n in plan.notes)


def test_codex_keeps_a_table_codex_appended_inside_the_markers(tmp_path: Path, tmp_home: Path) -> None:
    """Codex may append its own tables (e.g. hook trust) after switchboard's table,
    before the end marker; uninstall and re-install keep them."""
    uh = tmp_path / "uh"
    seed(uh, "codex", normalize=False)
    install("codex", tmp_home, uh)
    cfg = uh / ".codex/config.toml"
    foreign = '[hooks.state."/x/hooks.json:stop:1:0"]\ntrusted_hash = "sha256:abc"\n'
    cfg.write_text(cfg.read_text().replace(codex.END, foreign + codex.END))
    # a re-install keeps it too
    again = codex.plan(uh, sys.executable, real(tmp_home), hook_sha12())
    assert foreign in again.edits[0].after
    plan = codex.unplan(uh, real(tmp_home))
    after = plan.edits[0].after
    assert foreign in after and codex.BEGIN not in after and "mcp_servers.switchboard" not in after
    want = tomllib.loads((FIX / "codex/config.seed.toml").read_text())
    want["hooks"] = {"state": {"/x/hooks.json:stop:1:0": {"trusted_hash": "sha256:abc"}}}
    assert tomllib.loads(after) == want
    assert "trusted_hash" not in "\n".join(plan.edits[0].display)


def test_codex_leaves_other_homes_and_outside_entries(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    (uh / ".codex").mkdir(parents=True)
    cfg = uh / ".codex/config.toml"
    cfg.write_text(codex.toml_block(PY, "/opt/other/home"))
    plan = codex.unplan(uh, real(tmp_home))
    assert not plan.edits[0].changed and any("another switchboard home (/opt/other/home)" in n for n in plan.notes)
    cfg.write_text('[mcp_servers.switchboard]\ncommand = "x"\n')
    plan = codex.unplan(uh, real(tmp_home))
    assert not plan.edits[0].changed and any("outside switchboard's markers" in n for n in plan.notes)


def test_codex_refuses_broken_markers_or_toml(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    (uh / ".codex").mkdir(parents=True)
    cfg = uh / ".codex/config.toml"
    for bad in (f"{codex.BEGIN}\n[mcp_servers.switchboard]\n", "model = [broken"):
        cfg.write_text(bad)
        before = snapshot(uh)
        rc, _ = uninstall("codex", tmp_home, uh, "--yes")
        assert rc == 1 and snapshot(uh) == before


def test_codex_block_in_the_middle_keeps_what_follows(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    (uh / ".codex").mkdir(parents=True)
    head, tail = 'model = "m"\n', '\n[tui]\nx = 1\n'
    (uh / ".codex/config.toml").write_text(head + "\n" + codex.toml_block(PY, real(tmp_home)) + tail)
    assert codex.unplan(uh, real(tmp_home)).edits[0].after == head + tail


# ------------------------------------------------------ claude mcp remove
@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[int], Path]:
    """A fake `claude` first on PATH that logs its argv (never the real CLI)."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    log = tmp_path / "claude.log"

    def make(rc: int = 0) -> Path:
        script = bindir / "claude"
        script.write_text(f'#!/bin/sh\nprintf "%s " "$@" >> "{log}"\necho >> "{log}"\nexit {rc}\n')
        script.chmod(0o755)
        return log

    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return make


def claude_json(uh: Path, entry: dict[str, Any] | None) -> None:
    servers: dict[str, Any] = {"other": {"command": "x", "env": {"K": "v"}}}
    if entry is not None:
        servers["switchboard"] = entry
    (uh / ".claude.json").write_text(json.dumps({"numStartups": 3, "mcpServers": servers}))


def test_claude_mcp_remove_runs_for_switchboard_entry_only(tmp_path: Path, tmp_home: Path,
                                                       fake_claude: Callable[[int], Path]) -> None:
    log = fake_claude(0)
    home = real(tmp_home)
    cases = {
        "ours": (claude.mcp_entry("/old/venv/bin/python", home), True),
        "other-home": (claude.mcp_entry(PY, "/opt/other/home"), False),
        "not-switchboard": ({"type": "stdio", "command": "npx", "args": ["-y", "some-server"]}, False),
        "absent": (None, False),
    }
    for name, (entry, runs) in cases.items():
        uh = tmp_path / f"uh-{name}"
        uh.mkdir()
        seed(uh, "claude", normalize=False)
        install("claude", tmp_home, uh)
        claude_json(uh, entry)
        dot = (uh / ".claude.json").read_bytes()
        if log.exists():
            log.unlink()
        plan = claude.unplan(uh, home, run_commands=True)
        assert common.apply_plan(plan, run_commands=True, out=io.StringIO()) == 0
        if runs:
            assert log.read_text().split("\n")[0].split() == ["mcp", "remove", "--scope", "user", "switchboard"], name
        else:
            assert not log.exists(), name
            if entry is not None:
                assert plan.notes and "left alone" in plan.notes[-1], name
        assert (uh / ".claude.json").read_bytes() == dot  # switchboard itself never writes ~/.claude.json
        assert "switchboard_hook-" not in (uh / ".claude/settings.json").read_text()


def test_claude_mcp_remove_failure_prints_the_command(tmp_path: Path, tmp_home: Path, capsys: pytest.CaptureFixture[str],
                                                      fake_claude: Callable[[int], Path]) -> None:
    fake_claude(1)
    uh = tmp_path / "uh"
    seed(uh, "claude", normalize=False)
    install("claude", tmp_home, uh)
    claude_json(uh, claude.mcp_entry(PY, real(tmp_home)))
    plan = claude.unplan(uh, real(tmp_home), run_commands=True)
    assert common.apply_plan(plan, run_commands=True, out=io.StringIO()) == 1
    err = capsys.readouterr().err
    assert "Run it yourself" in err and "claude mcp remove --scope user switchboard" in err
    assert "switchboard_hook-" not in (uh / ".claude/settings.json").read_text()  # the file edit still landed


def test_user_home_never_runs_claude(tmp_path: Path, tmp_home: Path, fake_claude: Callable[[int], Path]) -> None:
    log = fake_claude(0)
    uh = tmp_path / "uh"
    seed(uh, "claude", normalize=False)
    install("claude", tmp_home, uh)
    claude_json(uh, claude.mcp_entry(PY, real(tmp_home)))
    rc, out = uninstall("claude", tmp_home, uh, "--yes")
    assert rc == 0 and "skipped (--user-home): claude mcp remove --scope user switchboard" in out
    assert "$ claude mcp remove --scope user switchboard   # not run with --user-home" in out
    assert not log.exists()


# ------------------------------------------------------------------- all
def fake_clis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: list[str]) -> None:
    bindir = tmp_path / "fakebin-all"
    bindir.mkdir(exist_ok=True)
    for n in names:
        (bindir / n).write_text("#!/bin/sh\nexit 97\n")
        (bindir / n).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin")


def test_install_all_skips_missing_clis_and_uninstall_all_reverses_it(tmp_path: Path, tmp_home: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    fake_clis(tmp_path, monkeypatch, ["claude", "codex", "devin"])  # no cursor-agent / agent
    uh = tmp_path / "uh"
    for h in SEEDS:
        seed(uh, h, normalize=True)
    original = configs(uh)
    rc, out = install("all", tmp_home, uh)
    assert rc == 0, out
    assert "switchboard install cursor: skipped (`cursor-agent` or `agent` not on PATH)" in out
    assert out.count("Apply? [y/N]") == 0  # --yes
    assert out.splitlines()[-5:] == [
        "summary:",
        "  claude: installed (files only: harness commands aren't run with --user-home)",
        "  codex: installed", "  cursor: skipped (`cursor-agent` or `agent` not on PATH)", "  devin: installed"]
    assert configs(uh)[".cursor/hooks.json"] == original[".cursor/hooks.json"]
    for rel in (".claude/settings.json", ".codex/hooks.json", ".config/devin/config.json"):
        assert "switchboard_hook-" in (uh / rel).read_text()
    rc, out = uninstall("all", tmp_home, uh, "--yes")
    assert rc == 0, out
    assert configs(uh) == original
    assert out.splitlines()[-5:] == ["summary:", "  claude: removed", "  codex: removed",
                                     "  cursor: nothing to remove", "  devin: removed"]
    assert out.index("switchboard uninstall claude:") < out.index("switchboard uninstall codex:") \
        < out.index("switchboard uninstall cursor:") < out.index("switchboard uninstall devin:")
    rc, out = install("all", tmp_home, uh, "--dry-run")
    assert rc == 0 and "  claude: would change" in out and configs(uh) == original


def test_all_has_one_confirmation(tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_clis(tmp_path, monkeypatch, ["claude", "codex", "cursor-agent", "devin"])
    uh = tmp_path / "uh"
    for h in SEEDS:
        seed(uh, h, normalize=False)
    out = io.StringIO()
    rc = run_install(build_parser().parse_args(["install", "all", "--home", str(tmp_home), "--user-home", str(uh),
                                                "--allow-editable"]), stdin=TTY("y\n"), out=out)
    assert rc == 0 and out.getvalue().count("Apply? [y/N]") == 1
    assert all("switchboard_hook-" in (uh / SEEDS_HOOKS).read_text() for SEEDS_HOOKS in
               (".claude/settings.json", ".codex/hooks.json", ".cursor/hooks.json", ".config/devin/config.json"))
    rc, text = uninstall("all", tmp_home, uh, stdin=TTY("y\n"))
    assert rc == 0 and text.count("Apply? [y/N]") == 1
    assert not any("switchboard_hook-" in v.decode() for v in configs(uh).values())


def test_all_goes_on_past_a_broken_harness_and_exits_1(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    for h in SEEDS:
        seed(uh, h, normalize=False)
        install(h, tmp_home, uh)
    (uh / ".cursor/mcp.json").write_text("{not json")
    rc, out = uninstall("all", tmp_home, uh, "--yes")
    assert rc == 1
    assert "switchboard uninstall cursor: error:" in out and "not valid JSON" in out
    assert "  cursor: error:" in out and "  devin: removed" in out
    assert "switchboard_hook-" in (uh / ".cursor/hooks.json").read_text()  # the broken harness is untouched
    assert "switchboard_hook-" not in (uh / ".config/devin/config.json").read_text()


def test_install_all_print_args_is_refused(tmp_home: Path) -> None:
    out = io.StringIO()
    rc = run_install(build_parser().parse_args(["install", "all", "--print-args", "--home", str(tmp_home)]), out=out)
    assert rc == 1 and out.getvalue() == ""


def test_install_all_is_refused_on_editable_without_the_flag(tmp_path: Path, tmp_home: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "editable_install", lambda: True)
    fake_clis(tmp_path, monkeypatch, ["claude", "codex", "cursor-agent", "devin"])
    uh = tmp_path / "uh"
    uh.mkdir()
    out = io.StringIO()
    rc = run_install(build_parser().parse_args(["install", "all", "--home", str(tmp_home), "--user-home", str(uh),
                                                "--yes"]), out=out)
    assert rc == 1 and list(uh.iterdir()) == []


def test_cli_main_wires_uninstall(tmp_path: Path, tmp_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    uh = tmp_path / "uh"
    seed(uh, "cursor", normalize=False)
    install("cursor", tmp_home, uh)
    assert cli.main(["uninstall", "cursor", "--home", str(tmp_home), "--user-home", str(uh), "--dry-run"]) == 0
    assert "switchboard uninstall cursor:" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["uninstall", "vim", "--user-home", str(uh)])


# ------------------------------------------------------------ --purge-hooks
def test_purge_hooks_only_when_nothing_uses_them(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    for h in ("claude", "codex"):
        seed(uh, h, normalize=False)
        install(h, tmp_home, uh)
    hooks = tmp_home / "hooks"
    (hooks / "notes.txt").write_text("keep me")
    (hooks / f"switchboard_hook-{'0' * 12}.py").write_text("# an older copy\n")
    copies = sorted(p.name for p in hooks.glob("switchboard_hook-*.py"))
    assert len(copies) == 2
    rc, out = uninstall("claude", tmp_home, uh, "--yes", "--purge-hooks")
    assert rc == 0 and "kept (still used by ~/.codex/hooks.json)" in out
    assert sorted(p.name for p in hooks.glob("switchboard_hook-*.py")) == copies
    rc, out = uninstall("codex", tmp_home, uh, "--yes", "--purge-hooks")
    assert rc == 0, out
    assert "(delete 2 hook copies):" in out and "deleted 2 hook copies" in out
    assert sorted(os.listdir(hooks)) == ["notes.txt"]
    rc, out = uninstall("all", tmp_home, uh, "--yes", "--purge-hooks")
    assert rc == 0 and "no hook copies" in out


def test_without_purge_the_copies_stay_and_the_note_says_so(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    seed(uh, "devin", normalize=False)
    install("devin", tmp_home, uh)
    rc, out = uninstall("devin", tmp_home, uh, "--yes")
    assert rc == 0 and "--purge-hooks" in out and "stay" in out
    assert list((tmp_home / "hooks").glob("switchboard_hook-*.py"))


def test_purge_refuses_a_symlinked_hooks_dir(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    uh.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / f"switchboard_hook-{'a' * 12}.py").write_text("x")
    shutil.rmtree(tmp_home / "hooks", ignore_errors=True)
    (tmp_home / "hooks").symlink_to(target)
    rc, out = uninstall("all", tmp_home, uh, "--yes", "--purge-hooks")
    assert rc == 0 and "not a directory you own" in out
    assert (target / f"switchboard_hook-{'a' * 12}.py").exists()


def test_unplan_does_not_mutate_or_touch_the_switchboard_home(tmp_path: Path, tmp_home: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    uh = tmp_path / "uh"
    for h in SEEDS:
        seed(uh, h, normalize=False)
        install(h, tmp_home, uh)
    home = real(tmp_home)
    installed = configs(uh)
    yk_before = {str(p.relative_to(tmp_home)): p.read_bytes() for p in sorted(tmp_home.rglob("*")) if p.is_file()}
    assert any(k.startswith("hooks/switchboard_hook-") for k in yk_before)
    loaded: list[tuple[dict[str, Any], dict[str, Any]]] = []
    real_load = common.load_json_obj

    def spy(path: Path) -> tuple[str | None, dict[str, Any]]:
        text, data = real_load(path)
        loaded.append((data, copy.deepcopy(data)))
        return text, data

    monkeypatch.setattr(common, "load_json_obj", spy)
    for mod in MODS.values():
        assert mod.unplan(uh, home).changed
    assert loaded and all(data == kept for data, kept in loaded)  # planning edits copies only
    assert configs(uh) == installed  # and writes nothing
    monkeypatch.setattr(common, "load_json_obj", real_load)
    assert uninstall("all", tmp_home, uh, "--yes")[0] == 0
    assert configs(uh) != installed
    yk_after = {str(p.relative_to(tmp_home)): p.read_bytes() for p in sorted(tmp_home.rglob("*")) if p.is_file()}
    assert yk_after == yk_before  # the switchboard home, hook copies included, is untouched


# ------------------------------------------------------- review fixes
def test_devin_allow_names_stay_while_another_home_uses_them(tmp_path: Path, tmp_home: Path) -> None:
    """The eight names serve every switchboard home: uninstalling home A must not
    strip them from home B's still-installed Devin setup."""
    uh = tmp_path / "uh"
    seed(uh, "devin", normalize=True)
    original = configs(uh)
    other = tmp_path / "home-b"
    other.mkdir(mode=0o700)
    assert install("devin", tmp_home, uh)[0] == 0
    assert install("devin", other, uh)[0] == 0  # B's install now owns mcpServers.switchboard
    rc, out = uninstall("devin", tmp_home, uh, "--yes")
    assert rc == 0, out
    cfg = json.loads((uh / ".config/devin/config.json").read_text())
    assert set(devin.ALLOW) <= set(cfg["permissions"]["allow"])
    assert real(other) in json.dumps(cfg["hooks"]) and real(tmp_home) not in json.dumps(cfg["hooks"])
    assert "eight allow names stay" in out and real(other) in out
    assert common.mcp_home(json.loads((uh / ".config/devin/mcp_config.json").read_text())
                           ["mcpServers"]["switchboard"]) == real(other)
    # a home that never installed anything has nothing to remove
    unused = tmp_path / "home-unused"
    unused.mkdir(mode=0o700)
    rc, out = uninstall("devin", unused, uh, "--dry-run")
    assert rc == 0 and "- permissions.allow" not in out and out.rstrip().endswith("nothing to remove")
    rc, out = uninstall("all", unused, uh, "--dry-run")
    assert "  devin: nothing to remove" in out
    # the last home out takes the names with it, and the files are the user's again
    rc, out = uninstall("devin", other, uh, "--yes")
    assert rc == 0 and "- permissions.allow" in out
    assert configs(uh) == original


def test_devin_install_notes_the_pre_approval(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    uh.mkdir()
    rc, out = install("devin", tmp_home, uh, "--dry-run")
    assert rc == 0 and "pre-approves switchboard's eight tools" in out and "without an approval prompt" in out
    assert install("devin", tmp_home, uh)[0] == 0
    assert not any("pre-approves" in n for n in devin.plan(uh, PY, real(tmp_home), hook_sha12()).notes)


def test_removed_mcp_entries_show_only_command_and_args(tmp_path: Path, tmp_home: Path) -> None:
    """The user may have added env or headers to switchboard's entry: only command
    and args are printed (a value after a secret-looking flag masked)."""
    uh = tmp_path / "uh"
    home = real(tmp_home)
    entry = {"command": PY, "args": ["-I", "-m", "switchboard", "mcp", "--home", home, "--api-key", "sk-ARG",
                                     "--token=sk-EQ"],
             "env": {"GITHUB_PAT": "ghp_SECRET", "DB": "postgres://u:pw@h/db"}, "headers": {"X-Foo": "bar-SECRET"},
             "cwd": "/srv/private-SECRET"}
    for rel in (".cursor/mcp.json", ".config/devin/mcp_config.json"):
        (uh / rel).parent.mkdir(parents=True, exist_ok=True)
        (uh / rel).write_text(dump_json({"mcpServers": {"switchboard": entry}}))
    (uh / ".cursor/hooks.json").write_text(dump_json({"version": 1, "hooks": {"stop": [
        {"command": hook_command(PY, home, hook_sha12(), "cursor", "stop"), "env": {"X": "SECRET"}, "note": "SECRET"}]}}))
    rc, out = uninstall("all", tmp_home, uh, "--dry-run")
    assert rc == 0 and "SECRET" not in out and "sk-" not in out and "pw@" not in out
    assert '"--api-key", "***", "--token=***"], "env": "***", "headers": "***", "cwd": "***"}' in out
    assert '"env": "***", "note": "***"' in out


def test_removed_toml_lines_are_masked_by_allowlist() -> None:
    lines = [f"{codex.BEGIN}\n", "# Added by `switchboard install codex`; this span is replaced on re-install.\n",
             "[mcp_servers.switchboard]\n", 'command = "/p"\n', 'args = ["-I", "--api-key", "sk-1", "--token=sk-2"]\n',
             'http_headers = { Authorization = "Bearer sk-3" }\n', "extra = [\n", '  "--db", "postgres://u:sk-4@h",\n',
             "]\n", "# my token: sk-5\n", "[mcp_servers.switchboard.env]\n", 'A = "sk-6"\n', f"{codex.END}\n"]
    shown = codex._show_toml(lines)
    assert "sk-" not in "\n".join(shown)
    assert shown == [f"  - {codex.BEGIN}", "  - # Added by `switchboard install codex`; this span is replaced on re-install.",
                     "  - [mcp_servers.switchboard]", '  - command = "/p"',
                     '  - args = ["-I", "--api-key", "***", "--token=***"]', "  - http_headers = ***",
                     "  - extra = ***", "  -   ***", "  -   ***", "  - # ***", "  - [mcp_servers.switchboard.env]",
                     "  - A = ***", f"  - {codex.END}"]


@pytest.mark.parametrize("harness,rel", [("cursor", ".cursor/mcp.json"), ("devin", ".config/devin/mcp_config.json"),
                                         ("claude", ".claude.json"), ("codex", ".codex/config.toml")])
def test_a_foreign_home_is_escaped_and_quoted_in_notes(tmp_path: Path, tmp_home: Path, harness: str, rel: str) -> None:
    """A --home read from a config file can't inject terminal escapes or shell
    syntax into the printed "run this yourself" command."""
    uh = tmp_path / "uh"
    (uh / rel).parent.mkdir(parents=True, exist_ok=True)
    evil = "/tmp/x; touch /tmp/PWNED #\x1b[2K\rnothing to remove"
    entry = {"command": PY, "args": ["-I", "-m", "switchboard", "mcp", "--home", evil]}
    if harness == "codex":
        (uh / rel).write_text(codex.toml_block(PY, "/tmp/PLACEHOLDER").replace('"/tmp/PLACEHOLDER"',
                                                                             json.dumps(evil)))
    elif harness == "claude":
        claude_json(uh, entry)
    else:
        (uh / rel).write_text(dump_json({"mcpServers": {"switchboard": entry}}))
    rc, out = uninstall(harness, tmp_home, uh, "--dry-run")
    assert rc == 0, out
    assert "\x1b" not in out and "\r" not in out
    assert "\\x1b[2K\\rnothing to remove" in out
    assert f"switchboard uninstall {harness} --home DIR" in out
    mild = "/tmp/a b;c"
    if harness == "codex":
        (uh / rel).write_text(codex.toml_block(PY, "/tmp/PLACEHOLDER").replace("/tmp/PLACEHOLDER", mild))
    elif harness == "claude":
        claude_json(uh, {**entry, "args": entry["args"][:-1] + [mild]})
    else:
        (uh / rel).write_text(dump_json({"mcpServers": {"switchboard": {**entry, "args": entry["args"][:-1] + [mild]}}}))
    rc, out = uninstall(harness, tmp_home, uh, "--dry-run")
    assert f"switchboard uninstall {harness} --home '/tmp/a b;c'` for it" in out


def test_safe_text_escapes_non_printables(tmp_path: Path, tmp_home: Path) -> None:
    assert common.safe_text("a\x1b[2Kb\rc\x9bd") == "a\\x1b[2Kb\\rc\\x9bd"
    assert common.compact({"k2": "a\x9bb"}) == '{"k2": "a\\x9bb"}'


def test_install_all_goes_on_past_a_broken_harness(tmp_path: Path, tmp_home: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    fake_clis(tmp_path, monkeypatch, ["claude", "codex", "cursor-agent", "devin"])
    uh = tmp_path / "uh"
    for h in SEEDS:
        seed(uh, h, normalize=False)
    (uh / ".codex/hooks.json").write_text("{not json")
    before = (uh / ".codex/config.toml").read_bytes()
    rc, out = install("all", tmp_home, uh)
    assert rc == 1
    assert "switchboard install codex: error:" in out and "  codex: error:" in out
    assert "  claude: installed" in out and "  cursor: installed" in out and "  devin: installed" in out
    assert (uh / ".codex/config.toml").read_bytes() == before  # the broken harness is untouched
    assert "switchboard_hook-" in (uh / ".cursor/hooks.json").read_text()


def test_summary_says_why_hook_copies_are_kept(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    for h in ("claude", "codex"):
        seed(uh, h, normalize=False)
        install(h, tmp_home, uh)
    rc, out = uninstall("claude", tmp_home, uh, "--dry-run", "--purge-hooks")
    assert "  hook copies: kept (still used by ~/.codex/hooks.json)" in out.split("summary:")[1]
    rc, out = uninstall("claude", tmp_home, uh, "--yes", "--purge-hooks")
    assert rc == 0 and "  hook copies: kept (still used by ~/.codex/hooks.json)" in out.split("summary:")[1]
    assert list((tmp_home / "hooks").glob("switchboard_hook-*.py"))


def test_summary_when_only_a_harness_command_failed(tmp_path: Path, tmp_home: Path,
                                                    fake_claude: Callable[[int], Path]) -> None:
    fake_claude(1)
    uh = tmp_path / "uh"
    seed(uh, "claude", normalize=False)  # no switchboard hooks: only `claude mcp remove` to do
    claude_json(uh, claude.mcp_entry(PY, real(tmp_home)))
    plan = claude.unplan(uh, real(tmp_home), run_commands=True)
    run = common._Run("claude", plan=plan)
    run.rc = common.apply_plan(plan, run_commands=True, out=io.StringIO())
    assert run.rc == 1
    out = io.StringIO()
    common._summary([run], "uninstall", applied=True, out=out)
    assert out.getvalue() == "summary:\n  claude: a harness command failed (see above)\n"


def test_purge_checks_the_real_home_too(tmp_path: Path, tmp_home: Path, _fake_real_home: Path) -> None:
    """--user-home doesn't move the switchboard home: the copies the real ~'s
    configs run must survive `uninstall --user-home X --purge-hooks`."""
    assert install("claude", tmp_home, _fake_real_home)[0] == 0  # "the real ~" runs tmp_home's copies
    copies = sorted(p.name for p in (tmp_home / "hooks").glob("switchboard_hook-*.py"))
    assert copies
    other = tmp_path / "other-uh"
    other.mkdir()
    rc, out = uninstall("all", tmp_home, other, "--yes", "--purge-hooks")
    assert rc == 0, out
    assert f"kept (still used by {_fake_real_home / '.claude/settings.json'} (your real ~))" in out
    assert sorted(p.name for p in (tmp_home / "hooks").glob("switchboard_hook-*.py")) == copies
    # the same home as the real ~ is checked once, from the planned edits
    rc, out = uninstall("all", tmp_home, _fake_real_home, "--yes", "--purge-hooks")
    assert rc == 0 and "deleted" in out and not list((tmp_home / "hooks").glob("switchboard_hook-*.py"))


def test_codex_reinstall_shows_foreign_lines_moving(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    seed(uh, "codex", normalize=False)
    install("codex", tmp_home, uh)
    cfg = uh / ".codex/config.toml"
    foreign = '[hooks.state."/x:stop:1:0"]\ntrusted_hash = "sha256:1"\n'
    cfg.write_text(cfg.read_text().replace(codex.END, foreign + codex.END))
    plan = codex.plan(uh, sys.executable, real(tmp_home), hook_sha12())
    assert plan.edits[0].display == [f"  ~ 2 line(s) between the markers that aren't switchboard's move after"
                                     f" {codex.END!r}"]
    assert plan.edits[0].after.index(codex.END) < plan.edits[0].after.index(foreign)
