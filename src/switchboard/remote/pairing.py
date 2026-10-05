"""Pairing a remote: ``switchboard remote add|accept|remove|doctor`` (DESIGN.md §27.5.8, §27.8).

- **add** (the desktop): resolves ``[user@]host`` once with ``ssh -G`` (the owner's
  ssh config is read here only, never by a link; ``ProxyJump``/``ProxyCommand``
  are refused), makes the link key ``remotes/<name>/id_ed25519`` with
  ``ssh-keygen``, pins the remote's host key from the owner's ``known_hosts``
  (``ssh-keygen -F``, hashed entries too) as ``switchboard-<name> <key>`` in
  ``remotes/<name>/known_hosts``, refusing when there is none, writes the
  ``[remote.<name>]`` table into ``remotes.toml`` (0600, atomically) and prints the
  pinned fingerprint and the one-line token for the remote. It never writes the
  desktop's ``~/.ssh`` and never uses the owner's keys or agent.
- **accept** (the remote): shows the exact ``authorized_keys`` line
  ``restrict[,from="…"],command="<python> -I -m switchboard satellite --home <home>
  --name <name>" ssh-ed25519 … switchboard-link <name>``, asks, then writes it
  atomically with a 0600 backup, and ``satellite.toml``. It refuses an editable
  install, a python or home path the forced command can't carry, and a key that
  is already there without ``command=`` (a shell key).
- **remove**: on the desktop, the broker ends the host's members and forgets its
  consent (``remote.remove``, human only), then the table and ``remotes/<name>/``
  go; on the remote, the ``authorized_keys`` line (diff, confirm, backup) and
  ``satellite.toml`` go, and a running satellite of this home is stopped.
- **doctor**: what a reviewer of either machine should know (see ``doctor_desktop``
  and ``doctor_remote``). The one network probe (``--probe-desktop``) is opt-in.

Only ``/usr/bin/ssh`` and ``/usr/bin/ssh-keygen`` are ever run (``_run``), with a
fixed argv, no shell and a clean environment. The satellite never imports this
module (``tests/unit/test_satellite_static.py``).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import ipaddress
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from switchboard.install.common import (
    InstallError,
    atomic_write,
    backup,
    check_path,
    confirm,
    editable_install,
)
from switchboard.models import valid_host
from switchboard.paths import Paths, ensure_private_dir, hook_state_text, test_mode_refusal
from switchboard.remote.config import (
    LABEL_RE,
    PIN_KEY_TYPES,
    SSH_BIN,
    SSH_KEYGEN_BIN,
    USER_RE,
    RemoteConfigError,
    _is_host,
    home_path_problem,
    host_key_alias,
    key_fingerprint,
    link_key_path,
    load_remotes,
    parse_remotes,
    pin_path,
    read_satellite_conf,
    remote_dir,
    remotes_path,
    ssh_files_problem,
    system_bin_problem,
    write_satellite_conf,
)

TOKEN_MAGIC = "switchboard-link"
TOKEN_VERSION = "v1"
KEY_TYPE = "ssh-ed25519"
# an ssh-ed25519 public key blob: string "ssh-ed25519", string <32 bytes>
_ED25519_HEAD = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
_ED25519_LEN = len(_ED25519_HEAD) + 32
# which pinned host key to prefer when known_hosts has several for the remote
PIN_PREFERENCE = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "ssh-rsa",
)
_KEY_TYPE_RE = re.compile(
    r"^(ssh-[a-z0-9-]+|ecdsa-sha2-nistp[0-9]+|sk-[a-z0-9-]+@openssh\.com"
    r"|[a-z0-9-]+-cert-v01@openssh\.com)$"
)
_DEST_RE = re.compile(r"^(?:([^@\s]+)@)?([^@\s]+)$")
RUN_TIMEOUT_S = 20.0
PROBE_TIMEOUT_S = 15.0
RRSYNC_PATHS = (
    "/usr/bin/rrsync",
    "/usr/local/bin/rrsync",
    "/usr/share/doc/rsync/scripts/rrsync",
    "/usr/share/doc/rsync/scripts/rrsync.gz",
)


class PairingError(Exception):
    """A refusal, with the message to print."""


# ------------------------------------------------------------------- running
def _run(
    argv: Sequence[str], timeout: float = RUN_TIMEOUT_S, agent_sock: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the system's ``ssh`` or ``ssh-keygen`` (nothing else, ever): a fixed argv, no
    shell, stdin from /dev/null, a clean env, the binary root-owned (as the link's).
    ``agent_sock``: only ``doctor --probe-desktop`` passes this session's agent, since
    what an agent here could do with it is what that probe asks."""
    if not argv or argv[0] not in (SSH_BIN, SSH_KEYGEN_BIN):
        raise PairingError(f"refusing to run {argv[:1]!r}")
    why = system_bin_problem(argv[0])
    if why:
        raise PairingError(f"can't use {argv[0]}: {why}")
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/"), "LANG": "C"}
    if agent_sock:
        env["SSH_AUTH_SOCK"] = agent_sock
    try:
        return subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd="/",
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise PairingError(f"{os.path.basename(argv[0])} took longer than {timeout:.0f} s") from None
    except OSError as e:
        raise PairingError(f"can't run {argv[0]}: {e.strerror or e}") from None


# --------------------------------------------------------------------- token
@dataclass(frozen=True)
class Token:
    """``switchboard-link v1 <name> <desktop label> ssh-ed25519 <key>``: what ``remote add``
    prints and ``remote accept`` takes. Only a public key: nothing in it is secret."""

    name: str
    desktop: str
    key: str  # the link key's base64 blob

    def text(self) -> str:
        return f"{TOKEN_MAGIC} {TOKEN_VERSION} {self.name} {self.desktop} {KEY_TYPE} {self.key}"

    @property
    def fingerprint(self) -> str:
        return key_fingerprint(f"{KEY_TYPE} {self.key}")


def check_ed25519(b64: str) -> str:
    try:
        blob = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise PairingError("the token's key is not base64") from None
    if len(blob) != _ED25519_LEN or not blob.startswith(_ED25519_HEAD):
        raise PairingError("the token's key is not an ssh-ed25519 public key")
    return b64


