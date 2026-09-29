"""switchboard start / stop / status and the foreground broker (DESIGN.md §2)."""

from __future__ import annotations

import asyncio
import fcntl
import ipaddress
import logging
import logging.handlers
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from switchboard import db
from switchboard.broker.auth import WebOrigin
from switchboard.config import Config
from switchboard.mcp.client import BrokerDown, RpcError, call_sync, ping
from switchboard.paths import Paths, test_mode_refusal

log = logging.getLogger("switchboard.daemon")

START_WAIT_S = 8.0
STOP_WAIT_S = 10.0
# Env vars a daemonized broker must not inherit from the terminal that started it.
_DROP_ENV_PREFIXES = ("CLAUDE_CODE_MESSAGING_", "CLAUDE_CODE_SESSION_ID")
_DROP_ENV = frozenset({"CLAUDECODE"})
# the default listen address (guardrail 9): the only one that needs no --public-url
LOOPBACK = "127.0.0.1"


class DaemonError(Exception):
    pass


# ------------------------------------------------------------------ helpers
def setup_logging(paths: Paths, level: int = logging.INFO, *, stdout: bool = False) -> None:
    """Rotating 5 MB x 3 log at logs/broker.log (0600). Ids only, never message text.

    ``stdout`` (``start --foreground --log-stdout``, the container image) writes the same
    records to stdout instead, for the platform's log collector (docs/DEPLOY.md)."""
    os.umask(0o077)
    handler: logging.Handler
    if stdout:
        handler = logging.StreamHandler(sys.stdout)
    else:
        handler = logging.handlers.RotatingFileHandler(
            paths.log, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger("switchboard").setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastmcp", "mcp"):
        logging.getLogger(name).setLevel(logging.WARNING)


def check_test_mode(paths: Paths, home_given: bool) -> str | None:
    """Why --test-mode is refused here, or None if it's allowed (§2 test mode)."""
    return test_mode_refusal(paths, home_given)


def read_pidfile(paths: Paths) -> tuple[int, float] | None:
    try:
        pid_s, start_s = paths.pidfile.read_text().split()
        return int(pid_s), float(start_s)
    except (OSError, ValueError):
        return None


def _write_pidfile(paths: Paths) -> None:
    from switchboard.broker import proc

    me = proc.info(os.getpid())
    start = me.start if me else time.time()
    tmp = paths.pidfile.with_name(paths.pidfile.name + ".tmp")
    tmp.write_text(f"{os.getpid()} {start!r}\n")
    os.replace(tmp, paths.pidfile)


def _broker_env() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if k not in _DROP_ENV and not k.startswith(_DROP_ENV_PREFIXES)
    }


# --------------------------------------------------------------- foreground
def web_settings(listen: str, public_url: str) -> WebOrigin | None:
    """Check ``--listen`` and ``--public-url`` (DESIGN.md §30): the parsed public origin, or None
    for the default (``switchboard.localhost`` on the bound port). A ValueError says why not.

    Loopback stays the default (guardrail 9). Any other listen address needs a public URL, so the
    Host, Origin, CSP and cookie rules always name the one address browsers use, and the broker
    is meant to sit behind a proxy that terminates TLS (a platform's router, Caddy, an ingress).
    127.0.0.1 is the only address without one: ``switchboard.localhost`` resolves there and
    nowhere else.
    """
    try:
        ipaddress.IPv4Address(listen)
    except ValueError:
        raise ValueError(f"--listen must be an IPv4 address, such as 127.0.0.1 or 0.0.0.0 (not {listen!r})") from None
    try:
        origin = WebOrigin.parse(public_url) if public_url else None
    except ValueError as e:
        raise ValueError(f"--public-url {public_url!r}: {e}") from None
    if listen != LOOPBACK and origin is None:
        raise ValueError(f"listening on {listen} needs --public-url: the https:// address browsers use, "
                         "served by a proxy that terminates TLS")
    return origin


def tcp_listener(host: str, port: int) -> socket.socket:
    """The broker's TCP socket on ``host`` (``--listen``; see ``loopback_listener``)."""
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    try:
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp.bind((host, port))
    except OSError:
        tcp.close()
        raise
    return tcp


