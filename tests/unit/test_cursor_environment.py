"""The Cursor Cloud Agents environment (CONTRIBUTING.md, "Cloud agents").

Cursor prepares a cloud agent's machine by running the ``install`` command in ``.cursor/environment.json``,
which runs ``.cursor/install.sh``. The script pins uv, CPython and Node; these tests keep the pins equal to
CI's, so an agent's checks run on what CI runs, and keep every download a pinned, checksum-checked archive
(a piped installer or a moving release alias would run whatever the server sends that day).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from conftest import child_env

ROOT = Path(__file__).resolve().parents[2]
ENVIRONMENT = ROOT / ".cursor" / "environment.json"
SCRIPT = ROOT / ".cursor" / "install.sh"


def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def pin(name: str) -> str:
    found = re.search(rf"^{name}=(\S+)$", script(), re.MULTILINE)
    assert found, f"{name} is not set in .cursor/install.sh"
    return found.group(1)


def test_the_environment_runs_the_install_script() -> None:
    assert json.loads(ENVIRONMENT.read_text(encoding="utf-8")) == {"install": "bash .cursor/install.sh"}
    assert SCRIPT.is_file()


def test_the_install_script_parses(tmp_path: Path) -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True, env=child_env(tmp_path))


def test_uv_and_python_are_ci_s() -> None:
    action = (ROOT / ".github" / "actions" / "setup-uv-python" / "action.yml").read_text(encoding="utf-8")
    assert re.findall(r'^\s+version: "([^"]+)"$', action, re.MULTILINE) == [pin("UV_VERSION")]
    assert pin("PYTHON_VERSION") == (ROOT / ".python-version").read_text(encoding="utf-8").strip()


def test_node_is_ci_s_major() -> None:
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    assert set(re.findall(r'node-version: "(\d+)"', workflow)) == {pin("NODE_VERSION").split(".")[0]}


def test_every_download_is_a_pinned_checksummed_archive() -> None:
    text = script()
    digests = dict(re.findall(r"^((?:UV|NODE)_SHA256_\w+)=([0-9a-f]{64})$", text, re.MULTILINE))
    assert set(digests) == {"UV_SHA256_X86_64", "UV_SHA256_AARCH64", "NODE_SHA256_X64", "NODE_SHA256_ARM64"}
    # curl appears only inside fetch(), which checks the SHA-256 before anything uses the file
    assert [line.strip() for line in text.splitlines() if "curl " in line] == ['curl -fsSL "$2" -o "$tmp/$1"']
    assert 'sha256sum -c --quiet -' in text
    assert "| sh" not in text and "| bash" not in text
    assert "latest" not in text
    for call in re.findall(r"^\s*fetch (\S+) \"([^\"]+)\" \"\$(\w+)\"$", text, re.MULTILINE):
        assert "$UV_VERSION" in call[1] or "$NODE_VERSION" in call[1], call
        assert call[2] in {"uv_sha256", "node_sha256"}, call
    assert len(re.findall(r"^\s*fetch ", text, re.MULTILINE)) == 2
