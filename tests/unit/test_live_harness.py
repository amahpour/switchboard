"""The live tests' driver (tests/live/harness), without a live CLI (DESIGN.md §12.4).

The live tests drive real agent CLIs and run only when asked (``SWITCHBOARD_LIVE=...``). What they hand each
CLI is checked here on every run: the screens they read, the config they write, the flags they launch with
(never an approval switched off), and that a plain ``pytest`` skips every one of them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

LIVE = Path(__file__).resolve().parents[1] / "live"
REPO = LIVE.parents[1]
sys.path.insert(0, str(LIVE))

from harness import (  # noqa: E402
    claude,
    claude_signin,
    codex,
    cursor,
    devin,
    drift,
    preflight,
    scenarios,
    screens,
    world,
)

# ------------------------------------------------------------------- screens
DEVIN_BANNER = " Free plan, use /upgrade to access better models · {} remaining (resets in 3d 7h)\n"


def test_a_quota_is_exhausted_only_at_zero_or_by_its_message() -> None:
    """Fails if 10%, 50% or 100% left reads as used up (the run would skip a working account), or if a
    usage-limit message is not recognised (the run would wait out every timeout)."""
    for pct in ("10%", "20%", "50%", "76%", "100%", "0.5%"):
        assert not screens.quota_exhausted(DEVIN_BANNER.format(pct)), pct
    assert screens.quota_left(DEVIN_BANNER.format("76%")) == 76.0
    assert screens.quota_exhausted(DEVIN_BANNER.format("0%"))
    assert screens.quota_exhausted("ActionRequiredError: You've hit your usage limit Get Cursor Pro")
    assert screens.quota_exhausted("usage limit reached for this week")
    assert screens.quota_left("Ask Devin to build features") is None


@pytest.mark.parametrize(
    ("harness", "prompt"),
    [
        ("claude", "This command requires approval\n Do you want to proceed?\n ❯ 1. Yes\n   4. No\n"),
        ("codex", "Would you like to run the following command?\n  1. Yes, proceed (y)\n"),
        ("devin", "Run `git log`?\n❭ 1 Yes  (Approve once)\n↵ confirm · esc cancel\n"),
        # recorded from Cursor Agent 2026.10.18 asking about switchboard's own tool, 2026-10-22 (#394)
        (
            "cursor",
            'switchboard-https: switchboard_join\n  {\n    "room": "#build"\n  }\n Run this MCP tool?\n'
            " → Run (once) (y)\n   Allowlist MCP Tool (tab)\n   Reject & propose changes (p)\n"
            "   Skip (esc or n)\n",
        ),
    ],
)
def test_an_approval_prompt_is_recognised_so_the_driver_declines_it(harness: str, prompt: str) -> None:
    """Fails if a prompt goes unseen: the driver would type into it or wait on it, and a prompt is only ever
    declined with Esc."""
    assert screens.approval_open(harness, prompt)
    assert not screens.approval_open(harness, "● Posted pong to #build\n❯ \n  ⏸ manual mode on")


def test_claudes_start_up_dialogs_get_only_the_answers_this_run_owns() -> None:
    """The folder-trust dialog (Down from "No, exit" first, then Enter) and the Chrome offer (Esc) are
    answered; a bypass-mode warning never is. Fails if Enter is pressed on "No, exit"."""
    no_selected = " ❯ No, exit\n   Yes, I trust this folder\n Enter to confirm"
    yes_selected = "   No, exit\n ❯ Yes, I trust this folder\n Enter to confirm"
    assert screens.claude_dialog(no_selected) == "Down"
    assert screens.claude_dialog(yes_selected) == "Enter"
    assert screens.claude_dialog("Try Claude in Chrome") == "Escape"
    assert screens.claude_dialog("1. No\n2. Yes, I accept") == "FAIL"
    assert screens.claude_dialog("● joined") is None
    assert not screens.claude_ready(no_selected)
    assert screens.claude_ready("  ⏸ manual mode on · ? for shortcuts")


def test_what_the_screens_say_about_agents_and_waits() -> None:
    assert screens.claude_agent_finished('● Agent "Run echo and hold script" finished · 4s')
    assert not screens.claude_agent_finished("● Agent(Run echo and hold script)\n  Backgrounded agent")
    assert screens.codex_ready("  GPT-6-Luna low\n› Ask Codex to do anything")
    assert not screens.codex_ready("Do you trust the contents of this directory?\n› 1. Yes")
    assert screens.codex_tokens("Token usage: total=16,642 input=16,000") == 16642
    running = (
        " ⏺ mcp__switchboard_https__wait(timeout_s=100)\n⠸⠀ Running tools · 25s (esc twice to interrupt)"
    )
    assert screens.devin_waiting(running)
    assert not screens.devin_waiting(
        " ⏺ Called say from switchboard_https\n joined\n❭ Ask Devin to build features"
    )
    assert screens.devin_ready("❭ Ask Devin to build features, fix bugs")
    assert screens.claude_resume_id(
        "Resume this session with:\nclaude --resume 45c684f8-3007-426d-9dd9-5d40a3d9f2a3"
    )


def test_what_is_in_claudes_input_box_tells_a_sent_prompt_from_an_unsent_one() -> None:
    """After Enter the prompt has to be gone from the box (it is echoed above it). Fails if the driver
    reads the echo as an unsent prompt, or an unsent one as sent: the prompt would be lost or typed twice."""
    bar = "─" * 60
    unsent = (
        f"● earlier\n{bar}\n❯ Use the switchboard tools. Call join\n  with room\n{bar}\n  ⏸ manual mode on"
    )
    sent = f"❯ Use the switchboard tools. Call join\n● joined\n{bar}\n❯ \n{bar}\n  ⏸ manual mode on"
    assert "Use the switchboard tools. Call join" in screens.claude_input_text(unsent)
    assert "Use the switchboard tools" not in screens.claude_input_text(sent)
    assert screens.claude_input_text("no box here") == ""


def test_the_live_input_line_is_the_last_one_not_the_echo_of_a_sent_prompt() -> None:
    """Codex, Devin and Cursor echo a sent prompt in their history with the same marker as the input line.
    Fails if the echo is read as an unsent prompt (Enter would be pressed on an empty box, which in Devin
    interjects) or an unsent one as sent (the prompt would be lost)."""
    sent = (
        "› Use the switchboard tools. Call join\n• Called switchboard.join\n"
        "› Ask Codex to do anything\n  GPT-6-Luna low"
    )
    unsent = "• earlier\n› You are back. Reply with one word\n  GPT-6-Luna low\n  ? for shortcuts"
    assert screens.prompt_line(sent, "›") == "Ask Codex to do anything"
    assert screens.prompt_line(unsent, "›").startswith("You are back")
    assert (
        screens.prompt_line("❭ /exit\n ⏺ done\n❭ Guide Devin while it works", "❭")
        == "Guide Devin while it works"
    )
    assert screens.prompt_line("nothing", "→") == ""


def test_the_preflight_flags_another_mcp_server_under_the_cli() -> None:
    """Fails if a stdio MCP server of the user's, started under the CLI, goes unseen (the run would not be
    isolated from the user's config), or if the plugin's own scripts or the CLI's helper are flagged."""
    ps = "\n".join(
        [
            "100 1 tmux",
            "101 100 claude --model haiku --plugin-dir /x/plugins/claude",
            "102 101 /bin/sh /x/plugins/claude/bin/listen",
            "106 102 curl -s --max-time 930 -K - -X POST -H X-Switchboard-Session: s http://switchboard.localhost:9/mcp/listen",
            "107 101 curl -sf -K - -X POST -H Content-Type: application/json --data-binary {} http://sb.example.com/mcp/session",
            "108 101 python3 /x/plugins/cursor/bin/hook pre",
            "103 101 npx -y @acme/some-mcp-server",
            "104 103 node /Users/someone/.npm/_npx/abc/node_modules/.bin/some-mcp-server",
            "105 101 node /tmp/h/.npm/_npx/9/node_modules/.bin/typescript-language-server",
            "200 1 uvx unrelated-mcp",
        ]
    )
    procs = preflight.parse(ps)
    assert sorted(x.split(":")[0] for x in preflight.problems(101, procs=procs)) == ["103", "104", "105"]
    assert sorted(x.split(":")[0] for x in preflight.problems(101, ("typescript",), procs=procs)) == [
        "103",
        "104",
    ]
    assert preflight.problems(102, procs=procs) == []