def parse_token(text: str) -> Token:
    words = text.strip().split()
    if len(words) < 2 or words[0] != TOKEN_MAGIC:
        raise PairingError(f"not a switchboard link token (it starts with '{TOKEN_MAGIC} {TOKEN_VERSION}')")
    if words[1] != TOKEN_VERSION:
        raise PairingError(
            f"token version {words[1][:12]!r} is not {TOKEN_VERSION}: install the same switchboard"
            " version on both machines"
        )
    if len(words) != 6:
        raise PairingError(
            "a link token has six words: switchboard-link v1 <name> <desktop> ssh-ed25519 <key>"
        )
    _m, _v, name, desktop, ktype, b64 = words
    if not valid_host(name):
        raise PairingError("the token's remote name is not valid (a-z first, then a-z, 0-9, '-'; at most 24)")
    if not LABEL_RE.fullmatch(desktop):
        raise PairingError("the token's desktop label is not valid")
    if ktype != KEY_TYPE:
        raise PairingError(f"the token's key type is {ktype[:32]!r}, not {KEY_TYPE}")
    return Token(name=name, desktop=desktop, key=check_ed25519(b64))


def desktop_label(raw: str | None = None) -> str:
    """A short label for this machine (for the remote's messages): ``--label``, or the first
    part of the host name, reduced to ``[A-Za-z0-9._-]``."""
    base = raw if raw is not None else socket.gethostname().split(".")[0]
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-._")[:64]
    return s if s and LABEL_RE.fullmatch(s) else "desktop"


# ------------------------------------------------------------ authorized_keys
@dataclass
class AkEntry:
    """One ``authorized_keys`` key line: its options, key type, key and comment."""

    options: list[tuple[str, str | None]]
    keytype: str
    key: str
    comment: str = ""

    def option(self, name: str) -> str | None:
        for k, v in self.options:
            if k.lower() == name:
                return v if v is not None else ""
        return None

    @property
    def has_command(self) -> bool:
        return self.option("command") is not None

    @property
    def fingerprint(self) -> str:
        return key_fingerprint(f"{self.keytype} {self.key}")