def loopback_listener(port: int) -> socket.socket:
    """The broker's TCP socket, bound to 127.0.0.1 only (the default).

    ``proto=IPPROTO_TCP`` matters on Linux: asyncio sets TCP_NODELAY only on
    sockets whose ``proto`` is IPPROTO_TCP, which a Linux accepted socket takes
    from the listener. With a plain ``socket(AF_INET, SOCK_STREAM)`` (proto 0)
    Nagle held every second small write (a response's body after its headers,
    back-to-back WebSocket frames) until the client's delayed ACK: about 40 ms
    per response on Linux. (macOS reports an accepted socket's proto as 0 either
    way, so nothing changes there; no delay shows on macOS.)
    """
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    try:
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp.bind((LOOPBACK, port))
    except OSError:
        tcp.close()
        raise
    return tcp


def run_foreground(
    paths: Paths,
    cfg: Config,
    *,
    port: int | None = None,
    listen: str | None = None,
    public_url: str | None = None,
    log_stdout: bool = False,
    test_mode: bool = False,
    test_trust_uds: bool = False,
    announce: bool = True,
) -> int:
    import uvicorn

    from switchboard.broker.app import create_app, default_peer_policy

    listen = cfg.listen if listen is None else listen
    public_url = cfg.public_url if public_url is None else public_url
    try:
        public = web_settings(listen, public_url)
    except ValueError as e:
        print(f"switchboard: {e}", file=sys.stderr)
        return 1
    os.umask(0o077)
    paths.ensure()
    lock_fd = os.open(paths.lockfile, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        print("switchboard: another broker already runs from this home", file=sys.stderr)
        return 1
    try:
        setup_logging(paths, stdout=log_stdout)
        # Create the database, or migrate a version-1 one (after a verified backup,
        # DESIGN.md §27.6), here: under the single-instance lock and before anything
        # listens, with a refusal on stderr (broker.out, which `switchboard start`
        # shows) and not only in broker.log. The lifespan then finds it current.
        try:
            db.open_db(paths.db).close()
        except db.SchemaError as e:
            log.error("database refused: %s", e)
            print(f"switchboard: {e}", file=sys.stderr)
            return 1
        want = cfg.port if port is None else port
        try:
            tcp = loopback_listener(want) if listen == LOOPBACK else tcp_listener(listen, want)
        except OSError as e:
            print(f"switchboard: can't listen on {listen}:{want}: {e.strerror}", file=sys.stderr)
            return 1
        actual = tcp.getsockname()[1]
        origin = public or WebOrigin.local(actual)
        _write_pidfile(paths)
        app = create_app(paths, cfg, default_peer_policy(cfg, test_trust_uds), test_mode, port=actual,
                         web_origin=origin)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                access_log=False,
                log_level="warning",
                log_config=None,
                lifespan="on",
                ws_ping_interval=20.0,
                ws_ping_timeout=20.0,
                server_header=False,
                date_header=False,
                proxy_headers=False,
            )
        )
        app.state.broker.shutdown_cb = lambda: setattr(server, "should_exit", True)
        if announce:
            print(
                f"switchboard broker pid {os.getpid()} on {origin.origin}/"
                + (f" (listening on {listen}:{actual})" if listen != LOOPBACK else "")
                + (" (TEST MODE)" if test_mode else ""),
                flush=True,
            )
        # SIGTERM (`docker stop`, a platform's redeploy) stops the broker as `switchboard stop`
        # does. uvicorn catches it, shuts down gracefully, then raises it again for the handler
        # it replaced; with this no-op there, the cleanup below runs and the exit status is 0.
        previous = signal.signal(signal.SIGTERM, lambda signum, frame: None)
        try:
            asyncio.run(server.serve(sockets=[tcp]))
        finally:
            signal.signal(signal.SIGTERM, previous)
            tcp.close()
        return 0 if server.started else 1
    finally:
        pf = read_pidfile(paths)
        if pf and pf[0] == os.getpid():
            try:
                paths.pidfile.unlink()
            except FileNotFoundError:
                pass
        os.close(lock_fd)