def test_cursor_quota_and_login_failures_are_named() -> None:
    """Fails if a Cursor account that can't make a call is not told apart from one that failed otherwise."""
    assert (
        cursor.unavailable("ActionRequiredError: You've hit your usage limit")
        == "the account has hit its usage limit"
    )
    assert "not logged in" in (cursor.unavailable("Error: Not logged in") or "")
    assert cursor.unavailable("ok") is None


# ------------------------------------------------------------------- config
def test_claudes_settings_hold_the_plugins_options_and_no_unattended_authority() -> None:
    """The ``--settings`` file gives the ``--plugin-dir`` plugin its address and token, allows switchboard's
    tools and the scratch scripts, denies cross-session messaging; the launch is permission mode manual with
    user settings off. Fails if a bypass switch or a broad allow rule creeps in."""
    s = claude.settings_json("http://switchboard.localhost:1", "sbat_x")
    assert s["pluginConfigs"] == {
        "switchboard@inline": {"options": {"server_url": "http://switchboard.localhost:1", "token": "sbat_x"}}
    }
    assert set(s["permissions"]["allow"]) == {
        claude.TOOLS, "Bash(sh *hold.sh *)", "Bash(bash *hold.sh *)", "Bash(echo *)",
    }  # fmt: skip
    assert {"SendMessage", "ListAgents"} <= set(s["permissions"]["deny"])
    argv = claude.argv(Path("/p"), Path("/s.json"), resume=False)
    assert argv[argv.index("--permission-mode") + 1] == "manual"
    assert argv[argv.index("--setting-sources") + 1] == "project,local"
    assert argv[argv.index("--model") + 1] == "haiku"
    assert "--continue" in claude.argv(Path("/p"), Path("/s.json"), resume=True)
    assert not [a for a in argv if "dangerously" in a or "bypass" in a.lower()]