def _split_options(opts: str) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    i, n = 0, len(opts)
    while i < n:
        j = i
        while j < n and opts[j] not in ",=":
            j += 1
        name = opts[i:j]
        if j < n and opts[j] == "=":
            j += 1
            if j < n and opts[j] == '"':
                j += 1
                buf = []
                while j < n and opts[j] != '"':
                    if opts[j] == "\\" and j + 1 < n and opts[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    buf.append(opts[j])
                    j += 1
                j += 1  # the closing quote
                out.append((name, "".join(buf)))
            else:
                k = j
                while k < n and opts[k] != ",":
                    k += 1
                out.append((name, opts[j:k]))
                j = k
        else:
            out.append((name, None))
        if j < n and opts[j] == ",":
            j += 1
        i = j
    return out


def parse_ak_line(line: str) -> AkEntry | None:
    """A key line of ``authorized_keys`` (sshd(8) AUTHORIZED_KEYS FILE FORMAT), or None for a
    blank line, a comment or something unreadable."""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    first = s.split(None, 1)[0]
    opts = ""
    rest = s
    if not _KEY_TYPE_RE.fullmatch(first):
        # options run to the first blank outside double quotes
        inq, j = False, 0
        while j < len(s):
            c = s[j]
            if c == "\\" and inq and j + 1 < len(s):
                j += 2
                continue
            if c == '"':
                inq = not inq
            elif c in " \t" and not inq:
                break
            j += 1
        opts, rest = s[:j], s[j:].strip()
    parts = rest.split(None, 2)
    if len(parts) < 2 or not _KEY_TYPE_RE.fullmatch(parts[0]):
        return None
    return AkEntry(
        options=_split_options(opts),
        keytype=parts[0],
        key=parts[1],
        comment=parts[2] if len(parts) > 2 else "",
    )


def forced_command(python: str, home: str, name: str) -> str:
    """What the link key may run on the remote, and nothing else."""
    try:
        py = check_path(python, "python")
        hm = check_path(home, "home")
    except InstallError as e:
        raise PairingError(str(e)) from None
    if not valid_host(name):
        raise PairingError(f"not a remote name: {name!r}")
    return " ".join(
        [shlex.quote(py), "-I", "-m", "switchboard", "satellite", "--home", shlex.quote(hm), "--name", name]
    )


def check_from(raw: str | None) -> str | None:
    """``--from``: an address or network, or a comma list of them (sshd's ``from=``)."""
    if raw is None:
        return None
    out: list[str] = []
    for part in raw.split(","):
        p = part.strip()
        try:
            net = ipaddress.ip_network(p, strict=False)
        except ValueError:
            raise PairingError(
                f"--from takes IP addresses or networks (e.g. 192.0.2.10), not {p[:60]!r}"
            ) from None
        out.append(str(net.network_address) if net.num_addresses == 1 else str(net))
    if not out:
        raise PairingError("--from is empty")
    return ",".join(out)


def authorized_line(token: Token, python: str, home: str, from_: str | None = None) -> str:
    """The exact line ``remote accept`` writes (§27.4.3)."""
    opts = ["restrict"]
    if from_:
        opts.append(f'from="{from_}"')
    opts.append(f'command="{forced_command(python, home, token.name)}"')
    return f"{','.join(opts)} {KEY_TYPE} {token.key} {TOKEN_MAGIC} {token.name}"


def satellite_command(e: AkEntry) -> tuple[str, str, str] | None:
    """(python, home, name) of a line whose forced command starts a switchboard satellite."""
    cmd = e.option("command")
    if not cmd:
        return None
    try:
        words = shlex.split(cmd)
    except ValueError:
        return None
    # the design's rc-noise wrapper (`sh -c '...; exec <python> -I -m switchboard satellite ...'`) is read too
    if len(words) >= 3 and os.path.basename(words[0]) in ("sh", "bash", "dash") and words[1] == "-c":
        try:
            words = shlex.split(words[2].split("exec", 1)[-1])
        except ValueError:
            return None
    try:
        i = words.index("satellite")
    except ValueError:
        return None
    if i < 2 or words[i - 2 : i] != ["-m", "switchboard"]:
        return None
    rest = words[i + 1 :]
    home = name = ""
    for k, v in zip(rest, rest[1:], strict=False):
        if k == "--home":
            home = v
        elif k == "--name":
            name = v
    return (words[0], home, name) if name else None


def _same_home(a: str, b: str | os.PathLike[str]) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def is_ours(e: AkEntry, name: str, home: str | os.PathLike[str]) -> bool:
    """A line ``remote accept`` wrote in satellite home ``home`` for remote ``name``: a
    forced command that starts that remote's satellite in this home, or, for a line
    whose command isn't a satellite's, its comment. Another home's satellite of the
    same name (two desktops on one account) is never ours."""
    sat = satellite_command(e)
    if sat is not None:
        return sat[2] == name and _same_home(sat[1], home)
    return e.comment.strip() == f"{TOKEN_MAGIC} {name}"


def line_problem(e: AkEntry, python: str, home: str | os.PathLike[str], name: str) -> str | None:
    """Why ``e`` is not exactly a line ``remote accept`` writes, or None: ``restrict``, an
    optional ``from=``, and ``command=`` the satellite's forced command, in that order, and
    nothing else. An option after ``restrict`` switches a feature back on (a forward, a
    pty, ``user-rc``, ``environment=``), and a command that wraps the satellite runs
    other code first."""
    try:
        want_cmd = forced_command(python, str(home), name)
    except PairingError as x:
        return f"its forced command can't be what accept writes ({x})"
    got = [(k.lower(), v) for k, v in e.options]
    frm = [v for k, v in got if k == "from"]
    want = [("restrict", None)] + ([("from", frm[0])] if len(frm) == 1 else []) + [("command", want_cmd)]
    if got == want:
        return None
    extra = sorted({k for k, _v in got if k not in ("restrict", "from", "command")})
    if extra:
        return f"it has options beyond restrict, from= and command= ({', '.join(extra)[:120]})"
    if ("restrict", None) not in got:
        return "it lacks `restrict`"
    if ("command", want_cmd) not in got:
        return "its command= is not exactly the satellite's (something else runs first)"
    return "its options are not restrict[,from=],command= in that order"


def plan_accept(before: str, token: Token, line: str, home: str | os.PathLike[str]) -> tuple[str, list[str]]:
    """``authorized_keys`` with the link line in place: an older line for this remote in
    this home (or for this key) is replaced where it stands, else the line is appended.
    Returns the new text and the lines it replaces. Refuses a key already there without
    ``command=``."""
    lines = before.splitlines(keepends=True)
    out: list[str] = []
    replaced: list[str] = []
    placed = False
    for raw in lines:
        e = parse_ak_line(raw)
        if e is not None and e.key == token.key and not e.has_command:
            raise PairingError(
                "this key is already in authorized_keys without command= (it opens a shell): remove"
                " that line by hand first; a link key must only start the satellite"
            )
        if e is not None and (e.key == token.key or is_ours(e, token.name, home)):
            replaced.append(raw.rstrip("\r\n"))
            if not placed:
                out.append(line + ("\r\n" if raw.endswith("\r\n") else "\n"))
                placed = True
            continue
        out.append(raw)
    if not placed:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(line + "\n")
    after = "".join(out)
    if after == before:
        return before, []
    return after, [r for r in replaced if r != line]


def plan_remove(before: str, name: str, home: str | os.PathLike[str]) -> tuple[str, list[str]]:
    """``authorized_keys`` without remote ``name``'s link line(s) of satellite home ``home``;
    the other lines byte for byte."""
    out: list[str] = []
    removed: list[str] = []
    for raw in before.splitlines(keepends=True):
        e = parse_ak_line(raw)
        if e is not None and is_ours(e, name, home):
            removed.append(raw.rstrip("\r\n"))
            continue
        out.append(raw)
    return "".join(out), removed


def read_text(path: Path) -> str | None:
    """A file's text exactly (no newline translation: a CRLF file stays CRLF), or None."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        raise PairingError(f"{path} is not UTF-8 text") from None


def _passwd_home() -> str:
    import pwd

    try:
        return pwd.getpwuid(os.getuid()).pw_dir or ""
    except KeyError:
        return ""


def default_authorized_keys() -> Path:
    """``.ssh/authorized_keys`` under this user's home in the password database, the one
    sshd reads. When ``$HOME`` points elsewhere (``sudo -E``, a custom HOME, a test),
    refuse to guess: a line written where sshd never looks would fail as ``auth``."""
    pw = _passwd_home()
    home = os.path.expanduser("~")
    if not pw or os.path.realpath(home) != os.path.realpath(pw):
        raise PairingError(
            f"$HOME ({home}) is not your home in the password database ({pw or '?'}), where sshd"
            " reads authorized_keys: pass --authorized-keys <file>"
        )
    return Path(pw) / ".ssh" / "authorized_keys"


def host_key_fingerprints(dirpath: str = "/etc/ssh") -> list[str]:
    """This machine's sshd host keys, as fingerprints (for the owner to compare with what
    ``remote add`` pinned on the desktop)."""
    out: list[str] = []
    try:
        names = sorted(os.listdir(dirpath))
    except OSError:
        return out
    for n in names:
        if n.startswith("ssh_host_") and n.endswith("_key.pub"):
            try:
                words = (Path(dirpath) / n).read_text(encoding="utf-8").split()
            except (OSError, UnicodeDecodeError):
                continue
            fp = key_fingerprint(" ".join(words[:2])) if len(words) >= 2 else ""
            if fp:
                out.append(f"{fp} ({words[0]})")
    return out


# --------------------------------------------------------------------- accept
def _home_problem(paths: Paths, name: str, ping: Callable[[Path], dict[str, Any] | None]) -> str | None:
    """Why this home can't become remote ``name``'s satellite home, or None."""
    try:
        conf = read_satellite_conf(paths)
    except FileNotFoundError:
        conf = None
    except (OSError, RemoteConfigError) as e:
        return f"satellite.toml: {e}"
    if conf is not None and conf.name != name:
        return (
            f"this home is already {conf.name}'s satellite home: run `switchboard remote remove {conf.name}`"
            " here first, or use another --home"
        )
    try:
        if load_remotes(paths, test_mode=True):
            return "this home dials remotes (remotes.toml): it is a desktop home; use another --home"
    except RemoteConfigError:
        return "this home has a remotes.toml: it is a desktop home; use another --home"
    info = ping(paths.sock)
    if info is not None and info.get("role") != "satellite":
        return (
            "a switchboard broker runs from this home: a satellite home has none (the broker runs on the"
            " desktop); stop it, or use another --home"
        )
    return None


def accept(
    paths: Paths,
    token_text: str,
    *,
    from_: str | None = None,
    ak_path: Path | None = None,
    yes: bool = False,
    allow_editable: bool = False,
    python: str | None = None,
    stdin: TextIO | None = None,
    out: TextIO | None = None,
    ping: Callable[[Path], dict[str, Any] | None] | None = None,
) -> int:
    """``switchboard remote accept '<token>'`` on the remote (§27.8.2)."""
    out = out or sys.stdout
    token = parse_token(token_text)
    py = python or sys.executable
    frm = check_from(from_)
    line = authorized_line(token, py, str(paths.home), frm)
    if editable_install():
        if not allow_editable:
            raise PairingError(
                "refusing an editable/source install: the forced command would run code an agent"
                " here could edit. Install a copy first (`uv tool install …`)"
            )
        why = test_mode_refusal(paths, True)
        if why:  # the override is for switchboard's own tests (their venv is editable), never a real home
            raise PairingError(
                f"--allow-editable is only for switchboard's own tests: {why.replace('--test-mode ', '')}"
            )
    if ping is None:
        from switchboard.mcp.client import ping as ping_sock

        ping = ping_sock
    why = _home_problem(paths, token.name, ping)
    if why:
        raise PairingError(why)
    ak = ak_path or default_authorized_keys()
    before = read_text(ak)
    after, replaced = plan_accept(before or "", token, line, paths.home)
    print(f"switchboard remote accept {token.name} (from the desktop {token.desktop}):", file=out)
    print(f"  link key {token.fingerprint}", file=out)
    print(f"  {ak}:", file=out)
    if after == (before or "") and before is not None:
        print("    no changes (the line is already there)", file=out)
    else:
        for r in replaced:
            print(f"    - {r}", file=out)
        print(f"    + {line}", file=out)
    print(f"  {paths.satellite_conf}: name {token.name}, desktop {token.desktop}", file=out)
    if not confirm(yes, stdin, out):
        print("not applied", file=out)
        return 1
    paths.ensure()
    write_satellite_conf(paths, token.name, desktop=token.desktop, key_fp=token.fingerprint)
    if before is None or after != before:
        if before is not None:
            b = backup(ak)
            if b is not None:
                print(f"backup: {b}", file=out)
        atomic_write(ak, after, default_mode=0o600)
        if before is None:
            os.chmod(ak, 0o600)
        print(f"wrote {ak}", file=out)
    fps = host_key_fingerprints()
    if fps:
        print(
            "this machine's host keys (compare with the fingerprint `remote add` pinned on the desktop):",
            file=out,
        )
        for fp in fps:
            print(f"  {fp}", file=out)
    else:
        print(
            "no host keys found in /etc/ssh to show; compare them by hand with what `remote add` pinned",
            file=out,
        )
    print(f"next, on the desktop ({token.desktop}): switchboard remote enable {token.name}", file=out)
    return 0


# ------------------------------------------------------------------------ add
@dataclass
class Resolved:
    """What ``ssh -G`` says about a destination."""

    hostname: str
    user: str
    port: int
    hostkeyalias: str | None = None
    known_hosts: list[str] = field(default_factory=list)
    proxy: str | None = None  # a ProxyJump or ProxyCommand that is set


def split_dest(dest: str) -> tuple[str | None, str]:
    m = _DEST_RE.fullmatch(dest)
    if not m or dest.startswith("-"):
        raise PairingError("the destination is [user@]host")
    user, host = m.group(1), m.group(2)
    if user is not None and not USER_RE.fullmatch(user):
        raise PairingError("the user must be a login name (a-z, 0-9, '_', '-')")
    if not _is_host(host):
        raise PairingError("the host must be a host name or an IP address")
    return user, host


def parse_ssh_g(text: str) -> Resolved:
    vals: dict[str, str] = {}
    for ln in text.splitlines():
        k, _, v = ln.strip().partition(" ")
        if k and k.lower() not in vals:
            vals[k.lower()] = v.strip()
    try:
        port = int(vals.get("port", "22"))
    except ValueError:
        port = -1
    proxy: str | None = None
    for k in ("proxyjump", "proxycommand"):
        ssh_value = vals.get(k)
        if ssh_value and ssh_value.lower() != "none":
            proxy = f"{k} {ssh_value}"
    alias = vals.get("hostkeyalias")
    files: list[str] = []
    for k in ("userknownhostsfile", "globalknownhostsfile"):
        files += [f for f in vals.get(k, "").split() if f and f != "none"]
    return Resolved(
        hostname=vals.get("hostname", ""),
        user=vals.get("user", ""),
        port=port,
        hostkeyalias=alias if alias and alias.lower() != "none" else None,
        known_hosts=files,
        proxy=proxy,
    )


def resolve(host: str, user: str | None, port: int | None, ssh_config: str | None) -> Resolved:
    argv = [SSH_BIN, "-G"]
    if ssh_config:
        argv += ["-F", ssh_config]
    if port:
        argv += ["-p", str(port)]
    if user:
        argv += ["-l", user]
    argv.append(host)
    r = _run(argv)
    if r.returncode != 0:
        raise PairingError(f"ssh -G {host} failed: {(r.stderr or '').strip()[:300]}")
    res = parse_ssh_g(r.stdout)
    if res.proxy:
        raise PairingError(
            f"your ssh config reaches {host} through {res.proxy}: a link dials directly, never"
            " through a jump host or proxy command (§27.4.1); give the remote's own address"
        )
    if not _is_host(res.hostname):
        raise PairingError(f"ssh -G gave the host name {res.hostname[:80]!r}, which a link can't use")
    if not USER_RE.fullmatch(res.user):
        raise PairingError(f"ssh -G gave the user {res.user[:40]!r}: give a login name as user@host")
    if not 1 <= res.port <= 65535:
        raise PairingError("ssh -G gave no valid port")
    return res


def known_hosts_name(res: Resolved) -> str:
    """The name the owner's known_hosts files hold the remote's key under."""
    if res.hostkeyalias:
        return res.hostkeyalias
    return res.hostname if res.port == 22 else f"[{res.hostname}]:{res.port}"


def find_pin(lookup: str, files: Sequence[str]) -> tuple[str, str, str] | None:
    """(key type, key, file) of the remote's host key in the first known_hosts file that has
    one (``ssh-keygen -F``: hashed entries too), preferring ed25519; None if none has."""
    for f in files:
        if not os.path.isfile(f):
            continue
        r = _run([SSH_KEYGEN_BIN, "-F", lookup, "-f", f])
        if r.returncode != 0:
            continue
        keys: list[tuple[str, str]] = []
        revoked: set[str] = set()
        for ln in r.stdout.splitlines():
            w = ln.split()
            if not w or w[0].startswith("#"):
                continue
            if w[0].startswith("@"):
                if w[0] == "@revoked" and len(w) >= 4:
                    revoked.add(w[3])
                continue  # a CA or a revocation is not a pin
            if len(w) >= 3 and PIN_KEY_TYPES.fullmatch(w[1]) and key_fingerprint(f"{w[1]} {w[2]}"):
                keys.append((w[1], w[2]))  # a plain host key (a certificate is not a pin)
        keys = [k for k in keys if k[1] not in revoked]
        if keys:
            rank = {t: i for i, t in enumerate(PIN_PREFERENCE)}
            kt, kb = min(keys, key=lambda k: rank.get(k[0], len(rank)))
            return kt, kb, f
    return None


def entry_text(name: str, res: Resolved, rooms: list[str], harnesses: list[str] | None) -> str:
    import json

    lines = [
        f"[remote.{name}]",
        f"host = {json.dumps(res.hostname)}",
        f"user = {json.dumps(res.user)}",
        f"port = {res.port}",
        "rooms = [" + ", ".join(json.dumps(r) for r in rooms) + "]",
    ]
    if harnesses is not None:
        lines.append("harnesses = [" + ", ".join(json.dumps(h) for h in harnesses) + "]")
    return "\n".join(lines) + "\n"


def unrestricted_entries(text: str | None) -> list[AkEntry]:
    """Key lines without ``command=``: each opens a shell for whoever holds the key."""
    return [
        e
        for e in (parse_ak_line(ln) for ln in (text or "").splitlines())
        if e is not None and not e.has_command
    ]


def _describe_key(e: AkEntry) -> str:
    c = re.sub(r"[^\x20-\x7e]", "?", e.comment.strip())[:60]
    return f"{e.fingerprint or e.keytype}{f' ({c})' if c else ''}"


def add(
    paths: Paths,
    name: str,
    dest: str,
    *,
    rooms: Sequence[str] | None = None,
    port: int | None = None,
    harnesses: Sequence[str] | None = None,
    ssh_config: str | None = None,
    known_hosts: str | None = None,
    authorized_keys: Path | None = None,
    label: str | None = None,
    out: TextIO | None = None,
) -> int:
    """``switchboard remote add <name> <[user@]host> --rooms …`` on the desktop (§27.8.1)."""
    out = out or sys.stdout
    if not valid_host(name):
        raise PairingError("remote names look like fpga-pi (a-z first, then a-z, 0-9, '-'; at most 24)")
    user, host = split_dest(dest)
    if port is not None and not 1 <= port <= 65535:
        raise PairingError("--port must be 1..65535")
    room_list = [r.strip() for spec in (rooms or []) for r in spec.split(",") if r.strip()] or ["*"]
    hs = None
    if harnesses:
        hs = [h.strip() for spec in harnesses for h in spec.split(",") if h.strip()]
    try:
        existing = load_remotes(paths, test_mode=True)
    except RemoteConfigError as e:
        raise PairingError(f"fix remotes.toml first: {e}") from None
    if name in existing:
        raise PairingError(f"{name} is already in remotes.toml: `switchboard remote remove {name}` first")
    why = home_path_problem(paths)
    if why:
        raise PairingError(why)
    d = remote_dir(paths, name)
    if os.path.lexists(d):
        raise PairingError(f"{d} already exists: `switchboard remote remove {name}` first")
    cfg = os.path.abspath(ssh_config) if ssh_config else None
    kh = os.path.abspath(known_hosts) if known_hosts else None
    res = resolve(host, user, port, cfg)
    text = entry_text(name, res, room_list, hs)
    try:
        entry = parse_remotes(text, test_mode=False)[name]
    except RemoteConfigError as e:
        raise PairingError(str(e)) from None
    lookup = known_hosts_name(res)
    files = [kh] if kh else res.known_hosts
    pin = find_pin(lookup, files)
    if pin is None:
        raise PairingError(
            f"no host key for {lookup} in {', '.join(files) or 'any known_hosts file'}:"
            " ssh to it once by hand"
            f" (ssh -p {res.port} {res.user}@{res.hostname} true), check the fingerprint it shows against the"
            " remote's own (ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub there), then run this again"
        )
    ktype, kblob, kfile = pin
    lab = desktop_label(label)
    base = paths.home / "remotes"
    paths.ensure()
    ensure_private_dir(base)
    ensure_private_dir(d)
    try:
        key = link_key_path(paths, name)
        r = _run(
            [
                SSH_KEYGEN_BIN,
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                f"{TOKEN_MAGIC} {name}@{lab}",
                "-f",
                str(key),
            ]
        )
        pub = key.with_name(key.name + ".pub")
        if r.returncode != 0 or not key.is_file() or not pub.is_file():
            raise PairingError(f"ssh-keygen failed: {(r.stderr or '').strip()[:300]}")
        os.chmod(key, 0o600)
        os.chmod(pub, 0o600)
        words = pub.read_text(encoding="utf-8").split()
        token = Token(name=name, desktop=lab, key=check_ed25519(words[1]))
        fd = os.open(pin_path(paths, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{host_key_alias(name)} {ktype} {kblob}\n")
        rp = remotes_path(paths)
        old = read_text(rp) or ""
        sep = "" if not old or old.endswith("\n\n") else ("\n" if old.endswith("\n") else "\n\n")
        new = old + sep + text
        if name not in parse_remotes(new, test_mode=True):
            raise PairingError("remotes.toml would not hold the new table")
        atomic_write(rp, new, default_mode=0o600)
        os.chmod(rp, 0o600)
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    problem = ssh_files_problem(paths, name)
    if problem:  # pragma: no cover - just written
        raise PairingError(problem)
    shown = "any room" if "*" in entry.rooms else "rooms " + ", ".join(entry.rooms)
    print(f"added remote {name}: {entry.user}@{entry.host} port {entry.port}, {shown}", file=out)
    print(
        f"pinned its host key {key_fingerprint(f'{ktype} {kblob}')} ({ktype}, from {kfile}):"
        " compare it with what `remote accept` prints there",
        file=out,
    )
    print(f"link key {token.fingerprint} ({link_key_path(paths, name)})", file=out)
    print("", file=out)
    print("On the remote, run:", file=out)
    print(f"  switchboard remote accept '{token.text()}'", file=out)
    print(f"then here: switchboard remote enable {name}", file=out)
    try:
        ak = authorized_keys or default_authorized_keys()
        loose = unrestricted_entries(read_text(ak))
    except PairingError as e:
        ak, loose = None, []
        print(f"note: this machine's authorized_keys not checked ({e})", file=out)
    if loose:
        print("", file=out)
        print(
            f"warning: {ak} lets {len(loose)} key(s) open a shell on this machine (no command=). Never give"
            " a remote machine such a key, and never `ssh -A` into it: an agent there could then act as you"
            " here (`switchboard remote doctor` lists them).",
            file=out,
        )
    return 0


# --------------------------------------------------------------------- remove
_HEADER_RE = re.compile(r'^\s*\[\s*remote\s*\.\s*(?:"([^"]*)"|([A-Za-z0-9_-]+))\s*\]\s*(?:#.*)?$')
_TABLE_RE = re.compile(r"^\s*\[\[?\s*[A-Za-z0-9_\"'.\s-]+\]\]?\s*(?:#.*)?$")


def remove_table(text: str, name: str) -> str:
    """``remotes.toml`` without ``[remote.<name>]``; every other table exactly as it was
    (checked by parsing both). Raises PairingError when that can't be done by lines."""
    out: list[str] = []
    skipping = False
    for ln in text.splitlines(keepends=True):
        bare = ln.rstrip("\r\n")
        m = _HEADER_RE.match(bare)
        if m:
            skipping = (m.group(1) or m.group(2)) == name
        elif _TABLE_RE.match(bare):
            skipping = False
        if not skipping:
            out.append(ln)
    new = "".join(out)
    try:
        before = parse_remotes(text, test_mode=True)
        after = parse_remotes(new, test_mode=True)
    except RemoteConfigError as e:
        raise PairingError(f"remotes.toml: {e}") from None
    want = {k: v for k, v in before.items() if k != name}
    if after != want:
        raise PairingError(
            f"couldn't take [remote.{name}] out of remotes.toml line by line: edit it by hand,"
            " then run this again"
        )
    return new


