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
  new session with ``close_fds``) doesn't inherit the test lock. The daemon takes ``LOCK_EX`` on
  ``BROKER_LOCK`` (``run/broker.lock``, ``Paths.lockfile``; conftest checks the two agree) right
  after ``paths.ensure()`` and holds it until it exits. The sweep takes it too, without waiting:
  held means in use.
- **Its sockets.** A home with any Unix socket under it that accepts a connection is in use. Only
  "refused" or "gone" counts as dead; any other answer (a full backlog, a timeout) keeps the home,
  and so does any error while looking (a directory it can't read, an entry it can't ``lstat``).

What it never uses, and why (the three review rounds of #920): a folder's mtime (a running broker
writes inside existing files and ``run/``), a pid in the marker (the maker isn't the home's only
user), and ``/proc/<pid>/fd`` (refused for a non-dumpable process of the same user, such as
ssh-agent, so it either removes nothing or misses a user). No file state tells a broker that is
restarting from one that stopped either (#922's fourth round: an old, unlocked ``run/broker.lock``
and ``logs/broker.out`` are both there in each case), so none is read.

**A broker that is starting.** ``daemon.start()`` returns only once the new broker's socket
answers, which is after the daemon holds ``run/broker.lock``, and the CLI process that runs it
holds the home's test lock the whole time (``SubprocBroker.cli`` and every test that runs
``switchboard start`` pass it, ``pass_fds``). So the handoff from the CLI's lock to the broker's is
covered on a first start and on a restart alike. The bounded exception: a daemon whose CLI is
killed before the daemon takes its broker lock belongs to a dead run, and its home may be swept.

A home is a candidate with both the lock file and the marker, as regular files, in a real
directory (not a symlink) owned by this user, directly under the root and named ``yk-*``. A
directory whose only entry is the lock file is removed too, once the sweep takes ``LOCK_EX`` on
that file without waiting (a test ``shutil.rmtree``-ing its own home still holds it): only a
creation or a removal killed part way leaves one. A directory holding only the marker (a pre-#922
home starts that way), an empty one, and any other directory are left. Nothing is ever chosen by
its name alone.

**Creation, removal and the sweep take one per-user lock.** ``SWEEP_LOCK`` is
``<root>/yk-sweep-<euid>.lock``, one per user, so one user's 0600 file never stops another's sweep.
It is opened through the root's fd with ``O_NOFOLLOW | O_CREAT``, mode 0600, and used only if
``fstat`` says it is a regular file owned by this euid (``_open_sweep_lock``, the same check on
every side). The sweep holds ``LOCK_EX | LOCK_NB`` on it for its whole pass, and skips the run if
it is held or unusable. ``make_locked_home()`` holds ``LOCK_SH`` on it from before ``mkdtemp``
until the marker is written; ``remove_home()`` holds ``LOCK_SH`` from before its first unlink until
its ``rmdir``. If the lock is unusable, creation and removal go on without it, and the sweep (which
makes the same check) skips, so a sweep never runs uncoordinated. While a sweep runs, then, no
coordinated code is part way through making or removing a home, and whatever such a state the
sweep finds was left by a process that died.