def test_a_sign_in_run_types_no_token_and_copies_only_the_login() -> None:
    """A run that signs in through the browser (#488) gives the plugin its address and no token, with the
    same permission rules, and starts Claude Code with a throwaway config directory holding the account's
    login and none of the real file's other secrets (its servers' tokens, its plugins' settings). A first
    run answers its two start-up screens with Enter and nothing else. Fails if a token option creeps in,
    another secret is copied, or the onboarding keys press anything else."""
    s = claude.settings_json("http://localhost:1", None)
    assert s["pluginConfigs"] == {"switchboard@inline": {"options": {"server_url": "http://localhost:1"}}}
    assert s["permissions"] == claude.settings_json("http://localhost:1", "sbat_x")["permissions"]
    creds = {
        "claudeAiOauth": {"accessToken": "a"},
        "mcpOAuth": {"other": "secret"},
        "pluginSecrets": {"p": "s"},
    }
    assert claude_signin.login_only(creds) == {"claudeAiOauth": {"accessToken": "a"}}
    assert screens.claude_onboarding(" Choose the text style that looks best with your terminal") == "Enter"
    assert (
        screens.claude_onboarding(
            " Security notes:\n 1. Claude can make mistakes.\n Press Enter to continue…"
        )
        == "Enter"
    )
    assert screens.claude_onboarding("Yes, I trust this folder") is None
    assert screens.claude_onboarding("Press Enter to continue") is None  # not the security notes
    assert screens.claude_onboarding("Yes, I accept") is None  # a bypass-mode warning is never answered here


