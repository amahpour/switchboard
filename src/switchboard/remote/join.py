"""``switchboard remote join <url> <code>`` and the rest of a machine's side of a broker it dials
(DESIGN.md §31.7): pairing, leaving, and what ``status`` and ``doctor`` say there.

``join``:
1. **The home** must be one this machine can give to a dialer: never a home that runs a broker
   (it has a database), so this machine's agents don't quietly move off a local broker; not an
   ssh satellite home; and not one whose dialer runs. ``--home`` names another one.
2. **A key**: Ed25519, ``<home>/link/id_ed25519`` (0600), made here and never sent anywhere; its
   fingerprint is printed, to compare with the one the web UI shows before approving.
3. **The pairing**: ``POST <url>/link/pair`` with the code, the public key and this machine's
   facts (host name, OS, arch, switchboard version, the harnesses on PATH), over TLS that
   trusts the operating system's certificate store. The answer pins the broker's key.
4. **satellite.toml** (``transport = "wss"``, the broker's URL and key), then the dialer starts,
   unless ``--no-start``. The commands that point this machine's agents at the home are printed.

A code that was already used is refused loudly: someone else paired with it, and the owner must
not approve that pending machine.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import shutil
import socket
import ssl
import sys
import textwrap
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, TextIO

from switchboard import __version__
from switchboard.broker.auth import WebOrigin
from switchboard.models import valid_host
from switchboard.paths import Paths
from switchboard.remote import linkkey
from switchboard.remote.config import (
    LABEL_RE,
    RemoteConfigError,
    read_satellite_conf,
    remotes_path,
    write_satellite_conf,
)

# each harness, and the commands on PATH that mean it is installed
HARNESS_BINS = {
    "claude": ("claude",),
    "codex": ("codex",),
    "cursor": ("cursor-agent", "agent"),
    "devin": ("devin",),
}
POST_TIMEOUT_S = 20.0
USED_BANNER = (
    "This code was already used by another machine. Don't approve the pending machine:"
    " remove it in the web UI and make a new code."
)


class JoinError(Exception):
    """A refusal, with the message to print."""


def found_harnesses() -> list[str]:
    return [h for h, bins in HARNESS_BINS.items() if any(shutil.which(b) for b in bins)]


def machine_facts() -> dict[str, Any]:
    """What this machine says about itself: shown to the owner as the machine's own claims."""
    return {
        "hostname": socket.gethostname()[:64],
        "os": f"{platform.system()} {platform.release()}"[:80],
        "arch": platform.machine()[:32],
        "version": __version__,
        "harnesses": found_harnesses(),
    }


def tls_context() -> ssl.SSLContext:
    import truststore

    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def post_pair(
    origin: str, body: dict[str, Any], timeout: float = POST_TIMEOUT_S
) -> tuple[int, dict[str, Any]]:
    """``POST <origin>/link/pair``; (status, JSON answer). No cookie, no Origin: this isn't a browser."""
    o = WebOrigin.parse(origin)
    req = urllib.request.Request(
        o.origin + linkkey.PAIR_PATH,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": f"switchboard/{__version__}"},
    )
    handlers: list[Any] = [urllib.request.HTTPSHandler(context=tls_context())] if o.scheme == "https" else []
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, _json(r.read(64 * 1024))
    except urllib.error.HTTPError as e:
        with contextlib.suppress(Exception):
            return e.code, _json(e.read(64 * 1024))
        return e.code, {}
    except (urllib.error.URLError, OSError, ssl.SSLError) as e:
        reason = getattr(e, "reason", None) or e
        raise JoinError(f"can't reach {o.origin}: {reason}") from None


