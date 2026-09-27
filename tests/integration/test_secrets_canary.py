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
    target.write_text(json.dumps({
        "env": {"API_TOKEN": CANARY, "DATABASE_URL": f"postgres://u:{CANARY2}@db/x"},
        "mcpServers": {"other": {"command": "x", "env": {"SECRET": CANARY2}}},
        "hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": f"notify {CANARY}"}]}]},
    }))
    target.chmod(0o644)
    base = [sys.executable, "-m", "switchboard", "--home", str(tmp_home), "install", "claude", "--user-home", str(uh)]
    outs = []
    for extra in (["--dry-run"], ["--yes", "--allow-editable"], ["--yes", "--allow-editable"], ["--print-args"]):
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
