"""Runtime layout under SWITCHBOARD_HOME (DESIGN.md §2)."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Paths",
    "UnsafePathError",
    "default_home",
    "ensure_private_dir",
    "is_under_system_tmp",
    "sock_path",
]

TEST_MARKER = ".switchboard-test"
SOCK_MAX_BYTES = 100  # macOS sun_path holds at most 103 bytes (FINDINGS §10 S6)


class UnsafePathError(RuntimeError):
    """A private directory or file is not safe to use."""


def default_home() -> str:
    """``$SWITCHBOARD_HOME`` if set, else ``~/.switchboard``."""
    env = os.environ.get("SWITCHBOARD_HOME")
    return env if env else os.path.join(os.path.expanduser("~"), ".switchboard")


def sock_path(home: str | os.PathLike) -> str:
    """The broker's Unix socket path for ``home``: ``<home>/run/broker.sock`` when it fits in 100
    bytes, otherwise ``/tmp/switchboard-<uid>/<sha256(realpath(home))[:12]>.sock``."""
    home = os.path.realpath(os.path.expanduser(str(home)))
    primary = os.path.join(home, "run", "broker.sock")
    if len(primary.encode("utf-8", "surrogateescape")) <= SOCK_MAX_BYTES:
        return primary
    digest = hashlib.sha256(home.encode("utf-8", "surrogateescape")).hexdigest()[:12]
    return os.path.join("/tmp", "switchboard-%d" % os.getuid(), digest + ".sock")


def _resolve(home: str | os.PathLike | None) -> Path:
    raw = str(home) if home is not None else default_home()
    return Path(os.path.realpath(os.path.expanduser(raw)))


@dataclass(frozen=True)
class Paths:
    home: Path

    @classmethod
    def from_home(cls, home: str | os.PathLike | None = None) -> "Paths":
        return cls(_resolve(home))

    # files and dirs --------------------------------------------------------
    @property
    def config(self) -> Path:
        return self.home / "config.toml"

    @property
    def db(self) -> Path:
        return self.home / "switchboard.db"

    @property
    def run_dir(self) -> Path:
        return self.home / "run"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def log(self) -> Path:
        return self.logs_dir / "broker.log"

    @property
    def out_log(self) -> Path:
        """stdout/stderr of a daemonized broker (crash tracebacks)."""
        return self.logs_dir / "broker.out"

    @property
    def sock(self) -> Path:
        return Path(sock_path(str(self.home)))

    @property
    def pidfile(self) -> Path:
        return self.run_dir / "broker.pid"

    @property
    def lockfile(self) -> Path:
        return self.run_dir / "broker.lock"

    @property
    def test_login_token(self) -> Path:
        return self.run_dir / "test-login-token"

    @property
    def test_claim_link(self) -> Path:
        """Test mode only: the current claim link of an unclaimed hosted broker (§31.3), so
        a test reads it from here instead of the broker's stdout."""
        return self.run_dir / "test-claim-link"

    @property
    def test_marker(self) -> Path:
        return self.home / TEST_MARKER

    def ensure(self) -> None:
        """Create every private directory the broker needs (0700, owner-checked)."""
        for d in (self.home, self.run_dir, self.logs_dir):
            ensure_private_dir(d)
        sock_dir = self.sock.parent
        if sock_dir != self.run_dir:
            ensure_private_dir(sock_dir)


def ensure_private_dir(d: str | os.PathLike) -> Path:
    """mkdir 0700, then insist on a real dir we own with no group/other bits."""
    p = Path(d)
    try:
        os.mkdir(p, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(p)
    if not stat.S_ISDIR(st.st_mode):
        raise UnsafePathError(f"{p} is not a directory (symlinks are refused)")
    if st.st_uid != os.getuid():
        raise UnsafePathError(f"{p} is owned by uid {st.st_uid}, not {os.getuid()}")
    if st.st_mode & 0o077:
        raise UnsafePathError(f"{p} has mode {oct(st.st_mode & 0o777)}; run: chmod 700 '{p}'")
    return p


def system_tmp_dirs() -> list[str]:
    dirs = {"/tmp", "/private/tmp", tempfile.gettempdir()}
    return sorted({os.path.realpath(d) for d in dirs})


def is_under_system_tmp(p: str | os.PathLike) -> bool:
    real = os.path.realpath(str(p))
    for d in system_tmp_dirs():
        if real != d and (real + os.sep).startswith(d.rstrip(os.sep) + os.sep):
            return True
    return False


def test_mode_refusal(paths: Paths, home_given: bool) -> str | None:
    """Why ``--test-mode`` is refused for this home, or None if it's allowed (§2 test
    mode, ``switchboard start --test-mode``)."""
    if os.environ.get("SWITCHBOARD_TEST") != "1":
        return "--test-mode needs SWITCHBOARD_TEST=1 in the environment"
    if not home_given:
        return "--test-mode needs an explicit --home"
    if not is_under_system_tmp(paths.home):
        return "--test-mode needs a --home under the system temp dir"
    if not paths.test_marker.exists():
        return f"--test-mode needs a {paths.test_marker.name} marker file in the home"
    real_default = os.path.realpath(os.path.expanduser("~/.switchboard"))
    if str(paths.home) == real_default:
        return "--test-mode can't use ~/.switchboard"
    return None
