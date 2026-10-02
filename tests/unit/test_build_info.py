"""A running broker identifies its release and the exact code it was built from (#94)."""

from __future__ import annotations

import argparse
import importlib
import subprocess
from pathlib import Path

import pytest

from switchboard import __version__, cli


FULL = "a" * 40


def info():
    return importlib.import_module("switchboard.build_info")


def test_build_commit_takes_a_valid_build_value_without_looking_up_git(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The image's declared revision wins even when installed outside a Git checkout."""
    monkeypatch.setenv("SWITCHBOARD_BUILD_COMMIT", FULL)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Git was consulted"))
    assert info().read_commit(tmp_path / "switchboard") == FULL


def test_editable_checkout_uses_its_own_git_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An editable install identifies the checkout itself, including in a linked worktree."""
    monkeypatch.delenv("SWITCHBOARD_BUILD_COMMIT", raising=False)
    root = tmp_path / "checkout"
    package = root / "src" / "switchboard"
    package.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "--allow-empty", "-q", "-m", "test"], check=True)
    want = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    assert info().read_commit(package) == want
    linked = tmp_path / "linked"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", "-b", "linked", str(linked)], check=True)
    linked_package = linked / "src" / "switchboard"
    linked_package.mkdir(parents=True)
    assert (linked / ".git").is_file()
    assert info().read_commit(linked_package) == want


def test_installed_package_without_a_build_commit_shows_version_alone(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wheel without build metadata cannot claim the checkout holding its virtualenv."""
    monkeypatch.delenv("SWITCHBOARD_BUILD_COMMIT", raising=False)
    assert info().read_commit(tmp_path / "site-packages" / "switchboard") is None
    monkeypatch.setenv("SWITCHBOARD_BUILD_COMMIT", "not-a-commit")
    assert info().read_commit(tmp_path / "site-packages" / "switchboard") is None


def test_version_and_broker_status_print_the_commit_when_known(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Both CLI entrypoints identify a build by its first seven commit characters."""
    monkeypatch.setattr(info(), "commit", lambda: FULL)
    with pytest.raises(SystemExit) as result:
        cli.build_parser().parse_args(["--version"])
    assert result.value.code == 0
    assert capsys.readouterr().out == f"switchboard {__version__} ({FULL[:7]})\n"

    monkeypatch.setattr(cli, "_satellite_home", lambda args: False)
    monkeypatch.setattr(cli, "_call", lambda *a, **k: {
        "version": __version__, "commit": FULL, "pid": 123, "url": "http://switchboard.localhost:7419/",
        "home": "/tmp/test-home", "hooks": "ok", "codex_link": "ok",
    })
    assert cli.cmd_status(argparse.Namespace(json=False, color="never")) == 0
    assert capsys.readouterr().out.startswith(f"switchboard {__version__} ({FULL[:7]}) running:")


def test_version_without_a_commit_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(info(), "commit", lambda: None)
    assert cli._version() == f"switchboard {__version__}"
