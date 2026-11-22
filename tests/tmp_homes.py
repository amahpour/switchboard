"""Test homes in /tmp, the locks their users hold, and the sweep of a killed run's homes (#919, #922).

``make_tmp_home()`` (tests/conftest.py) makes a short ``/tmp/yk-*`` home with the
``.switchboard-test`` marker. A run that ends normally removes every one (``no_homes_left_behind``,
#920); a run that is killed (SIGKILL, the OOM killer, a timeout, a frozen machine) skips that
cleanup and leaves them, and a RAM-backed /tmp fills (19,181 homes, 6 GB, once).

The sweep decides "nobody uses this home" from things the kernel keeps true however a process
dies, and nothing else:

- **The home's test lock.** Whoever makes a home opens ``LOCK_NAME`` in it and holds
  ``flock(LOCK_SH)`` on that fd for the home's life; a child that uses the home (a
  ``SubprocBroker``, a CLI call) gets the lock too (``lock_fds_for``, passed with ``pass_fds``).
  The kernel drops a flock when the last fd on it closes, which a SIGKILL does. The sweep removes
  a home only if it can take ``LOCK_EX`` without waiting, and holds it while it checks and
  removes, so nobody can join the home meanwhile.
- **The broker's own lock.** A broker that ``switchboard start`` daemonizes (broker/daemon.py, a
  new session with ``close_fds``) doesn't inherit the test lock, and the CLI that started it can
  return before it listens. The daemon takes ``LOCK_EX`` on ``BROKER_LOCK`` (``run/broker.lock``,
  ``Paths.lockfile``; conftest checks the two agree) right after ``paths.ensure()``, before it
  opens the database, and holds it until the process exits. The sweep takes it too, without
  waiting: held means in use.
- **Its sockets.** A home with any Unix socket under it that accepts a connection is in use. Only
  "refused" or "gone" counts as dead; any other answer (a full backlog, a timeout) keeps the home,
  and so does any error while looking (a directory it can't read, an entry it can't ``lstat``).

What it never uses, and why (the three review rounds of #920): a folder's mtime (a running broker
writes inside existing files and ``run/``), a pid in the marker (the maker isn't the home's only
user), and ``/proc/<pid>/fd`` (refused for a non-dumpable process of the same user, such as
ssh-agent, so it either removes nothing or misses a user).

A home is only a candidate with both the lock file and the marker, as regular files, in a real
directory (not a symlink) owned by this user, directly under the root and named ``yk-*``.
``make_locked_home()`` takes the lock before it writes the marker, so a home that is mid-creation,
or was made before this lock existed, has no marker or no lock file and is left alone; a failure
part way removes what it made. Nothing is ever chosen by its name alone.

**A broker that is starting.** ``switchboard start`` opens ``logs/broker.out`` (``Paths.out_log``)
before it spawns the daemon, and the daemon creates and locks ``run/broker.lock`` only once it runs.
So a home with ``logs/broker.out`` and no ``run/broker.lock`` is kept and counted in use: a daemon
may be starting. One that died before it made its lock leaves a home that is kept for good, which
is the safe side (CONTRIBUTING.md, "Sharing a machine", lists such homes). The window between the
daemon creating ``broker.lock`` and locking it is safe too: if the sweep takes the lock first, the
daemon's ``LOCK_EX | LOCK_NB`` fails and it exits without using the home.

**One directory, removed in place.** The root is opened once, the home is opened through it with
``O_NOFOLLOW`` and its (dev, inode) noted, the locks are opened through the home's own fd, and the
identity is checked again after the locks and once more before the final ``rmdir``; any mismatch
keeps the home. The removal (``_empty_home``, the same for the sweep and for ``remove_home()``,
the ``tmp_home`` fixture's teardown) goes through the home's fd: every entry but the marker and
the lock file first, then the marker, then the lock file, then the ``rmdir`` through the root's
fd. A removal interrupted in the first step (a killed run, an error) leaves a home that still has
the marker and the lock file under its own name, so the next sweep treats it as any other
candidate, and a fixture's ``no_homes_left_behind`` sees it and fails the run. Only the last two
unlinks and the ``rmdir`` can leave a remainder nothing recognizes: an empty directory, or one
with the lock file alone. There is no rename and no second name.

**One sweeper at a time.** The sweep first takes ``LOCK_EX | LOCK_NB`` on ``SWEEP_LOCK`` in the
root, a regular file of this user's opened with ``O_NOFOLLOW`` (anything else is refused, and
nothing is swept). If another sweep holds it, this one skips the run and says so in its line, so
no sweep ever finds a home another sweep is halfway through removing.

The sweep runs once a session in the xdist controller (``pytest_sessionstart`` in the root conftest),
and nothing it raises reaches the run, nor anything its report raises.
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
BROKER_LOCK = ("run", "broker.lock")  # switchboard.paths.Paths.lockfile, relative to the home
BROKER_OUT = ("logs", "broker.out")  # switchboard.paths.Paths.out_log, relative to the home
TMP_HOME_ROOT = Path("/tmp")
TMP_HOME_PREFIX = "yk-"
SWEEP_LOCK = "yk-sweep.lock"  # a file directly under the root, never a home (homes are directories)

# How long the sweep waits for a socket under a candidate home to answer. A live broker accepts at
# once (the kernel completes the connect from the backlog); a timeout keeps the home, never removes it.
SOCKET_TIMEOUT_S = 0.5

_OPEN_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_OPEN_LOCK = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC  # never blocks on a FIFO
_OPEN_SWEEP_LOCK = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

# The shared locks this process holds, one per home it made and hasn't seen removed yet.
_LOCKS: dict[Path, int] = {}

# Seams for the tests that inject a failure while looking inside a home (root reads anything).
_walk = os.fwalk


def _lstat_at(name: str, dir_fd: int) -> os.stat_result:
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def _rmtree_at(name: str, dir_fd: int) -> None:
    """A directory under a home, removed (a seam for the tests that interrupt a removal)."""
    shutil.rmtree(name, dir_fd=dir_fd)


def make_locked_home(root: Path = TMP_HOME_ROOT) -> Path:
    """A new home under ``root``, locked (``LOCK_SH``) by this process until ``release_home``.

    The order is the point: the lock file is created and locked before the marker exists, so a home
    with a marker always has a holder or had one. A failure at any step closes the fd, forgets it
    and removes the directory before the error goes on."""
    release_removed_homes()
    # short on purpose: the broker's socket inside it must fit sun_path (paths.SOCK_MAX_BYTES)
    d = Path(tempfile.mkdtemp(prefix=TMP_HOME_PREFIX, dir=root))
    fd = -1
    try:
        fd = os.open(d / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        fcntl.flock(fd, fcntl.LOCK_SH)
        _LOCKS[d] = fd
        os.close(os.open(d / TEST_MARKER, os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o600))
    except BaseException:
        _LOCKS.pop(d, None)
        if fd >= 0:
            os.close(fd)
        shutil.rmtree(d, ignore_errors=True)
        raise
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


def lock_fds_for(home: Path) -> tuple[int, ...]:
    """The fds to pass a child process (``pass_fds``) that uses ``home``, so the child holds the
    home's lock for as long as it lives, even after pytest is gone. Empty for a home with no lock
    file (one not made by ``make_tmp_home``)."""
    fd = _LOCKS.get(Path(home))
    if fd is not None and os.path.lexists(home):
        return (fd,)
    return ()


class _NotThisHome(Exception):
    """The name no longer is the directory that was checked: it is kept."""


def _empty_home(home_fd: int) -> None:
    """Remove what is in the open home, the marker and the lock file last (module docstring, "One
    directory, removed in place"), so that an interruption before the end leaves both."""
    last = (TEST_MARKER, LOCK_NAME)
    for name in sorted(os.listdir(home_fd)):
        if name in last:
            continue
        if stat.S_ISDIR(os.stat(name, dir_fd=home_fd, follow_symlinks=False).st_mode):
            _rmtree_at(name, home_fd)
        else:
            os.unlink(name, dir_fd=home_fd)
    for name in last:
        try:
            os.unlink(name, dir_fd=home_fd)
        except FileNotFoundError:
            pass


def _rmdir_if_same(root_fd: int, name: str, before: os.stat_result) -> None:
    """``rmdir`` the emptied home, after checking once more that ``name`` is still that directory."""
    if not _same(before, os.stat(name, dir_fd=root_fd, follow_symlinks=False)):
        raise _NotThisHome(name)
    os.rmdir(name, dir_fd=root_fd)


def remove_home(home: Path) -> None:
    """Remove a ``make_locked_home`` home in place, through its own fd, the marker and the lock file
    last, then close this process's lock. An error part way is not raised, as the
    ``shutil.rmtree(..., ignore_errors=True)`` this replaces did not; it leaves the home under its
    own name with the marker and the lock file, where ``no_homes_left_behind`` finds it and fails
    the run, and a later sweep removes it. A ``home`` that is not a real directory of this user's
    (a symlink a test left) is not touched."""
    home = Path(home)
    try:
        root_fd = os.open(home.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            before = os.stat(home.name, dir_fd=root_fd, follow_symlinks=False)
            if _own_dir(before):
                home_fd = os.open(home.name, _OPEN_DIR, dir_fd=root_fd)
                try:
                    if not _same(before, os.fstat(home_fd)):
                        raise _NotThisHome(home.name)
                    _empty_home(home_fd)
                finally:
                    os.close(home_fd)
                _rmdir_if_same(root_fd, home.name, before)
        finally:
            os.close(root_fd)
    except (OSError, _NotThisHome):
        pass
    release_home(home)


@dataclass
class SweepResult:
    root: Path = TMP_HOME_ROOT
    removed: list[Path] = field(default_factory=list)
    in_use: list[Path] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: bool = False  # another sweep held SWEEP_LOCK

    def line(self) -> str:
        if self.skipped:
            return (
                f"switchboard test homes: skipped, another sweep holds {self.root / SWEEP_LOCK}, "
                "nothing removed (#922)"
            )
        line = (
            f"switchboard test homes: removed {len(self.removed)} a killed run left in {self.root}, "
            f"kept {len(self.in_use)} in use (#922)"
        )
        if self.failed:
            line += f"; {len(self.failed)} not checked: {self.failed[0]}"
        return line


def _same(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _own_dir(st: os.stat_result) -> bool:
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()


def _entries(root_fd: int) -> list[str]:
    """The names under the root that may be homes: ``yk-*``, a real directory (not a symlink) owned
    by this user. Being one makes a name no more than a candidate (``_sweep_one``)."""
    homes = []
    with os.scandir(root_fd) as it:
        for e in it:
            if not e.name.startswith(TMP_HOME_PREFIX):
                continue
            try:
                if not _own_dir(e.stat(follow_symlinks=False)):
                    continue
            except FileNotFoundError:
                continue
            homes.append(e.name)
    return homes


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


def has_live_socket(home_fd: int, home: str) -> bool:
    """True if a Unix socket under the open home answers, or if anything under it can't be looked
    at: a directory the walk can't open or read, an entry ``lstat`` fails on for any reason but
    "gone". ``home`` is the path the sockets are connected by."""
    errors: list[OSError] = []
    for dirpath, dirnames, filenames, dir_fd in _walk(
        ".", dir_fd=home_fd, follow_symlinks=False, onerror=errors.append
    ):
        for name in filenames + dirnames:
            try:
                mode = _lstat_at(name, dir_fd).st_mode
            except FileNotFoundError:
                continue
            except OSError:
                return True
            if stat.S_ISSOCK(mode) and socket_answers(os.path.normpath(os.path.join(home, dirpath, name))):
                return True
    return bool(errors)


def _open_at(path: tuple[str, ...], dir_fd: int) -> int | None:
    """An fd on ``path`` (directories, then a file) under ``dir_fd``, following no symlink; None if
    some part of it isn't there."""
    fds: list[int] = []
    try:
        for part in path[:-1]:
            fds.append(os.open(part, _OPEN_DIR, dir_fd=fds[-1] if fds else dir_fd))
        return os.open(path[-1], _OPEN_LOCK, dir_fd=fds[-1] if fds else dir_fd)
    except FileNotFoundError:
        return None
    finally:
        for fd in fds:
            os.close(fd)


def _lexists_at(path: tuple[str, ...], dir_fd: int) -> bool:
    """Whether ``path`` (directories, then any entry) is under ``dir_fd``, following no symlink. Only
    "not there" is False; any other error is raised, and keeps the home."""
    fds: list[int] = []
    try:
        for part in path[:-1]:
            fds.append(os.open(part, _OPEN_DIR, dir_fd=fds[-1] if fds else dir_fd))
        _lstat_at(path[-1], fds[-1] if fds else dir_fd)
        return True
    except FileNotFoundError:
        return False
    finally:
        for fd in fds:
            os.close(fd)


def _take(fd: int) -> bool:
    """LOCK_EX on a regular file's fd without waiting; False if someone holds it."""
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _sweep_one(root_fd: int, root: Path, name: str) -> bool | None:
    """Remove the home ``name`` under the open root if nobody holds either lock and no socket under
    it answers. True if removed, False if kept, None if it isn't a candidate. Any mismatch of the
    home's identity keeps it."""
    try:
        before = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        home_fd = os.open(name, _OPEN_DIR, dir_fd=root_fd)
    except FileNotFoundError:
        return None
    fds = [home_fd]
    try:
        if not _same(before, os.fstat(home_fd)) or not _own_dir(before):
            return False
        try:
            if not stat.S_ISREG(_lstat_at(TEST_MARKER, home_fd).st_mode):
                return None
        except FileNotFoundError:
            return None  # no marker: mid-creation, or not made by make_tmp_home
        lock = _open_at((LOCK_NAME,), home_fd)
        if lock is None:
            return None  # no lock file: made before #922
        fds.append(lock)
        if not _take(lock):
            return False  # held: a process uses the home
        broker = _open_at(BROKER_LOCK, home_fd)
        if broker is not None:
            fds.append(broker)
            if not _take(broker):
                return False  # a broker runs from the home, daemonized perhaps
        elif _lexists_at(BROKER_OUT, home_fd):
            return False  # `switchboard start` made broker.out: its daemon may not have its lock yet
        if has_live_socket(home_fd, str(root / name)):
            return False
        try:
            if not _same(before, os.stat(name, dir_fd=root_fd, follow_symlinks=False)):
                return False  # the name now is another directory: not the one locked
            _empty_home(home_fd)
            _rmdir_if_same(root_fd, name, before)
        except _NotThisHome:
            return False
        return True
    finally:
        for fd in reversed(fds):
            os.close(fd)  # the locks go with them, after the home is gone


def _take_sweep_lock(root_fd: int, root: Path) -> int | None:
    """An fd holding ``LOCK_EX`` on the root's ``SWEEP_LOCK``, or None if another sweep holds it.
    Raises if it is not a regular file of this user's (a symlink, a FIFO, another user's file)."""
    fd = os.open(SWEEP_LOCK, _OPEN_SWEEP_LOCK, 0o600, dir_fd=root_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise PermissionError(errno.EPERM, "not a regular file of this user's", str(root / SWEEP_LOCK))
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise


def sweep_killed_homes(root: Path = TMP_HOME_ROOT) -> SweepResult:
    """Remove the homes under ``root`` that a killed run left and nobody uses (module docstring),
    holding the root's ``SWEEP_LOCK``, or skip if another sweep holds it. Never raises an
    ``Exception``: one that a home causes is noted and the next home is checked."""
    result = SweepResult(root)
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except Exception as e:
        result.failed.append(f"{root}: {type(e).__name__}: {e}")
        return result
    sweep_lock = None
    try:
        try:
            sweep_lock = _take_sweep_lock(root_fd, root)
            if sweep_lock is None:
                result.skipped = True
                return result
            homes = _entries(root_fd)
        except Exception as e:
            result.failed.append(f"{root}: {type(e).__name__}: {e}")
            return result
        for name in homes:
            try:
                swept = _sweep_one(root_fd, root, name)
                if swept is not None:
                    (result.removed if swept else result.in_use).append(root / name)
            except Exception as e:
                result.failed.append(f"{root / name}: {type(e).__name__}: {e}")
        return result
    finally:
        if sweep_lock is not None:
            os.close(sweep_lock)
        os.close(root_fd)


def _report(write_line: Callable[[str], object], line: str) -> None:
    """Write ``line``, or else to stderr, or else nowhere: a report never stops the run."""
    try:
        write_line(line)
        return
    except Exception as e:
        line = f"{line} (the report failed: {type(e).__name__}: {e})"
    try:
        print(line, file=sys.stderr)
    except Exception:
        pass


def sweep_at_session_start(
    is_controller: bool, write_line: Callable[[str], object], root: Path = TMP_HOME_ROOT
) -> SweepResult | None:
    """The session-start sweep: once a run, in the controller (each xdist worker is a session of
    its own, and one sweep is enough), and nothing it or its report raises aborts the run. It writes
    one line when it found anything; a failed sweep and a failed report say which they were."""
    if not is_controller:
        return None
    try:
        result = sweep_killed_homes(root)
    except Exception as e:
        failed = f"the sweep failed, nothing removed: {type(e).__name__}: {e}"
        _report(write_line, f"switchboard test homes: {failed}")
        return None
    try:
        if result.removed or result.in_use or result.failed or result.skipped:
            _report(write_line, result.line())
    except Exception:
        pass
    return result
