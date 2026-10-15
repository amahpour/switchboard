"""``switchboard update``: upgrade this install and bring what it set up along (DESIGN.md §45).

It does what docs/INSTALL.md's "To upgrade" says, in order:

1. **Upgrade this copy** the way it was installed: a uv tool (``uv tool install
   --refresh-package switchboard-chat switchboard-chat@latest``, or ``==<version>`` with ``--version``)
   or pipx (``pipx install --force switchboard-chat``). Anything else (a checkout, the container image,
   a plain venv) is refused with what to do instead: switchboard can't upgrade what it didn't install.
2. **Restart the dialer** if one runs in the home (a machine that dials a hosted broker). Nothing
   that wasn't running is started, so ``update`` never brings an old desktop broker back on a
   machine that now dials a hosted one; a running local broker is left to the person, who knows
   the flags they started it with (``--listen``, ``--public-url``).
3. **Re-register the harnesses** this home is registered with (the hook file's name changes with
   each version), through ``switchboard install <harness>``, which shows its diff and asks, or not
   with ``--yes``. A harness that isn't registered for this home is left alone.

Steps 2 and 3 run the *new* ``switchboard`` (a fresh process), never this one's old code.
The plan is pure (``plan``, ``installer``); ``run`` carries it out through an injected runner.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

DIST = "switchboard-chat"
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")  # a release, as PyPI lists it: never a URL or a flag


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class Installer:
    """How this copy of switchboard was installed: ``kind`` uv or pipx, and the tool's name."""

    kind: str
    name: str


def installer(prefix: str, editable: bool) -> Installer:
    """From ``sys.prefix``: a uv tool lives in ``…/uv/tools/<name>`` with a ``uv-receipt.toml``,
    a pipx one in ``…/pipx/venvs/<name>``. Anything else can't be upgraded from here."""
    if editable:
        raise UpdateError(
            "this switchboard runs from a checkout (an editable install): update the checkout"
            " (git pull), then reinstall a copy with `uv tool install --reinstall .`"
        )
    p = Path(prefix)
    if p.parent.name == "tools" and p.parent.parent.name == "uv" and (p / "uv-receipt.toml").is_file():
        return Installer("uv", p.name)
    if p.parent.name == "venvs" and p.parent.parent.name == "pipx":
        return Installer("pipx", p.name)
    if str(p).startswith("/opt/switchboard"):
        raise UpdateError(
            "this is the container image: pull the new release's image and restart the container"
            ' (docs/DEPLOY.md, "Upgrading")'
        )
    raise UpdateError(
        f"this switchboard ({prefix}) isn't a uv tool or a pipx install, so it can't upgrade itself:"
        f" upgrade it the way you installed it (docs/INSTALL.md)"
    )


def install_argv(inst: Installer, version: str | None, tool: str) -> list[str]:
    """The command that upgrades this copy: ``tool`` is the path of ``uv`` or ``pipx``."""
    if version is not None and not VERSION_RE.fullmatch(version):
        raise UpdateError(f"--version takes a release number like 0.23.0, not {version!r}")
    if inst.name != DIST:
        # before 0.21.1 the tool was `switchboard`, installed from GitHub: such a copy has no
        # `update` command, but a renamed one would still upgrade the wrong package
        raise UpdateError(
            f"this copy is the `{inst.name}` tool, not {DIST}: switch once with"
            f" `uv tool uninstall {inst.name} && uv tool install {DIST}` (docs/INSTALL.md)"
        )
    spec = f"{DIST}=={version}" if version else (f"{DIST}@latest" if inst.kind == "uv" else DIST)
    if inst.kind == "uv":
        # uv answers from its copy of the index for up to ten minutes, so right after a release it
        # said "already the newest" (#330): --refresh-package asks again, for this one package
        return [tool, "tool", "install", "--refresh-package", DIST, spec]
    # no flag for pipx: pip asks the index again on every run (it sends max-age=0 for /simple/ pages)
    return [tool, "install", "--force", spec]


@dataclass
class Plan:
    """What ``update`` does after the upgrade: restart a running dialer, say so of a running
    broker, re-register what's registered."""

    restart_dialer: bool = False
    broker_running: bool = False
    harnesses: list[str] = field(default_factory=list)


def plan(*, dialing: bool, dialer_running: bool, broker_running: bool, registered: Sequence[str]) -> Plan:
    """Restart only a dialer that already runs; a broker is told about, never restarted (its start
    flags aren't known here); a stopped one stays stopped. Re-register exactly the harnesses that
    are."""
    return Plan(
        restart_dialer=dialing and dialer_running,
        broker_running=(not dialing) and broker_running,
        harnesses=list(registered),
    )


Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    # the child's own output goes straight to the terminal (install's diff and its question)
    return subprocess.run(argv, check=False, text=True)


def _version_of(exe: str) -> str:
    r = subprocess.run([exe, "--version"], capture_output=True, text=True, check=False)
    m = VERSION_RE.search(r.stdout or "")
    return m.group(0) if m else "?"


def run(
    *,
    home: str,
    home_given: bool,
    version: str | None,
    yes: bool,
    current: str,
    inst: Installer,
    tool: str,
    exe: str,
    state: Callable[[], Plan],
    runner: Runner = _run,
    new_version: Callable[[str], str] | None = None,
    out: TextIO | None = None,
) -> int:
    """Upgrade, then restart and re-register through the new ``exe``. ``state`` reads this home
    (before the upgrade, with this process's code: what runs and what's registered)."""
    out = out or sys.stdout
    todo = state()
    argv = install_argv(inst, version, tool)
    print(f"switchboard {current}: upgrading with `{' '.join(argv)}`", file=out)
    if runner(argv).returncode != 0:
        print("switchboard: the upgrade failed; nothing else was changed", file=sys.stderr)
        return 1
    after = (new_version or _version_of)(exe)
    if after == current:
        print(f"switchboard {current} is already the newest" + (f" {version}" if version else ""), file=out)
        return 0
    print(f"switchboard {current} -> {after}", file=out)
    home_args = ["--home", home] if home_given else []
    rc = 0
    flag = f" --home {home}" if home_given else ""
    if todo.restart_dialer:
        print(f"restarting the dialer in {home}", file=out)
        for verb in ("stop", "start"):
            if runner([exe, verb, *home_args]).returncode != 0:
                print(f"switchboard: `switchboard {verb}{flag}` failed; run it yourself", file=sys.stderr)
                rc = 1
                break
    elif todo.broker_running:
        print(
            f"the broker in {home} still runs {current}: restart it yourself, with the flags you started it"
            f" with (`switchboard stop{flag}`, then `switchboard start{flag} ...`)",
            file=out,
        )
    else:
        print(f"no dialer runs in {home}: nothing restarted", file=out)
    for h in todo.harnesses:
        print(f"re-registering {h}", file=out)
        if runner([exe, "install", h, *home_args, *(["--yes"] if yes else [])]).returncode != 0:
            print(f"switchboard: `switchboard install {h}` didn't finish; run it yourself", file=sys.stderr)
            rc = 1
    if not todo.harnesses:
        print(f"no harness is registered for {home}: nothing to re-register", file=out)
    print("restart your running agent sessions: each keeps the switchboard it started with", file=out)
    return rc


def which(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise UpdateError(f"`{name}` isn't on PATH, and this switchboard was installed with it")
    return path