def test_codex_runs_with_approvals_on_request_and_its_hooks_trusted_at_their_hash() -> None:
    """Fails if Codex is launched with approvals off or a wider sandbox, or if a hook of the plugin is left
    untrusted (Codex would skip it and every wake would fail silently)."""
    hooks = [
        {"key": "switchboard@switchboard:hooks/hooks.json:stop:0:0", "currentHash": "sha256:aa"},
        {"key": "switchboard@switchboard:hooks/hooks.json:post_tool_use:0:0", "currentHash": "sha256:bb"},
    ]
    toml = codex.config_toml(
        "http://switchboard.localhost:9", Path("/tmp/ws"), hooks, extra="[marketplaces.x]\n"
    )
    assert '[mcp_servers.switchboard]\nurl = "http://switchboard.localhost:9/mcp"' in toml
    assert 'bearer_token_env_var = "SWITCHBOARD_AGENT_TOKEN"' in toml and "tool_timeout_sec = 86400" in toml
    assert 'trusted_hash = "sha256:aa"' in toml and 'trusted_hash = "sha256:bb"' in toml
    assert '[projects."/tmp/ws"]\ntrust_level = "trusted"' in toml
    assert "apps = false" in toml and "remote_plugin = false" in toml
    assert "sbat_" not in toml, "the token is in the child's environment, never in a file"
    argv = codex.argv(Path("/tmp/ws"), None, resume=False)
    assert argv[argv.index("-a") + 1] == "on-request" and argv[argv.index("-s") + 1] == "workspace-write"
    assert not [a for a in argv if "dangerously" in a or "never" in a or "danger-full-access" in a]
    assert codex.argv(Path("/w"), "01a16105-984c-77f3-832d-84cb907730d0", True)[:3] == [
        "codex", "resume", "01a16105-984c-77f3-832d-84cb907730d0",
    ]  # fmt: skip
    assert codex.argv(Path("/w"), None, True)[1:3] == ["resume", "--last"]
    assert codex.resume_id(
        "To continue this session, run:\n  codex resume 01a16105-984c-77f3-832d-84cb907730d0"
    )


def test_devins_project_config_is_the_plugins_with_its_path_and_nothing_imported() -> None:
    """Fails if a hook still points at the placeholder path, if another tool's config is imported, or if the
    allow list is wider than switchboard's own tools and the scratch script."""
    cfg = devin.project_config(REPO / "plugins" / "devin")
    commands = [h["command"] for groups in cfg["hooks"].values() for g in groups for h in g["hooks"]]
    assert commands and all(c.startswith(str(REPO / "plugins" / "devin" / "bin" / "hook")) for c in commands)
    assert "/ABSOLUTE/PATH/TO" not in json.dumps(cfg)
    assert not any(cfg["read_config_from"].values())
    assert set(cfg["permissions"]["allow"]) == {f"mcp__switchboard_https__{t}" for t in devin.TOOLS} | set(
        devin.ALLOW_EXTRA
    )
    entry = devin.mcp_config("http://switchboard.localhost:9", "sbat_x")["mcpServers"]["switchboard_https"]
    assert (
        entry["url"] == "http://switchboard.localhost:9/mcp"
        and entry["headers"]["Authorization"] == "Bearer sbat_x"
    )
    assert "--respect-workspace-trust" in devin.argv(False) and "--continue" in devin.argv(True)


def test_cursors_project_config_reads_the_address_and_token_from_the_environment() -> None:
    """Fails if the token or the address is written into a file, or a plugin variable is left unfilled."""
    plugin = REPO / "plugins" / "cursor"
    mcp = cursor.project_mcp(plugin)
    text = json.dumps(mcp)
    assert "${env:SWITCHBOARD_URL}/mcp" in text and "${env:SWITCHBOARD_AGENT_TOKEN}" in text
    hooks = json.dumps(cursor.project_hooks(plugin))
    assert "CURSOR_PLUGIN_ROOT" not in hooks and str(plugin) in hooks
    assert cursor.argv(False)[:3] == ["agent", "--model", "auto"]


