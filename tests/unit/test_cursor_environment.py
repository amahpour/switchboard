"""The Cursor Cloud Agents environment (CONTRIBUTING.md, "Cloud agents").

Cursor prepares a cloud agent's machine by running the ``install`` command in ``.cursor/environment.json``,
which runs ``.cursor/install.sh``. The script pins uv, CPython and Node; these tests keep the pins equal to
CI's, so an agent's checks run on what CI runs.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENVIRONMENT = ROOT / ".cursor" / "environment.json"
SCRIPT = ROOT / ".cursor" / "install.sh"


def pin(name: str) -> str:
    found = re.search(rf"^{name}=(\S+)$", SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
    assert found, f"{name} is not set in .cursor/install.sh"
    return found.group(1)


def test_the_environment_runs_the_install_script() -> None:
    assert json.loads(ENVIRONMENT.read_text(encoding="utf-8")) == {"install": "bash .cursor/install.sh"}
    assert SCRIPT.is_file()


def test_the_install_script_parses() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_uv_and_python_are_ci_s() -> None:
    action = (ROOT / ".github" / "actions" / "setup-uv-python" / "action.yml").read_text(encoding="utf-8")
    assert re.findall(r'^\s+version: "([^"]+)"$', action, re.MULTILINE) == [pin("UV_VERSION")]
    assert pin("PYTHON_VERSION") == (ROOT / ".python-version").read_text(encoding="utf-8").strip()


def test_node_is_ci_s() -> None:
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    assert set(re.findall(r'node-version: "(\d+)"', workflow)) == {pin("NODE_MAJOR")}