**One directory, removed in place.** The root is opened once, the home is opened through it with
``O_NOFOLLOW`` and its (dev, inode) noted, the locks are opened through the home's own fd, and the
identity is checked again after the locks and once more just before the final ``rmdir``; any
mismatch keeps the home. The removal (``_empty_home``, the same for the sweep and for
``remove_home()``, the ``tmp_home`` fixture's teardown) goes through the home's fd: every entry but
the marker and the lock file, then the marker, then the lock file, then the ``rmdir`` through the
root's fd. A removal interrupted before the marker goes leaves a candidate under its own name; one
interrupted after the marker leaves the lock file alone, which the sweep removes too. There is no
rename and no second name. **The bounded exception:** a removal killed between unlinking the lock
file and the ``rmdir`` leaves an empty directory, which the sweep leaves alone. It never removes
an empty directory: many tests remove their own homes with plain ``shutil.rmtree``, uncoordinated,
and their final ``rmdir`` would fail if a sweep took the empty directory first. (CONTRIBUTING.md,
"Sharing a machine", has the command that removes your empty ones.)

**The last stat-to-rmdir gap is bounded.** Between the final identity check and the ``rmdir``,
the name could in principle become another directory. ``rmdir`` removes only an empty directory;
/tmp is sticky, so only this user can rename the emptied home and put something else under its
name; and coordinated test code can't make or remove a home during a sweep. What remains is a
process of this user outside the tests doing exactly that in that instant, and the worst it costs
is one empty directory.

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
TMP_HOME_ROOT = Path("/tmp")
TMP_HOME_PREFIX = "yk-"
# A file directly under the root, never a home (homes are directories); one per user (module docstring).
SWEEP_LOCK = f"yk-sweep-{os.geteuid()}.lock"

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


def _unlink_at(name: str, dir_fd: int) -> None:
    """The marker or the lock file, unlinked (a seam for the tests that interrupt a removal)."""
    os.unlink(name, dir_fd=dir_fd)


def _rmdir_at(name: str, dir_fd: int) -> None:
    """The emptied home, removed (a seam for the tests that interrupt a removal)."""
    os.rmdir(name, dir_fd=dir_fd)


def _open_sweep_lock(root_fd: int) -> int | None:
    """An fd on the root's ``SWEEP_LOCK``, or None if it is unusable: it can't be opened (a symlink,
    a directory at its name) or ``fstat`` says it isn't a regular file owned by this euid. The one
    check creation, removal and the sweep all make (module docstring)."""
    try:
        fd = os.open(SWEEP_LOCK, _OPEN_SWEEP_LOCK, 0o600, dir_fd=root_fd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
        os.close(fd)
        return None
    return fd


def _shared_sweep_lock(root: Path) -> int | None:
    """``LOCK_SH`` on ``root``'s sweep lock, held until the fd returned is closed, so no sweep runs
    while this process makes or removes a home; None (go on without it) if it is unusable."""
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        fd = _open_sweep_lock(root_fd)
    finally:
        os.close(root_fd)
    if fd is None:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _close(fd: int | None) -> None:
    if fd is not None:
        os.close(fd)


def make_locked_home(root: Path = TMP_HOME_ROOT) -> Path:
    """A new home under ``root``, locked (``LOCK_SH``) by this process until ``release_home``.

    The order is the point: the lock file is created and locked before the marker exists, so a home
    with a marker always has a holder or had one. All of it happens under ``LOCK_SH`` on the root's
    sweep lock, so no sweep sees a home half made. A failure at any step closes the fd, forgets it
    and removes the directory before the error goes on."""
    release_removed_homes()
    sweep_lock = _shared_sweep_lock(root)
    try:
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
    finally:
        _close(sweep_lock)


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
            _unlink_at(name, home_fd)
        except FileNotFoundError:
            pass


def _rmdir_if_same(root_fd: int, name: str, before: os.stat_result) -> None:
    """``rmdir`` the emptied home, after checking once more that ``name`` is still that directory.
    The gap between the check and the ``rmdir`` is bounded (module docstring): ``rmdir`` takes only
    an empty directory, and only this user can put one under the name in a sticky /tmp."""
    if not _same(before, os.stat(name, dir_fd=root_fd, follow_symlinks=False)):
        raise _NotThisHome(name)
    _rmdir_at(name, root_fd)


def remove_home(home: Path) -> None:
    """Remove a ``make_locked_home`` home in place, through its own fd, the marker and the lock file
    last, then close this process's lock, all under ``LOCK_SH`` on the root's sweep lock. An error
    part way is not raised, as the ``shutil.rmtree(..., ignore_errors=True)`` this replaces did not;
    it leaves the home under its own name with the marker and the lock file (or the lock file alone),
    where ``no_homes_left_behind`` finds it and fails the run, and a later sweep removes it. A
    ``home`` that is not a real directory of this user's (a symlink a test left) is not touched."""
    home = Path(home)
    sweep_lock = _shared_sweep_lock(home.parent)
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
    finally:
        _close(sweep_lock)
    release_home(home)


@dataclass
class SweepResult:
    root: Path = TMP_HOME_ROOT
    removed: list[Path] = field(default_factory=list)
    in_use: list[Path] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: bool = False  # SWEEP_LOCK was held (another sweep, or a home being made or removed)
    unusable: bool = False  # SWEEP_LOCK is not a regular file of this user's: skipped too

    def line(self) -> str:
        if self.unusable:
            return (
                f"switchboard test homes: skipped, {self.root / SWEEP_LOCK} is not a regular file "
                "of this user's, nothing removed (#922)"
            )
        if self.skipped:
            return (
                f"switchboard test homes: skipped, {self.root / SWEEP_LOCK} is held (another sweep, "
                "or a home being made or removed), nothing removed (#922)"
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
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid()


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
            return _sweep_lock_only(root_fd, name, home_fd, before, fds)
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


def _sweep_lock_only(
    root_fd: int, name: str, home_fd: int, before: os.stat_result, fds: list[int]
) -> bool | None:
    """A directory with no marker: removed if its only entry is the lock file and the sweep takes
    ``LOCK_EX`` on it without waiting (only a creation or removal killed part way leaves one; a
    test ``shutil.rmtree``-ing its own home still holds it, and it is kept). Anything else, an empty
    directory included, is no candidate (module docstring)."""
    if os.listdir(home_fd) != [LOCK_NAME]:
        return None
    lock = _open_at((LOCK_NAME,), home_fd)
    if lock is None:
        return None
    fds.append(lock)
    if not _take(lock):
        return False  # held: its maker still removes it, or still uses it
    if os.listdir(home_fd) != [LOCK_NAME]:
        return False
    try:
        if not _same(before, os.stat(name, dir_fd=root_fd, follow_symlinks=False)):
            return False
        _unlink_at(LOCK_NAME, home_fd)
        _rmdir_if_same(root_fd, name, before)
    except _NotThisHome:
        return False
    return True


def _take_sweep_lock(root_fd: int) -> int | None:
    """An fd holding ``LOCK_EX`` on the root's ``SWEEP_LOCK``. Raises ``BlockingIOError`` if it is
    held, ``PermissionError`` if it is unusable (``_open_sweep_lock``)."""
    fd = _open_sweep_lock(root_fd)
    if fd is None:
        raise PermissionError(errno.EPERM, "not a regular file of this user's", SWEEP_LOCK)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def sweep_killed_homes(root: Path = TMP_HOME_ROOT) -> SweepResult:
    """Remove the homes under ``root`` that a killed run left and nobody uses (module docstring),
    holding the root's ``SWEEP_LOCK``, or skip if it is held or unusable. Never raises an
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
            sweep_lock = _take_sweep_lock(root_fd)
        except BlockingIOError:
            result.skipped = True
            return result
        except PermissionError:
            result.skipped = result.unusable = True
            return result
        try:
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
