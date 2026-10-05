"""Install into a temp home seeded with secrets: nothing leaks into output, backups are private."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

from conftest import child_env

CANARY = "sk-canary-7f3a9c2e1b"
CANARY2 = "canary-db-password-91xq"


def test_install_output_and_backups_never_show_secrets(tmp_path: Path, tmp_home: Path) -> None:
    uh = tmp_path / "uh"
    (uh / ".claude").mkdir(parents=True)
    target = uh / ".claude" / "settings.json"
    target.write_text(
        json.dumps(
            {
                "env": {"API_TOKEN": CANARY, "DATABASE_URL": f"postgres://u:{CANARY2}@db/x"},
                "mcpServers": {"other": {"command": "x", "env": {"SECRET": CANARY2}}},
                "hooks": {
                    "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": f"notify {CANARY}"}]}]
                },
            }
        )
    )
    target.chmod(0o644)
    base = [
        sys.executable,
        "-m",
        "switchboard",
        "--home",
        str(tmp_home),
        "install",
        "claude",
        "--user-home",
        str(uh),
    ]
    outs = []
    for extra in (
        ["--dry-run"],
        ["--yes", "--allow-editable"],
        ["--yes", "--allow-editable"],
        ["--print-args"],
    ):
        r = subprocess.run(base + extra, capture_output=True, text=True, env=child_env(), timeout=60)
        assert r.returncode == 0, r.stderr
        outs.append(r.stdout + r.stderr)
    blob = "\n".join(outs)
    assert CANARY not in blob and CANARY2 not in blob
    [bak] = list((uh / ".claude").glob("settings.json.bak-switchboard-*"))
    assert stat.S_IMODE(bak.stat().st_mode) == 0o600
    after = json.loads(target.read_text())
    assert after["env"]["API_TOKEN"] == CANARY  # kept as it was, just not printed
    for log in tmp_home.rglob("*.log"):
        assert CANARY not in log.read_text(errors="replace")


def test_canary_never_on_link_or_satellite_log(tmp_path: Path) -> None:
    """A secret in a remote agent's env and in what its user types (a prompt, tool input and
    output in hook payloads) never crosses the link, nor reaches satellite.log or broker.log
    (DESIGN.md §27.5.5: no argv, env, cwd or transcript leaves the remote host)."""
    from fakes.fake_claude import FakeClaude
    from fakes.fake_claude import fixture as claude_fixture
    from fakes.fake_link import FakeLink

    frames = tmp_path / "frames.log"
    link = FakeLink(trust=True, env={"SWITCHBOARD_TEST_FRAME_LOG": str(frames)})
    fc = None
    try:
        link.start()
        fc = FakeClaude(
            None,
            home=link.pi,
            sessions_dir=link.pi_sessions,
            env={"API_TOKEN": CANARY, "DATABASE_URL": f"postgres://u:{CANARY2}@db/x"},
        )
        assert fc.tool("join", room="#fpga", screen_name="bench")["ok"]
        fc.hook(
            claude_fixture("UserPromptSubmit", prompt=f"deploy with {CANARY} please", cwd=f"/w/{CANARY2}")
        )
        fc.hook(
            claude_fixture(
                "PostToolUse_bash",
                tool_input={"command": f"echo {CANARY}"},
                tool_response={"stdout": CANARY2, "stderr": ""},
            )
        )
        fc.hook(claude_fixture("Stop", transcript_path=f"/t/{CANARY}.jsonl"))
        assert fc.tool("say", room="#fpga", text="done, nothing secret here")["ok"]
        fc.close()
        fc = None
        blob = frames.read_text(errors="replace")
        assert '"t":"req"' in blob and "hook.event" in blob and "nothing secret here" in blob  # it did log
        logs = [link.pi / "logs" / "satellite.log", link.desk / "logs" / "broker.log"]
        assert all(p.exists() for p in logs)
        for text in [blob] + [p.read_text(errors="replace") for p in logs]:
            assert CANARY not in text and CANARY2 not in text
    finally:
        if fc is not None:
            fc.close()
        link.close()