def test_cursors_project_permissions_allow_switchboards_tools_and_the_scratch_script_only() -> None:
    """Approving the server (``agent mcp enable``) doesn't approve its tools: the first live run stopped at
    "Run this MCP tool?" on ``switchboard_join`` (#394). Fails if a tool is missing from the allow rules, if
    a broad rule creeps in (a wildcard server or tool, a bare ``Shell(sh)``, a read or write rule), or if
    the launch switches approvals off."""
    cli = cursor.project_cli()
    assert set(cli) == {"permissions"} and cli["permissions"]["deny"] == []
    allow = cli["permissions"]["allow"]
    assert [a for a in allow if a.startswith("Mcp(")] == [
        f"Mcp(switchboard-https:switchboard_{t})" for t in cursor.TOOLS
    ]
    assert len(cursor.TOOLS) == 11 and {"join", "say", "wait", "status"} <= set(cursor.TOOLS)
    assert [a for a in allow if not a.startswith("Mcp(")] == ["Shell(sh hold.sh)"]
    assert not any("*" in a for a in allow), allow
    assert not {"--force", "--yolo", "--approve-mcps", "--sandbox"} & set(
        cursor.argv(False) + cursor.argv(True)
    )


def test_cursors_mid_task_paths_follow_what_its_hooks_can_do() -> None:
    """While every tool hook of the Cursor plugin matches switchboard's own tools only, a shell command runs
    none, and a message for a busy session reaches it with its next switchboard call (``say``, ``read``,
    ``wait``) or as hook context after one, as for Devin (seen live, #394). Fails if the live test expects
    that once a tool hook matches other tools too (as Claude Code's and Codex's do, #422): then only
    ``hook_ctx`` is right, and a looser set would pass a plugin that no longer delivers after a shell call."""
    hooks = cursor.project_hooks(REPO / "plugins" / "cursor")["hooks"]
    events = ("preToolUse", "postToolUse", "postToolUseFailure")
    matchers = [g.get("matcher") for ev in events for g in hooks[ev]]
    only_switchboard = all(m is not None and m.startswith("MCP:switchboard_") for m in matchers)
    expected = {"say", "read", "wait", "hook_ctx"} if only_switchboard else {"hook_ctx"}
    assert set(cursor.CursorRun.mid_paths) == expected, (matchers, cursor.CursorRun.mid_paths)


def test_the_drift_check_names_the_files_that_changed() -> None:
    before = {".claude/settings.json": "a", ".claude.json": "x", ".codex/config.toml": None}
    after = {".claude/settings.json": "b", ".claude.json": "y", ".codex/config.toml": None}
    fail, info = drift.compare(before, after)
    assert fail == ["~/.claude/settings.json"] and info == ["~/.claude.json"]
    assert drift.compare(before, before) == ([], [])


def test_the_child_path_carries_the_cli_and_the_system_not_this_processes_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harness import run

    monkeypatch.setattr(run, "REAL_PATH", f"{REPO}/.venv/bin:/opt/plugin/bin:/usr/bin:/bin")
    path = run.child_path("sh")
    assert path.endswith(run.SYSTEM_PATH) and "/opt/plugin/bin" not in path and ".venv" not in path


