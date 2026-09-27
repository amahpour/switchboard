"""Process inspection: pid -> (ppid, start time, uid), argv, ancestry, tty (DESIGN.md §5.3).

macOS: ``proc_pidinfo(PROC_PIDTBSDINFO)`` through ctypes. Linux: ``/proc``.
Fallback everywhere: ``ps``, always by absolute path (never through ``$PATH``,
which may come from an agent's shell). A process is identified by
``(pid, start)`` so a recycled pid never matches.
"""

from __future__ import annotations

import ctypes
import os
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from functools import lru_cache

_PROC_PIDTBSDINFO = 3
_BSDINFO_SIZE = 136
_NODEV = 0xFFFFFFFF

# The peer check walks every ancestor up to pid 1; this cap only stops runaway walks.
MAX_CHAIN = 64
_PS_CANDIDATES = ("/bin/ps", "/usr/bin/ps")


@lru_cache(maxsize=1)
def ps_bin() -> str | None:
    """Absolute path of ``ps``, or None. Never resolved through ``$PATH``."""
    for c in _PS_CANDIDATES:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _run_ps(args: list[str]) -> str:
    """stdout of ``ps <args>``, or "" if ps is missing or fails."""
    ps = ps_bin()
    if ps is None:
        return ""
    try:
        return subprocess.run(
            [ps, *args], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    start: float
    uid: int
    comm: str = ""
    tty_dev: int | None = None  # controlling tty device, None if none


# --------------------------------------------------------------------- macOS
_libproc = None
if sys.platform == "darwin":
    try:
        _libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        _libproc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        _libproc.proc_pidinfo.restype = ctypes.c_int
    except OSError:  # pragma: no cover
        _libproc = None


def _info_darwin(pid: int) -> ProcInfo | None:
    if _libproc is None:
        return None
    buf = ctypes.create_string_buffer(_BSDINFO_SIZE)
    n = _libproc.proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, buf, _BSDINFO_SIZE)
    if n != _BSDINFO_SIZE:
        return None
    raw = buf.raw
    rpid, ppid, uid = struct.unpack_from("<III", raw, 12)
    if rpid != pid:
        return None
    comm = raw[48:64].split(b"\0", 1)[0].decode("utf-8", "replace")
    (tdev,) = struct.unpack_from("<I", raw, 108)
    sec, usec = struct.unpack_from("<QQ", raw, 120)
    return ProcInfo(
        pid=pid,
        ppid=ppid,
        start=sec + usec / 1e6,
        uid=uid,
        comm=comm,
        tty_dev=None if tdev in (_NODEV, 0) else tdev,
    )


# --------------------------------------------------------------------- Linux
@lru_cache(maxsize=1)
def _linux_btime() -> float:
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("btime "):
                return float(line.split()[1])
    return 0.0


@lru_cache(maxsize=1)
def _linux_procfs() -> bool:
    return os.path.exists("/proc/self/stat")


def _info_linux(pid: int) -> ProcInfo | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read().decode("utf-8", "replace")
        uid = os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None
    lp, rp = data.find("("), data.rfind(")")
    comm = data[lp + 1 : rp]
    fields = data[rp + 2 :].split()
    # fields[0] is field 3 (state); ppid is field 4, tty_nr field 7, starttime field 22.
    if fields[0] in ("Z", "X", "x"):
        # A zombie (exited, not yet reaped by its parent) keeps its pid and start
        # time in /proc; it is not alive. (On macOS proc_pidinfo fails for one.)
        return None
    ppid = int(fields[1])
    tty_nr = int(fields[4])
    ticks = int(fields[19])
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    start = _linux_btime() + ticks / float(hz)
    return ProcInfo(
        pid=pid, ppid=ppid, start=round(start, 2), uid=uid, comm=comm,
        tty_dev=tty_nr or None,
    )


# ------------------------------------------------------------------ fallback
def _info_ps(pid: int) -> ProcInfo | None:
    out = _run_ps(["-o", "ppid=,uid=,lstart=,comm=", "-p", str(pid)]).strip()
    if not out:
        return None
    parts = out.split()
    try:
        ppid, uid = int(parts[0]), int(parts[1])
        start = time.mktime(time.strptime(" ".join(parts[2:7]), "%a %b %d %H:%M:%S %Y"))
    except (ValueError, IndexError):
        return None
    comm = os.path.basename(" ".join(parts[7:])) if len(parts) > 7 else ""
    return ProcInfo(pid=pid, ppid=ppid, start=start, uid=uid, comm=comm)


