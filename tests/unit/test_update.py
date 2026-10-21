"""``switchboard update`` (DESIGN.md §45): how it finds its installer, the command it runs, what it
says of a running broker, that it never starts what wasn't running, and that it no longer re-registers
harnesses (``switchboard install`` is retired, 0.38.0). The runner is a fake that records each command,
so nothing here installs or starts anything."""

from __future__ import annotations

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


def test_a_running_broker_is_told_about() -> None:
    assert not update.plan(broker_running=False).broker_running
    assert update.plan(broker_running=True).broker_running


class Recorder:
    def __init__(self, fail: str = "") -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 1 if self.fail and self.fail in argv else 0)


def go(todo: update.Plan, *, after: str = "0.24.0", rec: Recorder | None = None) -> tuple[int, Recorder, str]:
    rec = rec or Recorder()
    out = io.StringIO()
    rc = update.run(
        home=HOME, home_given=True, version=None, current="0.23.0", inst=UV, tool="/bin/uv",
        exe="/bin/switchboard", state=lambda: todo, runner=rec, new_version=lambda exe: after, out=out,
    )  # fmt: skip
    return rc, rec, out.getvalue()


def test_an_update_only_upgrades_the_copy() -> None:
    """The one command it runs is the upgrade: nothing re-registers a harness (``switchboard install``
    is retired), starts or stops a broker. Fails if update calls the new copy for anything."""
    rc, rec, out = go(update.Plan())
    assert rc == 0 and rec.calls == [
        ["/bin/uv", "tool", "install", "--refresh-package", "switchboard-chat", "switchboard-chat@latest"],
    ]
    assert "0.23.0 -> 0.24.0" in out and "restart your running agent sessions" in out
    assert "re-register" not in out


def test_nothing_that_was_not_running_is_started() -> None:
    """The maintainer's worry: `switchboard start` in the wrong home brings an old desktop broker
    back. Fails if update starts or stops anything."""
    rc, rec, out = go(update.Plan())
    assert rc == 0 and not any(c[1] in ("start", "stop") for c in rec.calls)
    assert "no broker runs in" in out and "nothing restarted" in out


def test_a_running_broker_is_left_to_the_person() -> None:
    rc, rec, out = go(update.Plan(broker_running=True))
    assert rc == 0 and not any(c[1] in ("start", "stop") for c in rec.calls)
    assert "restart it yourself, with the flags you started it with" in out


def test_already_the_newest_changes_nothing_else() -> None:
    rc, rec, out = go(update.Plan(), after="0.23.0")
    assert rc == 0 and len(rec.calls) == 1 and "already the newest" in out


def test_a_failed_upgrade_stops_there() -> None:
    rc, rec, _out = go(update.Plan(), rec=Recorder(fail="tool"))
    assert rc == 1 and len(rec.calls) == 1


def test_a_script_that_still_passes_yes_is_not_broken(capsys: pytest.CaptureFixture[str]) -> None:
    """``update --yes`` confirmed the re-registering that is retired; the flag is accepted and ignored. Fails
    if argparse refuses it (exit 2 and "unrecognized arguments") instead of getting as far as update's
    own refusal of the checkout the tests run from."""
    assert cli.main(["update", "--yes"]) == cli.EXIT_ERR
    err = capsys.readouterr().err
    assert "runs from a checkout" in err and "unrecognized" not in err


def test_the_command_refuses_a_checkout_and_changes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    """The test run's switchboard is the repo, installed editable: update must say so and stop."""
    assert common.editable_install()
    assert cli.main(["update"]) == cli.EXIT_ERR
    assert "runs from a checkout" in capsys.readouterr().err