def test_a_clis_environment_reaches_it_through_a_script_and_never_a_command_line(tmp_path: Path) -> None:
    """The agent token is in the child's environment, not in any argv: tmux is given ``env -i /bin/sh
    <script>``, and the script (0700) holds the exports. Fails if the token is in the command tmux is
    given, if the script is readable by others, or if the CLI doesn't get exactly that environment."""
    from harness import tmuxdrv

    token = "sbat_" + "x" * 43
    env = {"PATH": "/usr/bin:/bin", "SWITCHBOARD_AGENT_TOKEN": token, "ODD": 'it\'s a "value" with $dollars'}
    command = tmuxdrv.launch_command(
        tmp_path / "launch-a1.sh", env, ["sh", "-c", 'echo "$SWITCHBOARD_AGENT_TOKEN|$ODD|$HOME|$USER"']
    )
    assert token not in command and command.startswith("env -i /bin/sh ")
    assert (tmp_path / "launch-a1.sh").stat().st_mode & 0o777 == 0o700
    r = subprocess.run(
        ["sh", "-c", command], env={"HOME": "/leak", "USER": "leak", "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=20,
    )  # fmt: skip
    assert r.stdout.strip() == f'{token}|it\'s a "value" with $dollars||', r


def test_the_scratch_script_waits_for_the_go_flag_and_gives_up(tmp_path: Path) -> None:
    """``hold.sh`` marks that it started, waits for ``<name>.go`` and ends at once when it exists. Fails if
    it doesn't touch ``<name>.started`` (the test would never see the call in flight) or ignores the flag."""
    from harness.run import HOLD_SH

    (tmp_path / "hold.sh").write_text(HOLD_SH)
    (tmp_path / "t.go").touch()
    r = subprocess.run(["sh", "hold.sh", "t"], cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert (
        r.stdout.strip() == "released t"
        and (tmp_path / "t.started").exists()
        and (tmp_path / "t.done").exists()
    )
    assert "150" in HOLD_SH, "a held call must end by itself so a failed run leaves nothing waiting"


def test_scenarios_name_what_they_check() -> None:
    """Every scenario function has a docstring that says what it checks and which bug it catches."""
    for name in (
        "join_and_say",
        "idle_wake",
        "mid_task",
        "subagent",
        "quit_offline",
        "resume",
        "revoked_token",
    ):
        assert (getattr(scenarios, name).__doc__ or "").strip(), name


# --------------------------------------------------------------- the opt-in
def test_a_harness_runs_only_when_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """The opt-in is the harness's name (or ``all``) in ``SWITCHBOARD_LIVE``, and the CLIs and tmux must be
    there. Fails if a harness runs without being named, or if a missing CLI is not named in the reason. The
    PATH lookup is faked: a CI runner may have no tmux."""
    monkeypatch.setattr(
        world.shutil, "which", lambda name, path=None: None if name == "no-such-cli" else "/bin/x"
    )
    monkeypatch.delenv("SWITCHBOARD_LIVE", raising=False)
    assert "SWITCHBOARD_LIVE=claude" in (world.skip_reason("claude", "sh") or "")
    monkeypatch.setenv("SWITCHBOARD_LIVE", "codex, devin")
    assert world.skip_reason("codex", "sh") is None and world.skip_reason("claude", "sh")
    monkeypatch.setenv("SWITCHBOARD_LIVE", "all")
    assert world.skip_reason("cursor", "sh") is None
    assert "no-such-cli" in (world.skip_reason("cursor", "no-such-cli") or "")


def test_a_plain_pytest_skips_every_live_module() -> None:
    """With no ``SWITCHBOARD_LIVE`` the five live modules are collected and skipped, each with the line that
    says how to run it, and nothing is run. Fails if a live module starts a broker or a CLI on import, or if
    one is added without the opt-in."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SWITCHBOARD", "CLAUDE", "CODEX"))}
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/live", "-q", "-rs", "-p", "no:cacheprovider", "-m", "live"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    out = r.stdout + r.stderr
    assert r.returncode in (0, 5), out  # 5: no test was collected, every module skipped itself
    assert "5 skipped" in out and "passed" not in out and "failed" not in out, out
    for h in ("claude", "codex", "devin", "cursor"):
        assert f"SWITCHBOARD_LIVE={h}" in out, out
    assert sorted(p.name for p in (LIVE).glob("test_live_*.py")) == [
        "test_live_claude.py", "test_live_claude_signin.py", "test_live_codex.py", "test_live_cursor.py",
        "test_live_devin.py",
    ]  # fmt: skip
