"""Test homes in /tmp, the lock their users hold, and the sweep of a killed run's homes (#919, #922).

``make_tmp_home()`` (tests/conftest.py) makes a short ``/tmp/yk-*`` home with the
``.switchboard-test`` marker. A run that ends normally removes every one (``no_homes_left_behind``,
#920); a run that is killed (SIGKILL, the OOM killer, a timeout, a frozen machine) skips that
cleanup and leaves them, and a RAM-backed /tmp fills (19,181 homes, 6 GB, once).

The sweep decides "nobody uses this home" from two things the kernel keeps true however a process
dies, and nothing else:

- **The home's lock.** Whoever makes a home opens ``LOCK_NAME`` in it and holds ``flock(LOCK_SH)``
  on that fd for the home's life; a child that uses the home (a ``SubprocBroker``, a CLI call) gets
  the lock too (``lock_fds_for``, passed with ``pass_fds``). The kernel drops a flock when the last
  fd on it closes, which a SIGKILL does. The sweep removes a home only if it can take ``LOCK_EX``
  without waiting, and holds it while it checks and removes, so nobody can join the home meanwhile.
- **Its sockets.** A broker that ``switchboard start`` daemonizes (broker/daemon.py, a new session
  with ``close_fds``) doesn't inherit the lock and can outlive pytest. A home with any Unix socket
  under it that accepts a connection is in use. Only "refused" or "gone" counts as dead; any other
  answer (a full backlog, a timeout) keeps the home.

What it never uses, and why (the three review rounds of #920): a folder's mtime (a running broker
writes inside existing files and ``run/``), a pid in the marker (the maker isn't the home's only
user), and ``/proc/<pid>/fd`` (refused for a non-dumpable process of the same user, such as
ssh-agent, so it either removes nothing or misses a user).

A home is only a candidate with both the lock file and the marker, as regular files, in a real
directory (not a symlink) owned by this user, directly under the root and named ``yk-*``.
``make_tmp_home()`` takes the lock before it writes the marker, so a home that is mid-creation, or
was made before this lock existed, has no marker or no lock file and is left alone.

The sweep runs once a session in the xdist controller (``pytest_sessionstart`` in the root conftest),
and nothing it raises reaches the run.
"""

from __future__ import annotations

import errno
import fcntl
import os
import shutil
import socket
import stat
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

TEST_MARKER = ".switchboard-test"  # switchboard.paths.TEST_MARKER; this module imports nothing of src
LOCK_NAME = ".switchboard-test.lock"
TMP_HOME_ROOT = Path("/tmp")
TMP_HOME_PREFIX = "yk-"

# How long the sweep waits for a socket under a candidate home to answer. A live broker accepts at
# once (the kernel completes the connect from the backlog); a timeout keeps the home, never removes it.
SOCKET_TIMEOUT_S = 0.5

# The shared locks this process holds, one per home it made and hasn't seen removed yet.
_LOCKS: dict[Path, int] = {}