# --------------------------------------------------------------------- start
def start(
    paths: Paths,
    *,
    port: int | None = None,
    listen: str | None = None,
    public_url: str | None = None,
    test_mode: bool = False,
    test_trust_uds: bool = False,
    out: Any = None,
) -> int:
    out = out or sys.stdout
    alive = ping(paths.sock)
    if alive:
        url = alive.get("url") or f"http://switchboard.localhost:{alive.get('port')}/"
        print(
            f"switchboard is already running (pid {alive.get('pid')}) at {url}\n"
            "Run `switchboard login` for a new sign-in link.",
            file=out,
        )
        return 0
    os.umask(0o077)
    paths.ensure()
    cmd = [sys.executable, "-I", "-m", "switchboard", "start", "--foreground", "--home", str(paths.home)]
    if port is not None:
        cmd += ["--port", str(port)]
    if listen is not None:
        cmd += ["--listen", listen]
    if public_url is not None:
        cmd += ["--public-url", public_url]
    if test_mode:
        cmd.append("--test-mode")
        if test_trust_uds:
            cmd.append("--test-trust-uds")
    fd = os.open(paths.out_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        child = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=fd,
            stderr=fd,
            start_new_session=True,
            env=_broker_env(),
            cwd="/",
        )
    finally:
        os.close(fd)
    deadline = time.monotonic() + START_WAIT_S
    info = None
    while time.monotonic() < deadline:
        info = ping(paths.sock, timeout=1.0)
        if info:
            break
        if child.poll() is not None:
            break
        time.sleep(0.1)
    if not info:
        print("switchboard: the broker did not start. Last lines of its output:", file=sys.stderr)
        print(_tail(paths.out_log), file=sys.stderr)
        return 1
    url = info.get("url") or f"http://switchboard.localhost:{info['port']}/"
    print(f"switchboard is running (pid {info['pid']}) at {url}", file=out)
    if info.get("test_mode"):
        print("TEST MODE", file=out)
    try:
        res = call_sync(paths.sock, "human.login_link", {})
        print(f"Sign in (the link works once, for 5 minutes):\n  {res['url']}", file=out)
    except RpcError as e:
        if e.code == "forbidden" and "arrived through ssh" in e.message:
            # the SSH rules (DESIGN.md §27.5.7): `switchboard login` over this ssh login fails the same way
            print("To sign in, run `switchboard login` in a terminal on this machine.\n"
                  f"No link here: this command {e.message.removeprefix('human.login_link ')}.", file=out)
        elif e.code == "forbidden":
            print("To sign in, run `switchboard login` in your own terminal.", file=out)
        else:
            print(f"switchboard: login link failed: {e.message}", file=sys.stderr)
    return 0


def _tail(p: Path, n: int = 15) -> str:
    try:
        lines = p.read_text(errors="replace").splitlines()
    except OSError:
        return "(no output)"
    return "\n".join(lines[-n:]) or "(no output)"


# ---------------------------------------------------------------------- stop
def stop(paths: Paths, out: Any = None) -> int:
    out = out or sys.stdout
    pf = read_pidfile(paths)
    try:
        call_sync(paths.sock, "sys.stop", {}, timeout=5.0)
    except RpcError as e:
        print(f"switchboard: {e.code}: {e.message}", file=sys.stderr)
        return 1
    except (BrokerDown, ConnectionError, TimeoutError, OSError):
        # The socket doesn't answer: fall back to SIGTERM, but only for the
        # exact process recorded in the pidfile (pid + start time).
        return _sigterm_fallback(paths, pf, out)
    deadline = time.monotonic() + STOP_WAIT_S
    while time.monotonic() < deadline:
        if ping(paths.sock, timeout=0.5) is None and (pf is None or not _pid_matches(pf)):
            print("switchboard stopped", file=out)
            return 0
        time.sleep(0.1)
    print("switchboard: the broker is still shutting down", file=sys.stderr)
    return 1


def _pid_matches(pf: tuple[int, float]) -> bool:
    from switchboard.broker import proc

    return proc.alive(pf[0], pf[1])


def _sigterm_fallback(paths: Paths, pf: tuple[int, float] | None, out: Any) -> int:
    if pf is None or not _pid_matches(pf):
        print("switchboard is not running", file=out)
        return 0
    os.kill(pf[0], signal.SIGTERM)
    deadline = time.monotonic() + STOP_WAIT_S
    while time.monotonic() < deadline:
        if not _pid_matches(pf):
            print("switchboard stopped (SIGTERM)", file=out)
            return 0
        time.sleep(0.1)
    print("switchboard: the broker did not exit after SIGTERM", file=sys.stderr)
    return 1
