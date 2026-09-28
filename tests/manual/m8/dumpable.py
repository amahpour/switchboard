"""What a same-uid process can do to another process, before and after
``prctl(PR_SET_DUMPABLE, 0)`` (DESIGN.md §27.4.8, the satellite's hardening).
Linux only. README.md here.

For each of two stand-in "satellites" (a child holding a pipe on its fd 1; the
second one makes itself non-dumpable first), this process, same uid, tries to:
write a forged frame into its stdout through ``/proc/<pid>/fd/1``, list its
fds, read its environ, and ``PTRACE_ATTACH`` to it. Prints one JSON object.

Run it in the Linux test image, no network and no capabilities (a plain
same-uid user, as a prompt-injected Pi agent would be):

    docker compose -f sandbox/compose.yaml build test
    docker run --rm --network none --cap-drop ALL --entrypoint /bin/sh switchboard-test \\
        -c '.venv/bin/python tests/manual/m8/dumpable.py'
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys

PR_SET_DUMPABLE = 4
PTRACE_ATTACH, PTRACE_DETACH = 16, 17

CHILD = """
import ctypes, sys, time
if {nodump}:
    assert ctypes.CDLL(None, use_errno=True).prctl({pr}, 0, 0, 0, 0) == 0
sys.stdout.write("ready\\n")
sys.stdout.flush()
time.sleep(10)
"""


def probe(nodump: bool) -> dict[str, str]:
    code = CHILD.format(nodump=nodump, pr=PR_SET_DUMPABLE)
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, env={"CANARY": "not-a-secret"})
    assert p.stdout is not None and p.stdout.readline() == b"ready\n"
    res: dict[str, str] = {}
    try:
        fd = os.open(f"/proc/{p.pid}/fd/1", os.O_WRONLY)
        os.write(fd, b'{"t":"forged"}\n')
        os.close(fd)
        res["write_to_its_stdout"] = "ok: " + p.stdout.readline().decode().strip()
    except OSError as e:
        res["write_to_its_stdout"] = f"refused: {e.strerror}"
    try:
        res["list_fds"] = f"ok: {len(os.listdir(f'/proc/{p.pid}/fd'))} fds"
    except OSError as e:
        res["list_fds"] = f"refused: {e.strerror}"
    try:
        with open(f"/proc/{p.pid}/environ", "rb") as f:
            res["read_environ"] = "ok: canary visible" if b"CANARY=" in f.read() else "ok"
    except OSError as e:
        res["read_environ"] = f"refused: {e.strerror}"
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
    r = libc.ptrace(PTRACE_ATTACH, p.pid, None, None)
    err = ctypes.get_errno()
    if r == 0:
        res["ptrace_attach"] = "ok"
        os.waitpid(p.pid, 0)  # the attach stops it; let it go again
        libc.ptrace(PTRACE_DETACH, p.pid, None, None)
    else:
        res["ptrace_attach"] = f"refused: {os.strerror(err)}"
    p.kill()
    p.wait()
    return res


def main() -> None:
    if not sys.platform.startswith("linux"):
        sys.exit("Linux only (it reads /proc); on macOS same-user task_for_pid is already refused")
    yama = "/proc/sys/kernel/yama/ptrace_scope"
    out = {
        "kernel": os.uname().release,
        "uid": os.getuid(),
        "yama_ptrace_scope": open(yama).read().strip() if os.path.exists(yama) else "absent",
        "default": probe(False),
        "after_prctl_dumpable_0": probe(True),
    }
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
