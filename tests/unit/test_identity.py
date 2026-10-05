"""mcp.identity.detect: rule order, Claude verification, env leaks (DESIGN.md §6.2)."""

from __future__ import annotations

import json
from pathlib import Path

from switchboard.mcp.identity import detect, env_leak
from switchboard.mcp.server import env_view

CLAUDE_ARGV = "/Users/x/.local/share/claude/versions/2.1.282 --model haiku"


def registry(tmp_path: Path, ppid: int, sock: str) -> str:
    d = tmp_path / "sessions"
    d.mkdir(exist_ok=True)
    (d / f"{ppid}.json").write_text(json.dumps({"pid": ppid, "messagingSocketPath": sock}))
    return str(d)


def test_flag_test_wins_over_everything(tmp_path: Path) -> None:
    sd = registry(tmp_path, 7, "/tmp/cc-socks/7.sock")
    env = {"CLAUDECODE": "1", "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/cc-socks/7.sock"}
    h, ev = detect(env, {"name": "Cursor"}, CLAUDE_ARGV, harness_flag="test", ppid=7, sessions_dir=sd)
    assert h == "test" and ev["rule"] == "flag"


def test_cursor_by_client_info_then_devin_then_codex() -> None:
    assert detect({}, {"name": "Cursor"}, "/usr/bin/zsh")[0] == "cursor"
    assert detect({}, {"name": "x"}, "/opt/devin/bin/devin acp --stdio")[0] == "devin"
    assert detect({}, {"name": "x"}, "/opt/homebrew/bin/codex app-server --listen unix://x")[0] == "codex"
    assert detect({}, {"name": "x"}, "/usr/local/bin/codex")[0] == "codex"
    assert detect({}, None, "/bin/zsh -l")[0] == "unknown"


def test_claude_needs_env_parent_and_registry(tmp_path: Path) -> None:
    sock = "/tmp/cc-socks/42.sock"
    sd = registry(tmp_path, 42, sock)
    env = {"CLAUDECODE": "1", "CLAUDE_CODE_MESSAGING_SOCKET": sock}
    assert detect(env, {"name": "claude-code"}, CLAUDE_ARGV, ppid=42, sessions_dir=sd)[0] == "claude"
    # env alone never makes a session Claude
    assert detect(env, {"name": "claude-code"}, "/bin/bash", ppid=42, sessions_dir=sd)[0] == "unknown"
    # a registry mismatch (another session's socket) is not Claude
    assert (
        detect(
            {**env, "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/cc-socks/9.sock"},
            None,
            CLAUDE_ARGV,
            ppid=42,
            sessions_dir=sd,
        )[0]
        == "unknown"
    )
    # no registry file
    assert detect(env, None, CLAUDE_ARGV, ppid=43, sessions_dir=sd)[0] == "unknown"
    assert (
        detect({"CLAUDE_CODE_MESSAGING_SOCKET": sock}, None, CLAUDE_ARGV, ppid=42, sessions_dir=sd)[0]
        == "unknown"
    )


def test_codex_with_leaked_claude_env_is_codex_with_env_leak() -> None:
    env = {"CLAUDECODE": "1", "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/cc-socks/1.sock"}
    h, ev = detect(env, None, "/usr/local/bin/codex app-server")
    assert h == "codex" and ev["env_leak"] is True
    assert env_leak(env, "claude") is False
    assert env_leak({}, "codex") is False


def test_env_view_never_copies_the_token_value() -> None:
    environ = {
        "CLAUDECODE": "1",
        "CLAUDE_CODE_MESSAGING_TOKEN": "secret-token-value",
        "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/s",
        "HOME": "/h",
        "API_KEY": "k",
    }
    v = env_view(environ)
    assert v["CLAUDE_CODE_MESSAGING_TOKEN"] == ""
    assert "secret-token-value" not in json.dumps(v)
    assert "API_KEY" not in v and "HOME" not in v