def _delete_remote_dir(d: Path) -> None:
    """``remotes/<name>/``: a real directory of ours (never a link), its files, then itself."""
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise PairingError(f"{d} is not a directory of yours: remove it by hand")
    shutil.rmtree(d)


def remove_desktop(
    paths: Paths,
    name: str,
    *,
    yes: bool = False,
    call: Callable[..., dict[str, Any]],
    stdin: TextIO | None = None,
    out: TextIO | None = None,
) -> int:
    """``switchboard remote remove <name>`` on the desktop: the broker ends the host's members
    and drops its consent (human only), then the table and ``remotes/<name>/`` go."""
    from switchboard.mcp.client import BrokerDown

    out = out or sys.stdout
    if not valid_host(name):
        raise PairingError("remote names look like fpga-pi")
    rp = remotes_path(paths)
    text = read_text(rp)
    try:
        entries = parse_remotes(text or "", test_mode=True)
    except RemoteConfigError as e:
        raise PairingError(f"remotes.toml: {e}") from None
    d = remote_dir(paths, name)
    has_dir = os.path.lexists(d)
    new = remove_table(text, name) if name in entries and text is not None else None
    print(f"switchboard remote remove {name}:", file=out)
    print("  the broker ends its members and forgets its consent (if it runs)", file=out)
    if new is not None:
        print(f"  - [remote.{name}] from {rp}", file=out)
    if has_dir:
        print(f"  - {d}/ (its link key and pinned host key)", file=out)
    if not confirm(yes, stdin, out):
        print("not applied", file=out)
        return 1
    try:
        res = call("remote.remove", {"name": name})
        n = res.get("ended", 0)
        print(
            f"broker: link closed, {n} member(s) ended, consent "
            f"{'forgotten' if res.get('had_row') else 'none'}",
            file=out,
        )
    except BrokerDown:
        print(
            "the broker is not running: members of this remote left from before are ended when it starts",
            file=out,
        )
    if new is not None:
        atomic_write(rp, new, default_mode=0o600)
    if has_dir:
        _delete_remote_dir(d)
    print(f"removed {name}. On the remote, run: switchboard remote remove {name}", file=out)
    return 0


