"""``remotes.toml`` (the desktop) and ``satellite.toml`` (a Pi home), DESIGN.md §27.3, §27.8.

``remotes.toml``, one table per remote, every key checked (unknown keys refused)::

    [remote.fpga-pi]
    host = "fpga-pi.local"          # a DNS name or an IP literal
    user = "alice"
    port = 22
    rooms = ["#fpga"]               # required: the only rooms this host's members may join
    harnesses = ["claude", "codex", "cursor", "devin"]
    max_members = 8                 # 1..32
    end_after_s = 900               # members of a host unreachable this long are ended

A test-mode broker also accepts ``transport = "exec"`` with ``home = "<satellite home>"``:
the broker then runs the satellite itself on this machine instead of over ssh (§27.4.1).
The ``test`` harness is never listed: it is allowed exactly when both ends run in test mode.

The owner's consent is for one exact config: ``config_hash`` covers every field, the
link key's fingerprint and the pinned host-key line (§27.5.8).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import os
import re
import stat
import time
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from switchboard.models import InvalidName, normalize_room, valid_host
from switchboard.paths import Paths

REMOTE_HARNESSES = ("claude", "codex", "cursor", "devin")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
MAX_MEMBERS_CAP = 32
END_AFTER_MAX_S = 30 * 24 * 3600
TRANSPORTS = ("ssh", "exec")
_KEYS = frozenset({"host", "user", "port", "rooms", "harnesses", "max_members", "end_after_s", "transport", "home"})
_SAT_KEYS = frozenset({"name", "desktop", "key_fingerprint", "accepted_at"})
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class RemoteConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RemoteEntry:
    name: str
    host: str
    user: str
    port: int = 22
    rooms: tuple[str, ...] = ()
    harnesses: tuple[str, ...] = REMOTE_HARNESSES
    max_members: int = 8
    end_after_s: int = 900
    transport: str = "ssh"
    home: str = ""  # the satellite's home, exec transport only

    def canonical(self) -> dict[str, Any]:
        d = asdict(self)
        d["rooms"] = list(self.rooms)
        d["harnesses"] = list(self.harnesses)
        return d


def remotes_path(paths: Paths) -> Path:
    return paths.home / "remotes.toml"


def remote_dir(paths: Paths, name: str) -> Path:
    return paths.home / "remotes" / name


SCOPE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,14}$")  # an interface name (IFNAMSIZ) or index


def _is_host(h: str) -> bool:
    """A DNS name, or an IP literal; an IPv6 zone (``fe80::1%eth0``) only if it looks like
    an interface name, since the value becomes ssh's destination argument (M8e)."""
    if h.startswith("-"):
        return False
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return bool(HOSTNAME_RE.fullmatch(h)) and ".." not in h and not h.endswith("-")
    scope = getattr(ip, "scope_id", None)
    return scope is None or bool(SCOPE_ID_RE.fullmatch(scope))


