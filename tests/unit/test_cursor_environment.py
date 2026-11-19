"""The Cursor Cloud Agents environment (CONTRIBUTING.md, "Cloud agents").

Cursor prepares a cloud agent's machine by running the ``install`` command in ``.cursor/environment.json``,
which runs ``.cursor/install.sh``. The script pins uv, CPython and Node; these tests keep uv's and Python's
pins equal to CI's and Node's on CI's major, so an agent's checks run on what CI runs, and keep every
download a pinned, checksum-checked archive (a piped installer or a moving release alias would run whatever
the server sends that day). The last three run the script itself, with stand-ins for curl, tar, uv, Node
and npm in a temp home, to show the checks run rather than just appear in the text.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest
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
    assert "sha256sum -c --quiet -" in text
    # nothing downloaded is piped into a shell (`| sha256sum` is the check, not a shell)
    assert not re.search(r"\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b", text)
    assert "latest" not in text
    for call in re.findall(r"^\s*fetch (\S+) \"([^\"]+)\" \"\$(\w+)\"$", text, re.MULTILINE):
        assert "$UV_VERSION" in call[1] or "$NODE_VERSION" in call[1], call
        assert call[2] in {"uv_sha256", "node_sha256"}, call
    assert len(re.findall(r"^\s*fetch ", text, re.MULTILINE)) == 2


def stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)


def run_script(tmp_path: Path, text: str, stubs: Path) -> subprocess.CompletedProcess[str]:
    copy = tmp_path / "install.sh"
    copy.write_text(text, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    env = child_env(tmp_path / "home")
    env["PATH"] = f"{stubs}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(copy)], cwd=work, env=env, capture_output=True, text=True, timeout=60, check=False
    )


@pytest.mark.parametrize("digest", ["pinned", "of these bytes"])
def test_a_download_is_unpacked_only_when_its_checksum_matches(tmp_path: Path, digest: str) -> None:
    """The checksum check runs: a stand-in curl writes bytes that aren't uv's archive, and with the
    script's own pin the run stops before tar or install touch them. With the pin swapped for these
    bytes' digest, tar runs, which shows the check, not the stand-ins, is what stopped it."""
    payload = "not the archive"
    stubs = tmp_path / "stubs"
    calls = tmp_path / "calls"
    stub(
        stubs / "curl",
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in -o) out="$2"; shift ;; https://*) url="$1" ;; esac; shift\n'
        "done\n"
        f'echo "$url" >> {calls}.curl\nprintf %s {payload!r} > "$out"',
    )
    for tool in ("tar", "install"):
        stub(stubs / tool, f'echo "$*" >> {calls}.{tool}')
    text = script()
    if digest != "pinned":
        mine = hashlib.sha256(payload.encode()).hexdigest()
        text = re.sub(r"^(UV_SHA256_\w+)=[0-9a-f]{64}$", rf"\1={mine}", text, flags=re.MULTILINE)
    result = run_script(tmp_path, text, stubs)
    assert result.returncode != 0  # either way: no real uv was installed
    fetched = Path(f"{calls}.curl").read_text(encoding="utf-8")
    assert f"https://github.com/astral-sh/uv/releases/download/{pin('UV_VERSION')}/" in fetched
    unpacked = Path(f"{calls}.tar").exists()
    assert unpacked == (digest != "pinned"), result.stderr
    if digest == "pinned":
        assert not Path(f"{calls}.install").exists()
        assert "did NOT match" in result.stderr  # sha256sum's own refusal


def test_npm_runs_on_the_pinned_node_and_a_broken_npm_stops_the_build(tmp_path: Path) -> None:
    """npm's launcher runs `/usr/bin/env node`, so this run's PATH must find the pinned Node first;
    and an npm that fails stops the Build before Python and the .venv, rather than vanishing inside
    an echo. The tools already in place are stand-ins at the pinned versions, so nothing downloads."""
    home = tmp_path / "home"
    calls = tmp_path / "calls"
    stub(home / ".local" / "bin" / "uv", f'echo "$*" >> {calls}.uv\necho "uv {pin("UV_VERSION")}"')
    node = home / ".local" / "node" / "bin"
    stub(node / "node", f'echo "v{pin("NODE_VERSION")}"')
    stub(node / "npm", f"command -v node > {calls}.npm-node\nexit 3")
    stub(node / "npx", "exit 0")
    result = run_script(tmp_path, script(), tmp_path / "no-stubs")
    assert result.returncode == 3, result.stderr
    assert Path(f"{calls}.npm-node").read_text(encoding="utf-8").strip() == str(
        home / ".local" / "bin" / "node"
    )
    assert "python" not in Path(f"{calls}.uv").read_text(encoding="utf-8")  # stopped before Python


@pytest.mark.parametrize(("venv_base", "outcome"), [("/usr", "rebuilt"), ("/managed/cpython-3.13", "kept")])
def test_a_venv_on_another_python_is_rebuilt(tmp_path: Path, venv_base: str, outcome: str) -> None:
    """A .venv an earlier Build made from a system Python 3.13 is removed before `uv sync`, and one on
    uv's managed Python is kept. The stand-in uv answers `python find` as uv 0.7.13 does (checked by
    hand): the discovered .venv first, the managed install only with --system. Without --system the
    script would compare the .venv with itself and keep it."""
    home = tmp_path / "home"
    calls = tmp_path / "calls"
    managed = tmp_path / "managed" / "python"
    stub(managed, "echo /managed/cpython-3.13")
    stub(
        home / ".local" / "bin" / "uv",
        f'echo "$*" >> {calls}.uv\n'
        'case "$1" in\n'
        f'  --version) echo "uv {pin("UV_VERSION")}" ;;\n'
        '  python) [ "$2" = find ] || exit 0\n'
        f'    case " $* " in *" --system "*) echo {managed} ;; *) echo "$PWD/.venv/bin/python" ;; esac ;;\n'
        f"  sync) if [ -e .venv ]; then echo kept; else echo rebuilt; fi > {calls}.venv ;;\n"
        "esac",
    )
    node = home / ".local" / "node" / "bin"
    stub(node / "node", f'echo "v{pin("NODE_VERSION")}"')
    stub(node / "npm", "echo 10.9.9")
    stub(node / "npx", "exit 0")
    stub(tmp_path / "work" / ".venv" / "bin" / "python", f"echo {venv_base}")
    copy = tmp_path / "install.sh"
    copy.write_text(script(), encoding="utf-8")
    result = subprocess.run(
        ["bash", str(copy)],
        cwd=tmp_path / "work",
        env=child_env(home),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert Path(f"{calls}.venv").read_text(encoding="utf-8").strip() == outcome