def _stop_satellite(paths: Paths) -> int | None:
    """SIGTERM this home's running satellite (it ends its link with ``bye``); its pid or None."""
    from switchboard.remote.satellite import _is_satellite, _read_pair

    pair = _read_pair(paths.run_dir / "satellite.pid")
    if pair is None:
        return None
    pid, start = pair
    if pid == os.getpid() or not _is_satellite(pid, start):
        return None
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return None
    return pid


def remove_remote(
    paths: Paths,
    name: str,
    *,
    ak_path: Path | None = None,
    yes: bool = False,
    stdin: TextIO | None = None,
    out: TextIO | None = None,
) -> int:
    """``switchboard remote remove <name>`` on the remote: the link line (diff, confirm,
    backup), ``satellite.toml``, and a running satellite of this home."""
    out = out or sys.stdout
    try:
        conf = read_satellite_conf(paths)
    except (OSError, RemoteConfigError) as e:
        raise PairingError(f"satellite.toml: {e}") from None
    if conf.name != name:
        raise PairingError(f"this home is {conf.name}'s satellite home, not {name}'s")
    ak = ak_path or default_authorized_keys()
    before = read_text(ak)
    after, removed = plan_remove(before or "", name, paths.home)
    print(f"switchboard remote remove {name}:", file=out)
    print(f"  {ak}:", file=out)
    if removed:
        for r in removed:
            print(f"    - {r}", file=out)
    else:
        print("    no line for this remote", file=out)
    print(f"  - {paths.satellite_conf}", file=out)
    if not confirm(yes, stdin, out):
        print("not applied", file=out)
        return 1
    if removed and before is not None:
        b = backup(ak)
        if b is not None:
            print(f"backup: {b}", file=out)
        atomic_write(ak, after, default_mode=0o600)
        print(f"wrote {ak}", file=out)
    try:
        paths.satellite_conf.unlink()
    except FileNotFoundError:
        pass
    pid = _stop_satellite(paths)
    if pid is not None:
        print(f"stopped the running satellite (pid {pid})", file=out)
    print(f"removed {name} here. On the desktop: switchboard remote remove {name}", file=out)
    return 0


