"""The fixed Codex helper survives cache updates without trusting environment paths (§49.11, #665)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import child_env
from test_agent_plugins import CURRENT_IMPL, PWSH, impl  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cache(tmp_path: Path, impl: str) -> Path:  # noqa: F811
    """An owned cache, independent of a real harness home."""
    base = tmp_path / "home with spaces/.codex/plugins/cache/switchboard"
    base.mkdir(parents=True)
    return base


def version(cache: Path, number: str) -> Path:
    plugin = cache / "switchboard" / number
    shutil.copytree(ROOT / "plugins/codex", plugin)
    manifest = plugin / ".codex-plugin/plugin.json"
    data = json.loads(manifest.read_text())
    data["version"] = number
    manifest.write_text(json.dumps(data))
    # An observable helper rather than random key generation: selection is what these tests check.
    (plugin / "bin/headers").write_text(f'#!/bin/sh\nprintf \'{{"version":"{number}"}}\\n\'\n')
    (plugin / "bin/windows/headers.ps1").write_text(f'Write-Output \'{{"version":"{number}"}}\'\n')
    # copytree preserves the checkout's shared developer modes; installed fixtures are private to this user.
    for path in [plugin, *plugin.rglob("*")]:
        path.chmod(0o755 if path.is_dir() or path.suffix == "" else 0o644)
    return plugin


def run(script: Path, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    argv = ["sh", str(script)] if CURRENT_IMPL[0] == "sh" else [PWSH, "-NoProfile", "-File", str(script)]
    return subprocess.run(
        argv + list(args), env=child_env(**extra), capture_output=True, text=True, timeout=30
    )


def source(plugin: Path) -> Path:
    return plugin / (
        "bin/headers-launcher" if CURRENT_IMPL[0] == "sh" else "bin/windows/headers-launcher.ps1"
    )


def stable(cache: Path) -> Path:
    return cache / ("codex-headers" if CURRENT_IMPL[0] == "sh" else "codex-headers.ps1")


def test_stable_helper_follows_numeric_updates_after_original_version_is_removed(cache: Path) -> None:
    """The configured command stays put and selects 0.10.0 over 0.9.0 without the old plugin."""
    old = version(cache, "0.8.0")
    installed = run(source(old), "--install")
    assert installed.returncode == 0, installed.stderr
    assert stable(cache).is_file()
    version(cache, "0.9.0")
    version(cache, "0.10.0")
    shutil.rmtree(old)
    result = run(stable(cache))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"version": "0.10.0"}


@pytest.mark.parametrize("unsafe", ["link", "writable", "manifest", "prerelease"])
def test_launcher_refuses_unsafe_or_unidentified_cache_entries(cache: Path, unsafe: str) -> None:
    """A newer entry cannot redirect execution through a link, shared write access or false identity."""
    old = version(cache, "0.8.0")
    assert run(source(old), "--install").returncode == 0
    new = version(cache, "0.9.0" if unsafe != "prerelease" else "9.0.0-evil")
    if unsafe == "link":
        target = new / "bin/headers"
        if CURRENT_IMPL[0] != "sh":
            target = new / "bin/windows/headers.ps1"
        saved = cache / "outside-helper"
        target.rename(saved)
        target.symlink_to(saved)
    elif unsafe == "writable":
        new.chmod(0o777)
    elif unsafe == "manifest":
        (new / ".codex-plugin/plugin.json").write_text('{"name":"other","version":"0.9.0"}')
    result = run(stable(cache))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"version": "0.8.0"}


def test_launcher_ignores_environment_cache_redirects_and_fails_closed_without_a_helper(cache: Path) -> None:
    """No runtime environment variable supplies an executable path; empty caches emit only an empty map."""
    old = version(cache, "0.8.0")
    assert run(source(old), "--install").returncode == 0
    elsewhere = cache.parent / "attacker"
    version(elsewhere, "99.0.0")
    shutil.rmtree(old)
    result = run(
        stable(cache), CODEX_HOME=str(elsewhere), SWITCHBOARD_PLUGIN_ROOT=str(elsewhere), HOME=str(elsewhere)
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {}
    assert "no checked switchboard helper" in result.stderr


def test_installation_refuses_a_shared_or_linked_cache(cache: Path) -> None:
    """Installing a stable executable must not write through a shared cache or a planted launcher link."""
    old = version(cache, "0.8.0")
    victim = cache / "victim"
    victim.write_text("unchanged")
    stable(cache).symlink_to(victim)
    assert run(source(old), "--install").returncode != 0
    assert victim.read_text() == "unchanged"
    stable(cache).unlink()
    cache.chmod(0o777)
    assert run(source(old), "--install").returncode != 0


def test_stable_launcher_runs_the_real_helper_without_changing_process_identity(cache: Path) -> None:
    """Forward a data directory with spaces and retain the caller pid used by meeting-host's ancestor walk."""
    import hashlib
    import os

    old = version(cache, "0.8.0")
    relative = "bin/headers" if CURRENT_IMPL[0] == "sh" else "bin/windows/headers.ps1"
    shutil.copyfile(ROOT / "plugins/codex" / relative, old / relative)
    assert run(source(old), "--install").returncode == 0
    data = cache.parent / "data with spaces"
    result = run(stable(cache), str(data))
    assert result.returncode == 0, result.stderr
    key = (data / f"codex-listener-key-{os.getpid()}").read_text().strip()
    assert json.loads(result.stdout) == {"X-Switchboard-Listener": hashlib.sha256(key.encode()).hexdigest()}
    assert key not in result.stdout + result.stderr