def make_locked_home(root: Path = TMP_HOME_ROOT) -> Path:
    """A new home under ``root``, locked (``LOCK_SH``) by this process until ``release_home``.

    The order is the point: the lock file is created and locked before the marker exists, so a home
    with a marker always has a holder or had one."""
    release_removed_homes()
    # short on purpose: the broker's socket inside it must fit sun_path (paths.SOCK_MAX_BYTES)
    d = Path(tempfile.mkdtemp(prefix=TMP_HOME_PREFIX, dir=root))
    fd = os.open(d / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
    except BaseException:
        os.close(fd)
        raise
    _LOCKS[d] = fd
    (d / TEST_MARKER).touch()
    return d


def release_home(home: Path) -> None:
    """Close this process's lock on ``home``; called once the home is removed."""
    fd = _LOCKS.pop(Path(home), None)
    if fd is not None:
        os.close(fd)


def release_removed_homes() -> None:
    """Close the locks of homes that are gone. Most tests remove a home with ``shutil.rmtree`` and
    never tell anyone, so each new home first closes the fds of the removed ones: one open fd a home
    still present, not one a home ever made (the unit tests make thousands)."""
    for d in [d for d in _LOCKS if not os.path.lexists(d)]:
        release_home(d)


def release_all_homes() -> None:
    for d in list(_LOCKS):
        release_home(d)


def lock_fds_for(home: Path) -> tuple[int, ...]:
    """The fds to pass a child process (``pass_fds``) that uses ``home``, so the child holds the
    home's lock for as long as it lives, even after pytest is gone. Empty for a home with no lock
    file (one not made by ``make_tmp_home``)."""
    fd = _LOCKS.get(Path(home))
    if fd is not None and os.path.lexists(home):
        return (fd,)
    return ()


@dataclass
class SweepResult:
    removed: list[Path] = field(default_factory=list)
    in_use: list[Path] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def line(self) -> str:
        line = (
            f"switchboard test homes: removed {len(self.removed)} a killed run left in /tmp, "
            f"kept {len(self.in_use)} in use (#922)"
        )
        if self.failed:
            line += f"; {len(self.failed)} not checked: {self.failed[0]}"
        return line


def _candidates(root: Path) -> list[Path]:
    """The homes that may be swept: real directories named ``yk-*`` directly under ``root``, owned
    by this user, holding the marker and the lock file as regular files."""
    uid = os.getuid()
    out = []
    with os.scandir(root) as it:
        for e in it:
            if not e.name.startswith(TMP_HOME_PREFIX):
                continue
            try:
                st = e.stat(follow_symlinks=False)
                if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
                    continue
                d = Path(e.path)
                marker = os.lstat(d / TEST_MARKER)
                lock = os.lstat(d / LOCK_NAME)
            except OSError:
                continue  # no marker or no lock file: mid-creation, or made before #922
            if stat.S_ISREG(marker.st_mode) and stat.S_ISREG(lock.st_mode):
                out.append(d)
    return out


def socket_answers(path: str) -> bool:
    """True unless a connect to the Unix socket at ``path`` is refused or finds nothing there."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(SOCKET_TIMEOUT_S)
        s.connect(path)
        return True
    except OSError as e:
        return e.errno not in (errno.ECONNREFUSED, errno.ENOENT)
    finally:
        s.close()


def has_live_socket(home: Path) -> bool:
    for dirpath, dirnames, filenames in os.walk(home, followlinks=False):
        for name in filenames + dirnames:
            p = os.path.join(dirpath, name)
            try:
                if not stat.S_ISSOCK(os.lstat(p).st_mode):
                    continue
            except OSError:
                continue
            if socket_answers(p):
                return True
    return False


def _sweep_one(home: Path) -> bool:
    """Remove ``home`` if nobody holds its lock and no socket under it answers. True if removed."""
    try:
        fd = os.open(home / LOCK_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False  # held: a process uses the home
        if has_live_socket(home):
            return False
        shutil.rmtree(home)
        return True
    finally:
        os.close(fd)  # the lock goes with it, after the home is gone


def sweep_killed_homes(root: Path = TMP_HOME_ROOT) -> SweepResult:
    """Remove the homes under ``root`` that a killed run left and nobody uses (module docstring).
    Never raises an ``Exception``: one that a home causes is noted and the next home is checked."""
    result = SweepResult()
    try:
        candidates = _candidates(root)
    except Exception as e:
        result.failed.append(f"{root}: {type(e).__name__}: {e}")
        return result
    for home in candidates:
        try:
            (result.removed if _sweep_one(home) else result.in_use).append(home)
        except Exception as e:
            result.failed.append(f"{home}: {type(e).__name__}: {e}")
    return result


def sweep_at_session_start(
    is_controller: bool, write_line: Callable[[str], object], root: Path = TMP_HOME_ROOT
) -> SweepResult | None:
    """The session-start sweep: once a run, in the controller (each xdist worker is a session of
    its own, and one sweep is enough), and nothing it raises aborts the run. It writes one line
    when it found anything."""
    if not is_controller:
        return None
    try:
        result = sweep_killed_homes(root)
        if result.removed or result.in_use or result.failed:
            write_line(result.line())
        return result
    except Exception as e:
        try:
            write_line(f"switchboard test homes: the sweep failed, nothing removed: {type(e).__name__}: {e}")
        except Exception:
            print(f"switchboard test homes: the sweep failed: {e!r}", file=sys.stderr)
        return None