# --------------------------------------------------------------------- doctor
@dataclass
class Finding:
    level: str  # ok, note, WARN, FAIL
    text: str


def _rrsync() -> str | None:
    for p in RRSYNC_PATHS:
        if os.path.isfile(p):
            return p
    return shutil.which("rrsync", path="/usr/bin:/bin:/usr/local/bin")


def own_public_keys(ssh_dir: Path, paths: Paths) -> dict[str, str]:
    """{key blob: where} for this machine's own public keys (``~/.ssh/*.pub``) and the link
    keys of every remote."""
    out: dict[str, str] = {}
    cands: list[Path] = []
    with contextlib.suppress(OSError):
        cands += sorted(p for p in ssh_dir.glob("*.pub") if p.is_file())
    with contextlib.suppress(OSError):
        cands += sorted((paths.home / "remotes").glob("*/id_ed25519.pub"))
    for p in cands:
        try:
            words = p.read_text(encoding="utf-8").split()
        except (OSError, UnicodeDecodeError):
            continue
        if len(words) >= 2:
            out.setdefault(words[1], str(p))
    return out


def doctor_desktop(
    paths: Paths,
    *,
    ak_path: Path,
    ssh_dir: Path,
    allow_ssh_cli: bool,
    status: Callable[[], dict[str, Any] | None],
) -> list[Finding]:
    """The desktop's checks (§27.8.3): links, key and pin files, ``allow_ssh_cli``, shell keys."""
    from switchboard.remote.describe import describe

    f: list[Finding] = []
    rp = remotes_path(paths)
    try:
        entries = load_remotes(paths, test_mode=True)
    except RemoteConfigError as e:
        entries = {}
        f.append(Finding("FAIL", f"remotes.toml refused: {e}"))
    else:
        if not entries:
            f.append(Finding("note", "no remotes in remotes.toml (`switchboard remote add` pairs one)"))
        elif os.stat(rp).st_mode & 0o077:
            f.append(Finding("WARN", f"{rp} is readable by others (chmod 600)"))
    why = system_bin_problem(SSH_BIN)
    f.append(Finding("FAIL", f"{SSH_BIN}: {why}") if why else Finding("ok", f"{SSH_BIN} is root-owned"))
    for name, entry in sorted(entries.items()):
        if entry.transport != "ssh":
            continue
        problem = ssh_files_problem(paths, name)
        if problem:
            f.append(Finding("FAIL", f"{name}: {problem}"))
        else:
            f.append(
                Finding(
                    "ok",
                    f"{name}: remotes/{name}/ 0700, link key 0600, one host key pinned under"
                    f" {host_key_alias(name)}",
                )
            )
    st = status()
    if st is None:
        f.append(Finding("note", "the broker is not running: link states unknown"))
    else:
        if st.get("config_error"):
            f.append(Finding("FAIL", f"the broker refuses remotes.toml: {st['config_error']}"))
        for info in st.get("remotes", []):
            state = info.get("state")
            lvl = "ok" if state == "up" else "WARN" if state in ("blocked", "down") else "note"
            f.append(Finding(lvl, describe(info)))
    if allow_ssh_cli:
        f.append(
            Finding(
                "WARN",
                "[security] allow_ssh_cli = true: human commands (say, cmd, login) are accepted"
                " from ssh logins, for every key that opens a shell here",
            )
        )
    else:
        f.append(Finding("ok", "[security] allow_ssh_cli is off: human commands only from a terminal here"))
    try:
        text = read_text(ak_path)
    except PairingError as e:
        text = None
        f.append(Finding("WARN", str(e)))
    if text is None:
        f.append(Finding("ok", f"no {ak_path}: no key opens a shell on this machine"))
    else:
        own = own_public_keys(ssh_dir, paths)
        loose = keys = 0
        for key_entry in (parse_ak_line(ln) for ln in text.splitlines()):
            if key_entry is None:
                continue
            keys += 1
            if key_entry.key in own:
                f.append(
                    Finding(
                        "WARN",
                        f"{ak_path} authorizes a key this machine holds ({own[key_entry.key]}): any"
                        " process here that reads it can log in here over ssh",
                    )
                )
            if not key_entry.has_command:
                loose += 1
                f.append(
                    Finding(
                        "WARN",
                        f"{ak_path}: {_describe_key(key_entry)} opens a shell on this machine (no"
                        " command=): never give it to a remote machine, never `ssh -A` into one",
                    )
                )
        if not keys:
            f.append(Finding("ok", f"{ak_path} holds no key: no key opens a shell on this machine"))
        elif not loose:
            f.append(Finding("ok", f"{ak_path}: every key is restricted to a command"))
    rr = _rrsync()
    f.append(
        Finding(
            "note",
            f"rrsync: {rr}"
            if rr
            else "rrsync not found (only the pull variant of the bitstream key needs it here, §27.8.4)",
        )
    )
    return f


