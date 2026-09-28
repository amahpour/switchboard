"""A user-level OpenSSH server on 127.0.0.1 for the ``ssh`` tier (DESIGN.md §27.13 T1).

``Sshd`` runs ``/usr/sbin/sshd -D -e -f <temp config>`` as this user on a free
loopback port, with a temp host key, its own ``AuthorizedKeysFile`` in the temp
dir, ``UsePAM no``, ``StrictModes no`` and ``PermitUserRC no``: never the system
sshd, never macOS Remote Login, never a system port, never ``~/.ssh``. Clients
in tests run ``/usr/bin/ssh -F /dev/null -o IdentityAgent=none -o
IdentitiesOnly=yes -o UserKnownHostsFile=<tmp>`` (``client_argv``); the link
itself uses the broker's production argv (``broker/remote.py`` ``ssh_argv``), with
the port from ``remotes.toml``.

A non-root sshd can only log in the user it runs as, which is all the tests need.
It still runs that user's login shell (``$SHELL -c <forced command>``), so the
user's shell start-up files are read, as on a real remote.
"""

from __future__ import annotations

import os
import pwd
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from conftest import child_env

SSHD_BIN = "/usr/sbin/sshd"
SSH_BIN = "/usr/bin/ssh"
SSH_KEYGEN_BIN = "/usr/bin/ssh-keygen"