def _int(v: Any, where: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise RemoteConfigError(f"{where} must be an integer in {lo}..{hi}")
    return v


def parse_entry(name: str, data: Any, *, test_mode: bool) -> RemoteEntry:
    where = f"[remote.{name}]"
    if not valid_host(name):
        raise RemoteConfigError(f"{where}: remote names look like fpga-pi (a-z first, then a-z, 0-9, '-'; at most 24)")
    if not isinstance(data, dict):
        raise RemoteConfigError(f"{where} must be a table")
    unknown = sorted(set(data) - _KEYS)
    if unknown:
        raise RemoteConfigError(f"unknown key(s) in {where}: {', '.join(unknown)}")
    transport = data.get("transport", "ssh")
    if transport not in TRANSPORTS:
        raise RemoteConfigError(f"{where}.transport must be \"ssh\"")
    if transport == "exec" and not test_mode:
        raise RemoteConfigError(f'{where}.transport = "exec" needs a test-mode broker')
    home = data.get("home", "")
    if transport == "exec":
        if not isinstance(home, str) or not os.path.isabs(home):
            raise RemoteConfigError(f"{where}.home (the satellite's home) must be an absolute path")
    elif "home" in data:
        raise RemoteConfigError(f'{where}.home is only for transport = "exec"')
    host = data.get("host", "")
    user = data.get("user", "")
    if transport == "ssh" or host:
        if not isinstance(host, str) or not _is_host(host):
            raise RemoteConfigError(f"{where}.host must be a host name or an IP address")
    if transport == "ssh" or user:
        if not isinstance(user, str) or not USER_RE.fullmatch(user):
            raise RemoteConfigError(f"{where}.user must be a login name (a-z, 0-9, '_', '-')")
    port = _int(data.get("port", 22), f"{where}.port", 1, 65535)
    raw_rooms = data.get("rooms")
    if not isinstance(raw_rooms, list) or not raw_rooms:
        raise RemoteConfigError(f"{where}.rooms is required: the rooms this host's members may join, e.g. [\"#fpga\"]")
    rooms: list[str] = []
    for r in raw_rooms:
        try:
            n = normalize_room(r)
        except InvalidName as e:
            raise RemoteConfigError(f"{where}.rooms: {e}") from None
        if n not in rooms:
            rooms.append(n)
    raw_h = data.get("harnesses", list(REMOTE_HARNESSES))
    if not isinstance(raw_h, list) or not all(isinstance(h, str) for h in raw_h):
        raise RemoteConfigError(f"{where}.harnesses must be a list of harness names")
    bad = sorted(set(raw_h) - set(REMOTE_HARNESSES))
    if bad:
        raise RemoteConfigError(f"{where}.harnesses: unknown harness(es) {', '.join(bad)}"
                                f" (allowed: {', '.join(REMOTE_HARNESSES)})")
    harnesses = tuple(h for h in REMOTE_HARNESSES if h in raw_h)
    return RemoteEntry(
        name=name, host=host, user=user, port=port, rooms=tuple(rooms), harnesses=harnesses,
        max_members=_int(data.get("max_members", 8), f"{where}.max_members", 1, MAX_MEMBERS_CAP),
        end_after_s=_int(data.get("end_after_s", 900), f"{where}.end_after_s", 1, END_AFTER_MAX_S),
        transport=transport, home=home if transport == "exec" else "",
    )


def parse_remotes(text: str, *, test_mode: bool) -> dict[str, RemoteEntry]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise RemoteConfigError(f"remotes.toml: {e}") from None
    unknown = sorted(set(data) - {"remote"})
    if unknown:
        raise RemoteConfigError(f"remotes.toml: unknown top-level key(s): {', '.join(unknown)}")
    tables = data.get("remote", {})
    if not isinstance(tables, dict):
        raise RemoteConfigError("remotes.toml: [remote.<name>] tables expected")
    return {name: parse_entry(name, t, test_mode=test_mode) for name, t in tables.items()}


def load_remotes(paths: Paths, *, test_mode: bool) -> dict[str, RemoteEntry]:
    """``<home>/remotes.toml``, or {} if there is none. The file must be yours and
    writable by nobody else (``remote add`` writes it 0600)."""
    path = remotes_path(paths)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise RemoteConfigError(f"remotes.toml: {e.strerror or e}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RemoteConfigError("remotes.toml is not a regular file")
        if st.st_uid != os.getuid() or st.st_mode & 0o022:
            raise RemoteConfigError("remotes.toml must be yours and writable only by you (chmod 600 remotes.toml)")
        with os.fdopen(fd, "rb") as f:
            fd = -1
            raw = f.read(1 << 20)
    finally:
        if fd >= 0:
            os.close(fd)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise RemoteConfigError(f"remotes.toml: {e}") from None
    return parse_remotes(text, test_mode=test_mode)


def key_fingerprint(pub_line: str) -> str:
    """``SHA256:<base64>`` of an OpenSSH public key line's key blob, or '' if it isn't one."""
    parts = pub_line.split()
    if len(parts) < 2:
        return ""
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        return ""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def link_material(paths: Paths, name: str) -> tuple[str, str]:
    """The link key's fingerprint and the pinned host keys of remote ``name``
    (``remotes/<name>/id_ed25519.pub``, and every non-blank line of ``known_hosts``:
    ssh trusts each of them, so an added line is a new config); '' for what isn't
    there (an exec-transport remote has neither)."""
    d = remote_dir(paths, name)
    try:
        fp = key_fingerprint((d / "id_ed25519.pub").read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        fp = ""
    try:
        lines = [ln.strip() for ln in (d / "known_hosts").read_text(encoding="utf-8").splitlines()]
        pin = "\n".join(ln for ln in lines if ln)
    except (OSError, UnicodeDecodeError):
        pin = ""
    return fp, pin


SSH_BIN = "/usr/bin/ssh"  # the only client a link dials through (§27.4.1)
SSH_KEYGEN_BIN = "/usr/bin/ssh-keygen"


def host_key_alias(name: str) -> str:
    """The name the link pins the remote's host key under (``HostKeyAlias``), so a new
    DHCP address doesn't break the pin (§27.4.1)."""
    return f"switchboard-{name}"


def link_key_path(paths: Paths, name: str) -> Path:
    return remote_dir(paths, name) / "id_ed25519"


def pin_path(paths: Paths, name: str) -> Path:
    return remote_dir(paths, name) / "known_hosts"


def system_bin_problem(path: str) -> str | None:
    """Why ``path`` may not be run as the system's OpenSSH (``/usr/bin/ssh``,
    ``ssh-keygen``), or None: it must be a regular executable file owned by root and
    writable by nobody else, as is every directory above it. A binary a user could
    replace would carry the link key to whatever it is."""
    p = Path(path)
    if not p.is_absolute():
        return f"{path} is not an absolute path"
    try:
        st = os.stat(p)
    except OSError as e:
        return f"{path}: {e.strerror or e}"
    if not stat.S_ISREG(st.st_mode) or not st.st_mode & 0o111:
        return f"{path} is not an executable file"
    for q, s in [(p, st)] + [(d, None) for d in p.resolve().parents]:
        try:
            s = s or os.stat(q)
        except OSError as e:
            return f"{q}: {e.strerror or e}"
        if s.st_uid != 0:
            return f"{q} is not owned by root"
        if s.st_mode & 0o022:
            return f"{q} is writable by others than root"
    return None


# what ssh would read as more than a path in ``-i`` and ``-o UserKnownHostsFile=``: a
# blank splits the value (several files), '#' starts a comment, '%' and '${' expand,
# a quote or backslash is parsed, '~' expands
_SSH_PATH_UNSAFE = re.compile(r"[\s\"'\\#%$~\x00-\x1f\x7f]")
PIN_KEY_TYPES = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521))$")  # plain host keys
PIN_MAX_BYTES = 16 * 1024


def home_path_problem(paths: Paths) -> str | None:
    """Why this home's path can't go into ssh's argv as one path, or None."""
    if _SSH_PATH_UNSAFE.search(str(paths.home)):
        return ("the switchboard home's path has a blank, a quote, a backslash or one of # % $ ~, which ssh"
                " would read as more than one path: use a home without them (--home or SWITCHBOARD_HOME)")
    return None


def _key_blob_type(b64: str) -> str:
    """The key type named inside an OpenSSH public key blob, or ''."""
    try:
        blob = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return ""
    if len(blob) < 4:
        return ""
    n = int.from_bytes(blob[:4], "big")
    try:
        return blob[4:4 + n].decode("ascii") if 0 < n <= 64 and len(blob) >= 4 + n else ""
    except UnicodeDecodeError:
        return ""


def _private_dir_problem(p: Path, rel: str) -> str | None:
    try:
        st = os.lstat(p)
    except OSError:
        return f"{rel}/ is missing"
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        return f"{rel}/ must be a private directory of yours (0700)"
    return None


def pin_problem(paths: Paths, name: str) -> str | None:
    """Why ``remotes/<name>/known_hosts`` is not exactly one pinned host key, or None: a
    regular file of yours (never a link) that nobody else can write, holding one line
    ``switchboard-<name> <type> <key>`` and nothing else. ssh trusts every line of it, so
    a second key, a wildcard or an ``@cert-authority`` line would widen the pin."""
    rel = f"remotes/{name}/known_hosts"
    try:
        fd = os.open(pin_path(paths, name), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return f"{rel} is missing or not a regular file (the pin `switchboard remote add` makes)"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
            return f"{rel} must be a regular file of yours that nobody else can write (0600)"
        raw = os.read(fd, PIN_MAX_BYTES + 1)
    finally:
        os.close(fd)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    alias = host_key_alias(name)
    if len(raw) > PIN_MAX_BYTES or len(lines) != 1:
        return (f"{rel} must hold exactly one line, the host key pinned under {alias}"
                f" ({len(lines)} found): anything more would be trusted too")
    words = lines[0].split()
    if (len(words) != 3 or words[0] != alias or not PIN_KEY_TYPES.fullmatch(words[1])
            or _key_blob_type(words[2]) != words[1]):
        return f"{rel} has no pinned host key for {alias} (one line: {alias} <type> <key>)"
    return None


def ssh_files_problem(paths: Paths, name: str) -> str | None:
    """Why remote ``name``'s link files can't be used, or None: the home's path fits in
    ssh's argv; ``remotes/`` and ``remotes/<name>/`` are private directories of yours;
    the link key ``remotes/<name>/id_ed25519`` is a regular file of yours that nobody
    else can read (ssh refuses it otherwise); ``known_hosts`` holds exactly the one
    pinned host key (``pin_problem``). Messages name files relative to the home (a
    blocked notice reaches the remote's rooms)."""
    why = home_path_problem(paths)
    if why:
        return why
    why = (_private_dir_problem(paths.home / "remotes", "remotes")
           or _private_dir_problem(remote_dir(paths, name), f"remotes/{name}"))
    if why:
        return why
    rel = f"remotes/{name}/id_ed25519"
    try:
        st = os.lstat(link_key_path(paths, name))
    except OSError:
        return f"{rel} is missing (the link key `switchboard remote add` makes)"
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        return f"{rel} must be a private file of yours (0600)"
    return pin_problem(paths, name)


def config_hash(entry: RemoteEntry, key_fp: str, pinned_line: str) -> str:
    """sha256 over the canonical entry, the link key's fingerprint and the pinned
    host-key line: any edit to any of them needs a new enable (§27.5.8)."""
    blob = json.dumps({"entry": entry.canonical(), "key": key_fp, "pin": pinned_line},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def entry_hash(paths: Paths, entry: RemoteEntry) -> str:
    return config_hash(entry, *link_material(paths, entry.name))


# ------------------------------------------------------------- satellite.toml
@dataclass(frozen=True)
class SatelliteConf:
    """A satellite home's ``satellite.toml`` (written by ``switchboard remote accept``)."""

    name: str
    desktop: str = ""  # a label for the desktop, for messages on the Pi
    key_fingerprint: str = ""
    accepted_at: float | None = None


def read_satellite_conf(paths: Paths) -> SatelliteConf:
    """Raises FileNotFoundError when this is not a satellite home, RemoteConfigError when
    the file is not valid."""
    raw = paths.satellite_conf.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise RemoteConfigError(f"satellite.toml: {e}") from None
    unknown = sorted(set(data) - _SAT_KEYS)
    if unknown:
        raise RemoteConfigError(f"satellite.toml: unknown key(s): {', '.join(unknown)}")
    name = data.get("name")
    if not isinstance(name, str) or not valid_host(name):
        raise RemoteConfigError("satellite.toml: name must be a remote name such as fpga-pi")
    desktop = data.get("desktop", "")
    if not isinstance(desktop, str) or (desktop and not LABEL_RE.fullmatch(desktop)):
        raise RemoteConfigError("satellite.toml: desktop must be a short label")
    fp = data.get("key_fingerprint", "")
    if not isinstance(fp, str) or len(fp) > 100:
        raise RemoteConfigError("satellite.toml: key_fingerprint must be a string")
    at = data.get("accepted_at")
    if at is not None and (isinstance(at, bool) or not isinstance(at, (int, float))):
        raise RemoteConfigError("satellite.toml: accepted_at must be a number")
    return SatelliteConf(name=name, desktop=desktop, key_fingerprint=fp,
                         accepted_at=float(at) if at is not None else None)


def _toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=True)  # a TOML basic string for these plain values


def write_satellite_conf(paths: Paths, name: str, desktop: str = "", key_fp: str = "",
                         accepted_at: float | None = None) -> Path:
    """Write ``satellite.toml`` atomically, 0600."""
    if not valid_host(name):
        raise RemoteConfigError(f"not a remote name: {name!r}")
    if desktop and not LABEL_RE.fullmatch(desktop):
        raise RemoteConfigError(f"not a desktop label: {desktop!r}")
    at = time.time() if accepted_at is None else accepted_at
    text = (f"name = {_toml_str(name)}\n"
            f"desktop = {_toml_str(desktop)}\n"
            f"key_fingerprint = {_toml_str(key_fp)}\n"
            f"accepted_at = {at!r}\n")
    target = paths.satellite_conf
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, target)
    return target


def is_satellite_home(paths: Paths) -> bool:
    try:
        return paths.satellite_conf.exists()
    except OSError:
        return False


def satellite_desktop(paths: Paths) -> str:
    """The desktop's label from ``satellite.toml`` ('the desktop' when unknown)."""
    try:
        return read_satellite_conf(paths).desktop or "the desktop"
    except (OSError, RemoteConfigError):
        return "the desktop"