def _writable(p: str) -> bool:
    try:
        return os.access(p, os.W_OK)
    except OSError:
        return False


def doctor_remote(
    paths: Paths,
    *,
    ak_path: Path,
    environ: Any,
    probe_desktop: str | None = None,
    status: Callable[[], dict[str, Any] | None],
) -> list[Finding]:
    """The remote's checks (§27.8.2): the forced command, ``satellite.toml``, the hook copy,
    an agent socket in this session, ``rrsync``, and, only when asked, whether this machine
    can open a shell on the desktop."""
    f: list[Finding] = []
    try:
        conf = read_satellite_conf(paths)
    except (OSError, RemoteConfigError) as e:
        f.append(Finding("FAIL", f"satellite.toml: {e}"))
        return f
    f.append(Finding("ok", f"satellite.toml: {conf.name}, dialed by the desktop {conf.desktop or '?'}"))
    try:
        text = read_text(ak_path) or ""
    except PairingError as e:
        text = ""
        f.append(Finding("FAIL", str(e)))
    lines = [entry for entry in (parse_ak_line(ln) for ln in text.splitlines()) if entry is not None]
    ours = [entry for entry in lines if is_ours(entry, conf.name, paths.home)]
    if not ours:
        f.append(
            Finding(
                "FAIL",
                f"{ak_path} has no line for {conf.name} in this home: run `switchboard remote accept` again",
            )
        )
    for entry in ours:
        sat = satellite_command(entry)
        if sat is None:
            f.append(
                Finding(
                    "FAIL",
                    f"{conf.name}'s line has no satellite command: run `switchboard remote accept` again",
                )
            )
            continue
        py, _home, _n = sat
        if not (os.path.isfile(py) and os.access(py, os.X_OK)):
            f.append(
                Finding(
                    "FAIL",
                    f"the forced command's python {py} is missing: run `switchboard remote accept`"
                    " again (switchboard moved?)",
                )
            )
        else:
            f.append(Finding("ok", f"the forced command's python exists ({py}) and its home is this home"))
        why = line_problem(entry, py, paths.home, conf.name)
        f.append(
            Finding(
                "FAIL",
                f"{conf.name}'s line is not the one `remote accept` writes: {why}; run"
                " `switchboard remote accept` again",
            )
            if why
            else Finding("ok", f"{conf.name}'s line is exactly restrict[,from=],command=<the satellite>")
        )
        frm = entry.option("from")
        f.append(
            Finding("ok", f"the link key is accepted only from {frm}")
            if frm
            else Finding(
                "note",
                "the link key is accepted from any address: `remote accept --from <desktop ip>` restricts it",
            )
        )
        if any(x.key == entry.key and not x.has_command for x in lines):
            f.append(Finding("FAIL", f"{ak_path} also has the link key without command= (a shell)"))
    hooks = hook_state_text(paths)
    f.append(Finding("ok" if hooks.startswith("ok") else "WARN", f"hook copies: {hooks}"))
    if environ.get("SSH_AUTH_SOCK"):
        f.append(
            Finding(
                "WARN",
                "SSH_AUTH_SOCK is set in this session: don't `ssh -A` into this machine; an agent"
                " here could use your forwarded keys",
            )
        )
    rr = _rrsync()
    f.append(
        Finding(
            "note",
            f"rrsync: {rr}"
            if rr
            else "rrsync not found (the push variant of the bitstream key needs it here, §27.8.4)",
        )
    )
    import switchboard

    pkg = os.path.dirname(os.path.realpath(switchboard.__file__))
    mine = [p for p in (os.path.realpath(sys.executable), pkg) if _writable(p)]
    if mine:
        f.append(
            Finding(
                "note",
                "this user can write the satellite's install (" + ", ".join(mine) + "): any process"
                " of this user could change what the forced command runs at the next link start"
                " (§27.12); a root-owned install closes that",
            )
        )
    st = status()
    if st is not None and st.get("role") == "satellite":
        f.append(
            Finding(
                "ok",
                f"the satellite runs (pid {st.get('pid')}), link {st.get('link')},"
                f" harden {st.get('harden')}, stdio {st.get('stdio', '?')}",
            )
        )
    else:
        f.append(
            Finding(
                "note",
                "no satellite runs now: the desktop dials this machine (`switchboard remote status` there)",
            )
        )
    if probe_desktop is not None:
        f.append(probe(probe_desktop, environ.get("SSH_AUTH_SOCK") or None))
    return f