def _json(raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def home_problem(paths: Paths) -> str | None:
    """Why ``remote join`` can't use this home, or None."""
    if paths.db.exists() or remotes_path(paths).exists():
        return (
            f"{paths.home} runs a switchboard broker (it has its database): a machine that dials a broker"
            " needs a home of its own, so this machine's agents don't move off your local broker without"
            " you knowing. Run this again with --home, for example --home ~/.switchboard-link"
        )
    try:
        conf = read_satellite_conf(paths)
    except FileNotFoundError:
        conf = None
    except (OSError, RemoteConfigError) as e:
        return f"satellite.toml: {e}"
    if conf is not None and not conf.dials:
        return (
            f"{paths.home} is dialed over ssh by a desktop ({conf.desktop or '?'}): use another --home, or"
            f" `switchboard remote remove {conf.name}` here first"
        )
    from switchboard.remote.dialer import running_pid

    pid = running_pid(paths)
    if pid is not None:
        return f"this home's dialer runs (pid {pid}): stop it first (switchboard stop), or use another --home"
    from switchboard.mcp.client import ping

    if ping(paths.sock, timeout=1.0) is not None:
        return "a switchboard process answers on this home's socket: stop it first, or use another --home"
    return None


def _label(host: str) -> str:
    s = "".join(c if c.isalnum() or c in "._-" else "-" for c in host)[:64].strip("-._") or "broker"
    return s if LABEL_RE.fullmatch(s) else "broker"


def install_lines(paths: Paths, home_given: bool) -> list[str]:
    flag = f" --home {paths.home}" if home_given else ""
    hs = found_harnesses() or ["claude"]
    return [f"  switchboard install {h}{flag}" for h in hs]


def join(
    paths: Paths,
    url: str,
    code: str,
    *,
    home_given: bool,
    test_mode: bool = False,
    start: bool = True,
    out: TextIO | None = None,
    post: Callable[..., tuple[int, dict[str, Any]]] = post_pair,
    start_dialer: Callable[..., int] | None = None,
) -> int:
    out = out or sys.stdout
    try:
        origin = WebOrigin.parse(url)
    except ValueError as e:
        raise JoinError(f"{url!r}: {e}") from None
    if linkkey.normalize_code(code) is None:
        raise JoinError("a pairing code looks like 7KQ4-M2XD-9HVA: copy it from the web UI (Add a machine)")
    why = home_problem(paths)
    if why:
        raise JoinError(why)
    os.umask(0o077)
    paths.ensure()
    key_path = paths.home / "link" / linkkey.MACHINE_KEY
    key = linkkey.make_key(key_path)
    pub = linkkey.pub_raw(key)
    fp = linkkey.fingerprint(pub)
    status, data = post(origin.origin, {"code": code, "key": linkkey.b64u(pub), "facts": machine_facts()})
    if status == 409 and data.get("error") == "used":
        with contextlib.suppress(OSError):
            key_path.unlink()
        bar = "!" * 72
        print(f"{bar}\n{textwrap.fill(USED_BANNER, 72)}\n{bar}", file=out)
        return 1
    if status != 200:
        with contextlib.suppress(OSError):
            key_path.unlink()
        raise JoinError(
            str(
                data.get("message")
                or f"{origin.origin} answered {status}: is it a switchboard broker"
                " with passkeys (a hosted one)?"
            )
        )
    name, bkey, got_fp = data.get("name"), data.get("broker_key"), data.get("fingerprint")
    try:
        bkey_raw = linkkey.unb64u(bkey, 32)
    except ValueError:
        raise JoinError("the broker's answer holds no key") from None
    if not isinstance(name, str) or not valid_host(name):
        raise JoinError("the broker's answer holds no machine name")
    if got_fp != fp:
        raise JoinError(
            "the broker received another key than this machine's: something between this machine and the"
            " broker changed it. Don't approve anything: remove the pending machine in the web UI"
        )
    write_satellite_conf(
        paths,
        name,
        desktop=_label(origin.hostname),
        key_fp=fp,
        broker_url=origin.origin,
        broker_key=linkkey.b64u(bkey_raw),
    )
    print(f"Paired as {name} with {origin.origin}.", file=out)
    print(f"The broker's key, pinned here: {linkkey.fingerprint(bkey_raw)}", file=out)
    print(f"\nThis machine's key: {fp}", file=out)
    print("Check the web UI shows the same before you approve it.", file=out)
    print("\nTo point this machine's agents at it, run once:", file=out)
    for line in install_lines(paths, home_given):
        print(line, file=out)
    home_flag = f" --home {paths.home}" if home_given else ""
    if not start:
        print(f"\nThen start its dialer: switchboard start{home_flag}", file=out)
        return 0
    print("", file=out)
    if start_dialer is None:
        from switchboard.broker.daemon import start_dialer as start_dialer_

        start_dialer = start_dialer_
    return start_dialer(paths, test_mode=test_mode, out=out)


# ------------------------------------------------------------------ leave
def leave(
    paths: Paths, name: str, *, yes: bool = False, stdin: TextIO | None = None, out: TextIO | None = None
) -> int:
    """``switchboard remote remove <name>`` on a home that dials its broker: stop the dialer, and
    forget the pairing (``satellite.toml`` and the machine key)."""
    from switchboard.install.common import confirm
    from switchboard.remote.dialer import pid_path, state_path

    out = out or sys.stdout
    try:
        conf = read_satellite_conf(paths)
    except (OSError, RemoteConfigError) as e:
        raise JoinError(f"satellite.toml: {e}") from None
    if conf.name != name:
        raise JoinError(f"this home is {conf.name}'s, not {name}'s")
    key_path = paths.home / "link" / linkkey.MACHINE_KEY
    print(f"switchboard remote remove {name}:", file=out)
    print(f"  stops its dialer, then deletes {paths.satellite_conf} and {key_path}", file=out)
    if not confirm(yes, stdin, out):
        print("not applied", file=out)
        return 1
    from switchboard.broker.daemon import stop_dialer

    stop_dialer(paths, out=out)
    for p in (paths.satellite_conf, key_path, state_path(paths), pid_path(paths)):
        with contextlib.suppress(FileNotFoundError):
            p.unlink()
    print(
        f"removed {name} here. Remove it in the web UI too ({conf.broker_url}), if it's still listed.",
        file=out,
    )
    return 0


def status_lines(paths: Paths) -> list[str]:
    """What ``switchboard status`` says on a home that dials its broker."""
    from switchboard.remote.dialer import read_state, running_pid

    conf = read_satellite_conf(paths)
    st = read_state(paths) or {}
    pid = running_pid(paths)
    state = st.get("state") if pid is not None or st.get("state") == "stopped" else "not running"
    reason = st.get("reason")
    lines = [
        f"switchboard dialer for {conf.name}: {state}{f' ({reason})' if reason else ''}"
        + (f", pid {pid}" if pid else "")
    ]
    lines.append(f"  broker  {conf.broker_url}")
    lines.append(f"  key     {conf.key_fingerprint}")
    if st.get("message"):
        lines += textwrap.wrap(
            str(st["message"]), 96, initial_indent="  why     ", subsequent_indent=" " * 10
        )
    if pid is None and state != "stopped":
        where = "" if paths.home == Paths.from_home(None).home else f" --home {paths.home}"
        lines.append(f"  start it with `switchboard start{where}`")
    return lines
