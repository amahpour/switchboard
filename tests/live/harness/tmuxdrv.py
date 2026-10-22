"""A private tmux server that plays the human at a terminal (test harness only).

Every tmux client call and every launched CLI gets a clean ``env -i`` environment (HOME, PATH, USER,
LOGNAME, SHELL, TERM, LANG plus what the caller adds), so nothing from the session that runs pytest
(its ``CLAUDE_CODE_*`` variables, an agent token) leaks into a test session. The server lives on its
own ``-L sb-live-...`` socket and is killed at the end; the user's own tmux sessions are never touched.
"""

from __future__ import annotations

import os
import pwd
import shlex
import shutil
import stat
import subprocess
import time
from pathlib import Path

try:
    # read from the password database, not the environment: tests/conftest.py points $HOME at a temp dir
    _PW = pwd.getpwuid(os.getuid())
    REAL_HOME, REAL_USER = _PW.pw_dir, _PW.pw_name
except KeyError:  # a container user with no passwd entry: the live tests are skipped there anyway
    REAL_HOME, REAL_USER = os.environ.get("HOME", "/"), os.environ.get("USER", "user")


def clean_env(path: str, **extra: str) -> dict[str, str]:
    env = {
        "HOME": REAL_HOME,
        "PATH": path,
        "USER": REAL_USER,
        "LOGNAME": REAL_USER,
        "SHELL": "/bin/bash",
        "TERM": "xterm-256color",
        "LANG": "en_US.UTF-8",
    }
    env.update(extra)
    return env


def launch_command(script: Path, env: dict[str, str], argv: list[str]) -> str:
    """The command tmux runs for a CLI, with its environment kept off every command line.

    The environment (an agent token among it) is written to ``script`` (mode 0700, from its first byte, in the
    run's temp directory) as ``export`` lines, which are shell built-ins and so start no process whose
    arguments name them, then ``exec`` of the CLI. tmux is given ``env -i /bin/sh <script>``: the CLI starts
    with exactly that environment and nothing the tmux server had."""
    lines = ["#!/bin/sh", *[f"export {k}={shlex.quote(v)}" for k, v in env.items()]]
    lines.append("exec " + " ".join(shlex.quote(a) for a in argv))
    script.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(script, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    return f"env -i /bin/sh {shlex.quote(str(script))}"


class Tmux:
    def __init__(self, label: str, path: str):
        self.sock = f"sb-live-{label}-{os.getpid()}"
        self.path = path
        self.env = clean_env(path)
        self.bin = shutil.which("tmux", path=path) or "tmux"

    def run(self, *args: str, timeout: float = 15) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.bin, "-L", self.sock, *args], env=self.env, capture_output=True, text=True, timeout=timeout
        )

    def new_session(
        self,
        name: str,
        cwd: str,
        env: dict[str, str],
        argv: list[str],
        script_dir: Path,
        width: int = 160,
        height: int = 50,
    ) -> None:
        command = launch_command(script_dir / f"launch-{name}.sh", env, argv)
        r = self.run("new-session", "-d", "-s", name, "-x", str(width), "-y", str(height), "-c", cwd, command)
        if r.returncode != 0:
            raise RuntimeError(f"tmux new-session failed: {r.stderr.strip()}")
        # keep the pane after the program exits, so its last screen can be read
        self.run("set-option", "-t", name, "remain-on-exit", "on")

    def capture(self, name: str, history: int = 0) -> str:
        """The pane's text, with ``history`` lines of scrollback above it."""
        args = ["capture-pane", "-p", "-t", name]
        if history:
            args += ["-S", str(-history)]
        return self.run(*args).stdout

    def screen(self, name: str, n: int = 30) -> str:
        lines = [x for x in self.capture(name).splitlines() if x.strip()]
        return "\n".join(lines[-n:])

    def type(self, name: str, text: str) -> None:
        """Type a line like the human would, then Enter."""
        self.run("send-keys", "-t", name, "-l", text)
        time.sleep(0.4)
        self.run("send-keys", "-t", name, "Enter")

    def send_text(self, name: str, text: str) -> None:
        """Type text literally, without Enter (the caller checks the screen before pressing it)."""
        self.run("send-keys", "-t", name, "-l", text)

    def key(self, name: str, key: str) -> None:
        self.run("send-keys", "-t", name, key)

    def pane_pid(self, name: str) -> int | None:
        r = self.run("display-message", "-p", "-t", name, "#{pane_pid}")
        try:
            return int(r.stdout.strip())
        except ValueError:
            return None

    def pane_dead(self, name: str) -> bool:
        r = self.run("display-message", "-p", "-t", name, "#{pane_dead}")
        return r.stdout.strip() == "1" or r.returncode != 0

    def has(self, name: str) -> bool:
        return self.run("has-session", "-t", name).returncode == 0

    def kill_session(self, name: str) -> None:
        self.run("kill-session", "-t", name)

    def kill_server(self) -> None:
        self.run("kill-server")
        # tmux leaves its socket file behind; remove ours (and only ours)
        sock = Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}" / self.sock
        try:
            if stat.S_ISSOCK(os.lstat(sock).st_mode):
                sock.unlink()
        except OSError:
            pass