def probe(dest: str, agent_sock: str | None = None) -> Finding:
    """``--probe-desktop``: can this machine open a shell on the desktop with this user's own
    ssh setup, this session's agent included? Success is the finding (``ssh -o
    BatchMode=yes -o ConnectTimeout=5 <dest> true``); a probe that can't run is a WARN
    (unknown), never the end of the doctor."""
    user, host = split_dest(dest)
    target = f"{user}@{host}" if user else host
    try:
        r = _run(
            [SSH_BIN, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", target, "true"],
            timeout=PROBE_TIMEOUT_S,
            agent_sock=agent_sock,
        )
    except PairingError as e:
        return Finding(
            "WARN",
            f"the probe of {target} could not run ({e}): check by hand that `ssh {target} true` fails here",
        )
    if r.returncode == 0:
        return Finding(
            "WARN",
            f"this machine can open a shell on {target} (ssh … true succeeded): an agent here"
            " could act as you on the desktop; remove that key from the desktop",
        )
    how = (
        "this user's keys and this session's agent"
        if agent_sock
        else "this user's keys (no agent in this session)"
    )
    return Finding(
        "ok", f"ssh {target} true failed here (exit {r.returncode}): no shell on the desktop from {how}"
    )


def print_findings(findings: list[Finding], out: TextIO, paint: Any = None) -> int:
    """``remote doctor``'s findings, one per line; with a ``colors.Paint`` that is on,
    the level word is coloured (ok green, WARN yellow, FAIL red, a note dim)."""
    from switchboard.colors import PLAIN

    p = paint or PLAIN
    for x in findings:
        style = {"OK": p.ok, "WARN": p.warn, "FAIL": p.bad}.get(x.level.upper(), p.dim)
        print(f"  {style(x.level)}{' ' * max(0, 4 - len(x.level))}  {x.text}", file=out)
    bad = [x for x in findings if x.level in ("WARN", "FAIL")]
    fails = sum(1 for x in bad if x.level == "FAIL")
    summary = f"{len(bad) - fails} warning(s), {fails} failure(s)"
    print(p.ok("clean") if not bad else p.bad(summary) if fails else p.warn(summary), file=out)
    return 0 if not bad else 1