def sshd_missing() -> str | None:
    """Why the ssh tier can't run here, or None."""
    for b in (SSHD_BIN, SSH_BIN, SSH_KEYGEN_BIN):
        if not os.access(b, os.X_OK):
            return f"no {b}"
    if os.getuid() == 0:
        return "a user-level sshd needs a non-root user"
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def keygen(path: Path, comment: str = "yk-test") -> str:
    """A new ed25519 key pair at ``path``; returns the public line ``ssh-ed25519 AAAA… comment``."""
    subprocess.run([SSH_KEYGEN_BIN, "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
                   check=True, stdin=subprocess.DEVNULL, capture_output=True, env=child_env(), timeout=30)
    return path.with_name(path.name + ".pub").read_text().strip()


def descendants(root: int) -> list[int]:
    """Every process below ``root`` (sshd's sessions live in their own sessions)."""
    out = subprocess.run(["/bin/ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=10,
                         env=child_env()).stdout
    kids: dict[int, list[int]] = {}
    for ln in out.splitlines():
        try:
            pid, ppid = (int(x) for x in ln.split())
        except ValueError:
            continue
        kids.setdefault(ppid, []).append(pid)
    found: list[int] = []
    todo = [root]
    while todo:
        for c in kids.get(todo.pop(), []):
            if c not in found:
                found.append(c)
                todo.append(c)
    return found


class Sshd:
    def __init__(self, *, stream_local_bind_unlink: bool = False, extra: tuple[str, ...] = ()):
        self.dir = Path(tempfile.mkdtemp(prefix="yk-ssh-", dir="/tmp"))
        os.chmod(self.dir, 0o700)
        self.user = pwd.getpwuid(os.getuid()).pw_name
        self.port = free_port()
        self.authorized_keys = self.dir / "authorized_keys"
        self.authorized_keys.touch(mode=0o600)
        self.log = self.dir / "sshd.log"
        self.cfg = self.dir / "sshd_config"
        self.stream_local_bind_unlink = stream_local_bind_unlink
        self.extra = extra
        self.proc: subprocess.Popen[bytes] | None = None
        self.host_key = self.dir / "host_ed25519"
        self.host_pub = " ".join(keygen(self.host_key, "yk-host").split()[:2])
        self._penalties = True

    # ------------------------------------------------------------- config
    def config_text(self) -> str:
        lines = [
            f"Port {self.port}", "ListenAddress 127.0.0.1", f"HostKey {self.host_key}",
            f"PidFile {self.dir / 'sshd.pid'}", f"AuthorizedKeysFile {self.authorized_keys}",
            "StrictModes no", "UsePAM no", "PasswordAuthentication no", "KbdInteractiveAuthentication no",
            "PubkeyAuthentication yes", "PermitUserRC no", "UseDNS no", "LogLevel VERBOSE",
            f"StreamLocalBindUnlink {'yes' if self.stream_local_bind_unlink else 'no'}",
            # forwarding stays on for the server, so a refusal shows the link key's `restrict`
            "AllowTcpForwarding yes", "AllowStreamLocalForwarding yes", "PermitTTY yes",
            *self.extra,
        ]
        if self._penalties:
            lines.append("PerSourcePenalties no")  # OpenSSH >= 9.8: no lock-out between the tests' failures
        return "\n".join(lines) + "\n"

    def _write_config(self) -> None:
        self.cfg.write_text(self.config_text())
        r = subprocess.run([SSHD_BIN, "-t", "-f", str(self.cfg)], capture_output=True, text=True, timeout=30,
                           env=child_env(), stdin=subprocess.DEVNULL)
        if r.returncode != 0 and self._penalties and "PerSourcePenalties" in (r.stderr + r.stdout):
            self._penalties = False  # an older sshd (Debian 12's 9.2) has no such option
            self.cfg.write_text(self.config_text())
            r = subprocess.run([SSHD_BIN, "-t", "-f", str(self.cfg)], capture_output=True, text=True, timeout=30,
                               env=child_env(), stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            raise RuntimeError(f"sshd -t refused the test config: {r.stderr.strip()}")

    # ---------------------------------------------------------- lifecycle
    def start(self) -> "Sshd":
        self._write_config()
        out = open(self.log, "ab")
        try:
            self.proc = subprocess.Popen([SSHD_BIN, "-D", "-e", "-f", str(self.cfg)], stdin=subprocess.DEVNULL,
                                         stdout=out, stderr=out, env=child_env(), start_new_session=True)
        finally:
            out.close()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                return self
            except OSError:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.05)
        raise RuntimeError(f"sshd did not start: {self.log_text()[-2000:]}")

    def sessions(self) -> list[int]:
        return descendants(self.proc.pid) if self.proc is not None and self.proc.poll() is None else []

    def stop(self, *, sessions: bool = True) -> None:
        """Stop the listener, and (``sessions``) every session it started (SIGCONT first, in
        case a test stopped one)."""
        if self.proc is None:
            return
        kids = self.sessions() if sessions else []
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        for pid in kids:
            for sig in (signal.SIGCONT, signal.SIGTERM):
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    break
        self.proc = None

    def new_host_key(self) -> str:
        """Replace the host key (a reinstalled remote); takes effect at the next start."""
        for p in (self.host_key, self.host_key.with_name(self.host_key.name + ".pub")):
            p.unlink(missing_ok=True)
        self.host_pub = " ".join(keygen(self.host_key, "yk-host-2").split()[:2])
        return self.host_pub

    def close(self) -> None:
        self.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self) -> "Sshd":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ helpers
    def log_text(self) -> str:
        try:
            return self.log.read_text(errors="replace")
        except OSError:
            return ""

    def known_hosts_line(self) -> str:
        """The line a first `ssh -p <port> user@127.0.0.1` would have added to known_hosts."""
        return f"[127.0.0.1]:{self.port} {self.host_pub}\n"

    def authorize(self, line: str) -> None:
        with open(self.authorized_keys, "a") as f:
            f.write(line.rstrip("\n") + "\n")

    def client_argv(self, key: Path, known_hosts: Path, *extra: str) -> list[str]:
        """A test client: no config, no agent, only ``key``, its own known_hosts."""
        return [SSH_BIN, "-F", "/dev/null", "-i", str(key), "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                "-o", f"UserKnownHostsFile={known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null",
                "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes", "-o", "LogLevel=ERROR",
                "-p", str(self.port), *extra, f"{self.user}@127.0.0.1"]
