"""A private tmux server that plays the human at a terminal (test harness only).

Every tmux client call and every launched CLI gets a clean ``env -i`` env
(HOME, PATH, USER, LOGNAME, SHELL, TERM, LANG plus what the caller adds), so
nothing from the build session (e.g. its ``CLAUDE_CODE_MESSAGING_*``) leaks
into a test session. The server lives on its own ``-L yk-live-...`` socket and
is killed at the end; the user's own tmux sessions are never touched.
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

_PW = pwd.getpwuid(os.getuid())
REAL_HOME = _PW.pw_dir
REAL_USER = _PW.pw_name


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


def env_command(env: dict[str, str], argv: list[str]) -> str:
    """``env -i K=V ... argv``, every value quoted (PATH may contain spaces)."""
    parts = ["env", "-i"] + [f"{k}={shlex.quote(v)}" for k, v in env.items()]
    return " ".join(parts[:2] + parts[2:] + [shlex.quote(a) for a in argv])


class Tmux:
    def __init__(self, label: str, path: str):
        self.sock = f"yk-live-{label}-{os.getpid()}"
        self.path = path
        self.env = clean_env(path)
        self.bin = shutil.which("tmux", path=path) or "tmux"

    def run(self, *args: str, timeout: float = 15) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.bin, "-L", self.sock, *args], env=self.env, capture_output=True, text=True, timeout=timeout
        )

    def new_session(
        self, name: str, cwd: str, env: dict[str, str], argv: list[str], width: int = 200, height: int = 50
    ) -> None:
        r = self.run(
            "new-session",
            "-d",
            "-s",
            name,
            "-x",
            str(width),
            "-y",
            str(height),
            "-c",
            cwd,
            env_command(env, argv),
        )
        if r.returncode != 0:
            raise RuntimeError(f"tmux new-session failed: {r.stderr.strip()}")
        # keep the pane after the program exits, so its last screen can be read
        self.run("set-option", "-t", name, "remain-on-exit", "on")

    def capture(self, name: str) -> str:
        return self.run("capture-pane", "-p", "-t", name).stdout

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

    def kill_server(self) -> None:
        self.run("kill-server")
        # tmux leaves its socket file behind; remove ours (and only ours)
        sock = Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}" / self.sock
        try:
            if stat.S_ISSOCK(os.lstat(sock).st_mode):
                sock.unlink()
        except OSError:
            pass
