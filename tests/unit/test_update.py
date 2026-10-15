"""``switchboard update`` (DESIGN.md §45): how it finds its installer, the command it runs, what it
restarts and re-registers, and that it never starts what wasn't running. The runner is a fake that
records each command, so nothing here installs, starts or registers anything."""

from __future__ import annotations

import argparse
import io
import subprocess
from pathlib import Path

import pytest

from switchboard import cli, update
from switchboard.install import common
from switchboard.update import Installer, UpdateError

UV = Installer("uv", "switchboard-chat")
HOME = "/home/alice/.switchboard-hub"


def tool_dir(tmp_path: Path, *parts: str, receipt: bool = True) -> str:
    d = tmp_path.joinpath(*parts)
    d.mkdir(parents=True)
    if receipt:
        (d / "uv-receipt.toml").write_text("[tool]\n")
    return str(d)


def test_the_installer_is_read_from_where_this_copy_lives(tmp_path: Path) -> None:
    assert update.installer(tool_dir(tmp_path, "uv", "tools", "switchboard-chat"), False) == UV
    pipx = tool_dir(tmp_path, "pipx", "venvs", "switchboard-chat", receipt=False)
    assert update.installer(pipx, False) == Installer("pipx", "switchboard-chat")


@pytest.mark.parametrize(
    ("prefix", "editable", "says"),
    [
        ("/anywhere", True, "runs from a checkout"),
        ("/opt/switchboard", False, "container image"),
        ("/usr", False, "isn't a uv tool or a pipx install"),
    ],
)
def test_what_it_did_not_install_it_refuses_to_upgrade(prefix: str, editable: bool, says: str) -> None:
    with pytest.raises(UpdateError, match=says):
        update.installer(prefix, editable)


def test_a_uv_tool_without_its_receipt_is_not_taken_for_one(tmp_path: Path) -> None:
    with pytest.raises(UpdateError, match="isn't a uv tool"):
        update.installer(tool_dir(tmp_path, "uv", "tools", "switchboard-chat", receipt=False), False)


def test_the_upgrade_command_names_the_package_on_pypi() -> None:
    assert update.install_argv(UV, None, "/bin/uv") == [
        "/bin/uv",
        "tool",
        "install",
        "--refresh-package",
        "switchboard-chat",
        "switchboard-chat@latest",
    ]
    assert update.install_argv(UV, "0.23.0", "/bin/uv") == [
        "/bin/uv",
        "tool",
        "install",
        "--refresh-package",
        "switchboard-chat",
        "switchboard-chat==0.23.0",
    ]
    pipx = Installer("pipx", "switchboard-chat")
    assert update.install_argv(pipx, None, "/bin/pipx") == [
        "/bin/pipx",
        "install",
        "--force",
        "switchboard-chat",
    ]
    assert update.install_argv(pipx, "0.23.0", "/bin/pipx") == [
        "/bin/pipx",
        "install",
        "--force",
        "switchboard-chat==0.23.0",
    ]


@pytest.mark.parametrize("bad", ["latest", "0.23", "0.23.0; rm -rf ~", "--index-url=http://x", "1.2.3rc1"])
def test_a_version_is_a_release_number_and_nothing_else(bad: str) -> None:
    with pytest.raises(UpdateError, match="release number"):
        update.install_argv(UV, bad, "/bin/uv")


def test_the_old_github_tool_is_told_to_switch_once() -> None:
    with pytest.raises(
        UpdateError, match="uv tool uninstall switchboard && uv tool install switchboard-chat"
    ):
        update.install_argv(Installer("uv", "switchboard"), None, "/bin/uv")


def test_only_a_running_dialer_is_restarted_and_a_broker_never() -> None:
    assert update.plan(dialing=True, dialer_running=True, broker_running=False, registered=[]).restart_dialer
    stopped = update.plan(dialing=True, dialer_running=False, broker_running=False, registered=[])
    assert not stopped.restart_dialer and not stopped.broker_running
    desk = update.plan(dialing=False, dialer_running=False, broker_running=True, registered=["claude"])
    assert not desk.restart_dialer and desk.broker_running and desk.harnesses == ["claude"]


