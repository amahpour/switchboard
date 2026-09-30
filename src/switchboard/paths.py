"""Runtime layout under SWITCHBOARD_HOME (DESIGN.md §2)."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from switchboard.hook.switchboard_hook import sock_path

__all__ = [
    "Paths",
    "UnsafePathError",
    "default_home",
    "ensure_private_dir",
    "hook_source",
    "hook_sha12",
    "is_under_system_tmp",
    "sock_path",
]

TEST_MARKER = ".switchboard-test"


class UnsafePathError(RuntimeError):
    """A private directory or file is not safe to use."""


def default_home() -> str:
    """``$SWITCHBOARD_HOME`` if set, else ``~/.switchboard``."""
    env = os.environ.get("SWITCHBOARD_HOME")
    return env if env else os.path.join(os.path.expanduser("~"), ".switchboard")


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
    def hooks_dir(self) -> Path:
        return self.home / "hooks"

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

    @property
    def satellite_conf(self) -> Path:
        """``satellite.toml``: present only on a satellite home, the far end of a
        remote link (DESIGN.md §27.3; written by ``switchboard remote accept``)."""
        return self.home / "satellite.toml"

    def hook_copy(self, sha12: str) -> Path:
        return self.hooks_dir / f"switchboard_hook-{sha12}.py"

    def ensure(self) -> None:
        """Create every private directory the broker needs (0700, owner-checked)."""
        for d in (self.home, self.hooks_dir, self.run_dir, self.logs_dir):
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
        raise UnsafePathError(
            f"{p} has mode {oct(st.st_mode & 0o777)}; run: chmod 700 '{p}'"
        )
    return p


def hook_source() -> Path:
    """The packaged hook script (copied content-addressed into hooks/)."""
    return Path(__file__).resolve().parent / "hook" / "switchboard_hook.py"


def hook_sha12(data: bytes | None = None) -> str:
    if data is None:
        data = hook_source().read_bytes()
    return hashlib.sha256(data).hexdigest()[:12]


def system_tmp_dirs() -> list[str]:
    dirs = {"/tmp", "/private/tmp", tempfile.gettempdir()}
    return sorted({os.path.realpath(d) for d in dirs})


def is_under_system_tmp(p: str | os.PathLike) -> bool:
    real = os.path.realpath(str(p))
    for d in system_tmp_dirs():
        if real != d and (real + os.sep).startswith(d.rstrip(os.sep) + os.sep):
            return True
    return False


def write_hook_copy(paths: Paths) -> Path:
    """Write the content-addressed, read-only hook copy ``hooks/switchboard_hook-<sha12>.py``.

    Any script change is a new file name, so a harness that trusts hook
    commands (Codex) asks again. An existing copy with the right content is
    left alone; a tampered one is replaced.
    """
    data = hook_source().read_bytes()
    target = paths.hook_copy(hook_sha12(data))
    ensure_private_dir(paths.hooks_dir)
    try:
        if target.read_bytes() == data:
            if stat.S_IMODE(os.stat(target).st_mode) != 0o444:
                os.chmod(target, 0o444)
            return target
    except FileNotFoundError:
        pass
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o444)
    os.replace(tmp, target)
    return target


def test_mode_refusal(paths: Paths, home_given: bool) -> str | None:
    """Why ``--test-mode`` is refused for this home, or None if it's allowed (§2 test
    mode). The broker (``switchboard start --test-mode``) and the satellite
    (``switchboard satellite --test-mode``) share it."""
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


HOOK_STATE_NAMES = 4  # names listed in a MISMATCH (a satellite's hook_state is at most 200 chars)
_HOOK_COPY_NAME = re.compile(r"switchboard_hook-[0-9a-f]{12}\.py")


def hook_state_text(paths: Paths) -> str:
    """``ok (<n> copies)`` or ``MISMATCH: <names>``: the hook copies in ``<home>/hooks``.
    A satellite sends this to the desktop, where the owner reads it, so only names shaped
    like a hook copy are listed (at most ``HOOK_STATE_NAMES``); other files are counted."""
    bad = check_hook_copies(paths)
    if bad:
        shown = [b for b in bad if _HOOK_COPY_NAME.fullmatch(b)][:HOOK_STATE_NAMES]
        more = len(bad) - len(shown)  # named otherwise, or past the first few: counted only
        what = f"{more} {'other ' if shown else ''}file{'' if more == 1 else 's'}"
        return "MISMATCH: " + ", ".join(shown + ([what] if more else []))
    try:
        n = len(list(paths.hooks_dir.glob("switchboard_hook-*.py")))
    except OSError:
        n = 0
    return f"ok ({n} cop{'y' if n == 1 else 'ies'})"


def check_hook_copies(paths: Paths) -> list[str]:
    """Names of hook copies whose content no longer matches the hash in their name."""
    bad = []
    try:
        entries = sorted(paths.hooks_dir.glob("switchboard_hook-*.py"))
    except OSError:
        return bad
    for p in entries:
        want = p.name[len("switchboard_hook-") : -len(".py")]
        try:
            got = hook_sha12(p.read_bytes())
        except OSError:
            bad.append(p.name)
            continue
        if got != want:
            bad.append(p.name)
    return bad