def info(pid: int) -> ProcInfo | None:
    if pid is None or pid <= 0:
        return None
    if sys.platform == "darwin":
        r = _info_darwin(pid)
    elif sys.platform.startswith("linux"):
        r = _info_linux(pid)
        if r is None and _linux_procfs():
            return None  # /proc is authoritative (ps reads it too): gone or a zombie
    else:  # pragma: no cover
        r = None
    return r if r is not None else _info_ps(pid)


def same_start(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) < 0.011


def alive(pid: int | None, start: float | None) -> bool:
    """The pid exists and started at ``start`` (so it isn't a recycled pid)."""
    if not pid:
        return False
    p = info(pid)
    return p is not None and same_start(p.start, start)


def ancestry(pid: int, depth: int = 8) -> list[ProcInfo]:
    """[pid itself, parent, grandparent, ...] up to ``depth`` entries (stops at 1).

    May be truncated. Security decisions use :func:`ancestry_to_root` instead.
    """
    chain: list[ProcInfo] = []
    seen: set[int] = set()
    cur = pid
    while cur and cur not in seen and len(chain) < depth:
        seen.add(cur)
        p = info(cur)
        if p is None:
            break
        chain.append(p)
        if p.ppid <= 1:
            break
        cur = p.ppid
    return chain


def ancestry_to_root(pid: int, cap: int = MAX_CHAIN) -> tuple[list[ProcInfo], bool]:
    """[pid itself, parent, ..., pid 1] and whether the walk reached the root.

    ``complete`` is False when a process in the chain vanished or could not be
    read, when the walk looped, or when ``cap`` entries were read without
    reaching a process whose parent is 0 (pid 1 / launchd / a container's
    init). Callers that decide trust must treat an incomplete chain as
    untrusted.
    """
    chain: list[ProcInfo] = []
    seen: set[int] = set()
    cur = pid
    while len(chain) < cap:
        if cur <= 0 or cur in seen:
            return chain, False
        seen.add(cur)
        p = info(cur)
        if p is None:
            return chain, False
        chain.append(p)
        if p.pid == 1 or p.ppid == 0:
            return chain, True
        cur = p.ppid
    return chain, False


_argv_cache: dict[tuple[int, float], str] = {}


def argv_many(procs: list[ProcInfo]) -> dict[int, str]:
    """argv of several processes: ``/proc/<pid>/cmdline`` on Linux, else one
    ``ps -ww -o pid=,args=`` call. Cached by (pid, start). "" means unknown."""
    out: dict[int, str] = {}
    need = []
    for p in procs:
        key = (p.pid, p.start)
        if key in _argv_cache:
            out[p.pid] = _argv_cache[key]
        else:
            need.append(p)
    if need:
        got: dict[int, str] = {}
        if sys.platform.startswith("linux"):
            for p in need:
                a = _argv_linux(p.pid)
                if a:
                    got[p.pid] = a
        missing = [p for p in need if p.pid not in got]
        res = ""
        if missing:
            res = _run_ps(["-ww", "-o", "pid=,args=", "-p", ",".join(str(p.pid) for p in missing)])
        for line in res.splitlines():
            line = line.strip()
            if not line:
                continue
            head, _, rest = line.partition(" ")
            try:
                got[int(head)] = rest.strip()
            except ValueError:
                continue
        for p in need:
            a = got.get(p.pid, "")
            # Re-check the start time: a pid that exited or was recycled
            # mid-call reads as "" (unknown), never as another process's argv.
            if a and not alive(p.pid, p.start):
                a = ""
            if a:
                if len(_argv_cache) > 4096:
                    _argv_cache.clear()
                _argv_cache[(p.pid, p.start)] = a
            out[p.pid] = a
    return out


def _argv_linux(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return ""
    return raw.rstrip(b"\0").replace(b"\0", b" ").decode("utf-8", "replace").strip()


def argv(pid: int, start: float) -> str:
    return argv_many([ProcInfo(pid=pid, ppid=0, start=start, uid=-1)]).get(pid, "")


def tty(pid: int) -> str | None:
    """The controlling terminal of ``pid`` (e.g. 'ttys003'), or None."""
    p = info(pid)
    if p is None:
        return None
    if p.tty_dev is None and sys.platform == "darwin":
        return None  # proc_pidinfo says there is no controlling tty
    out = _run_ps(["-o", "tty=", "-p", str(pid)]).strip()
    if not out or out.startswith("?"):
        return None
    return out