class Recorder:
    def __init__(self, fail: str = "") -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 1 if self.fail and self.fail in argv else 0)


def go(
    todo: update.Plan, *, after: str = "0.24.0", yes: bool = True, rec: Recorder | None = None
) -> tuple[int, Recorder, str]:
    rec = rec or Recorder()
    out = io.StringIO()
    rc = update.run(
        home=HOME, home_given=True, version=None, yes=yes, current="0.23.0", inst=UV, tool="/bin/uv",
        exe="/bin/switchboard", state=lambda: todo, runner=rec, new_version=lambda exe: after, out=out,
    )  # fmt: skip
    return rc, rec, out.getvalue()


def test_an_update_upgrades_restarts_the_dialer_and_re_registers_with_the_new_copy() -> None:
    rc, rec, out = go(update.Plan(restart_dialer=True, harnesses=["claude", "codex"]))
    assert rc == 0 and rec.calls == [
        ["/bin/uv", "tool", "install", "--refresh-package", "switchboard-chat", "switchboard-chat@latest"],
        ["/bin/switchboard", "stop", "--home", HOME],
        ["/bin/switchboard", "start", "--home", HOME],
        ["/bin/switchboard", "install", "claude", "--home", HOME, "--yes"],
        ["/bin/switchboard", "install", "codex", "--home", HOME, "--yes"],
    ]
    assert "0.23.0 -> 0.24.0" in out and "restart your running agent sessions" in out


def test_nothing_that_was_not_running_is_started() -> None:
    """The maintainer's worry: `switchboard start` in the wrong home brings an old desktop broker
    back. Fails if update starts anything when no dialer ran there."""
    rc, rec, out = go(update.Plan(harnesses=["claude"]))
    assert rc == 0 and not any(c[1] in ("start", "stop") for c in rec.calls)
    assert "nothing restarted" in out


def test_a_running_broker_is_left_to_the_person() -> None:
    rc, rec, out = go(update.Plan(broker_running=True))
    assert rc == 0 and not any(c[1] in ("start", "stop") for c in rec.calls)
    assert "restart it yourself, with the flags you started it with" in out


def test_already_the_newest_changes_nothing_else() -> None:
    rc, rec, out = go(update.Plan(restart_dialer=True, harnesses=["claude"]), after="0.23.0")
    assert rc == 0 and len(rec.calls) == 1 and "already the newest" in out


def test_a_failed_upgrade_stops_there() -> None:
    rc, rec, _out = go(update.Plan(restart_dialer=True, harnesses=["claude"]), rec=Recorder(fail="tool"))
    assert rc == 1 and len(rec.calls) == 1


def test_without_yes_install_asks_as_it_always_does() -> None:
    _rc, rec, _out = go(update.Plan(harnesses=["claude"]), yes=False)
    assert rec.calls[-1] == ["/bin/switchboard", "install", "claude", "--home", HOME]


def test_registered_reads_whether_a_harness_runs_this_home(tmp_path: Path) -> None:
    """``update`` re-registers exactly the harnesses that are: Claude Code installed into a temp ~
    for one home is registered for it, not for another, and Codex isn't at all."""
    user_home, home = tmp_path / "u", str(tmp_path / "sb")
    user_home.mkdir()
    assert not common.registered("claude", home, user_home)
    args = argparse.Namespace(
        harness="claude", home=home, user_home=str(user_home), yes=True, dry_run=False, print_args=False,
        workspace=None, allow_editable=True,
    )  # fmt: skip
    assert common.run_install(args, out=io.StringIO()) == 0
    assert common.registered("claude", home, user_home)
    assert not common.registered("claude", str(tmp_path / "other"), user_home)
    assert not common.registered("codex", home, user_home)


def test_the_command_refuses_a_checkout_and_changes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    """The test run's switchboard is the repo, installed editable: update must say so and stop."""
    assert common.editable_install()
    assert cli.main(["update"]) == cli.EXIT_ERR
    assert "runs from a checkout" in capsys.readouterr().err
